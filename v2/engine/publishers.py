"""Real deploy publisher: drives whatever deploy command a project's
config.publisher_argv/publisher_dry_run_argv name (e.g. a shared deploy
script taking an app name and an optional "--check" flag) instead of
only FakePublisher's in-memory stand-in. Same publish()/lookup()
interface FakePublisher has, so engine.steps.publish's call_publisher/
_reconcile_call_publisher work unchanged regardless of which one
engine.cli wires into Fakes.publisher.

Crash-safe across resume: before running the command, writes a receipt
JSON to receipts_dir/<key>.json with status="started"; after the
command exits (or fails to even start, confirms as terminated on
timeout, or cannot confirm termination), rewrites the SAME file with
status "done", "failed", or "unknown" plus the actual returncode/tail.
lookup(key) just reads this file back. The three receipt statuses mean
exactly, and only, this:
  "done"    the command exited 0 -- the only status publish()/
            reconcile() ever treat as success.
  "failed"  the command exited non-zero, or timed out and its whole
            process group was confirmed terminated, or never started
            at all (spawn failure) -- a known, final outcome.
  "started" or "unknown"  the outcome could not be determined (a crash
            strictly between the "started" write and any terminal
            rewrite, or a timeout whose process group could not be
            confirmed dead) -- never treated as success, and never
            silently re-run (see publish()'s own guard below).
A crash strictly between the "started" write and a terminal rewrite is
exactly the "started" case above (verified directly:
tests/unit/test_publishers.py); engine.steps.publish's own reconcile
decides what a resumed run_once does with it, this module only
guarantees the receipt itself never claims success by accident.

Never run inside the OS sandbox (engine.sandbox) -- a deploy script
restarts real systemd user units and swaps real directories on the
host; sandboxing it would defeat the whole point. Runs with an
explicitly sanitized env (read from THIS process's own os.environ --
never anything request- or worker-controlled) instead of inheriting
the processor's full environment, same reasoning as
bench_run._bench_env.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from engine.fakes.publisher import PublisherCrashed

# PATH/HOME/LANG for a plain shell command; XDG_RUNTIME_DIR and
# DBUS_SESSION_BUS_ADDRESS so a deploy command that drives
# `systemctl --user` can actually reach the user session bus -- without
# them a deploy running as a non-interactive/cron-like process gets
# "Failed to connect to bus" even though PATH/HOME/LANG are all correct.
_DEFAULT_ENV_PASSTHROUGH: tuple[str, ...] = (
    "PATH", "HOME", "LANG", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
)
_TAIL_CHARS = 4000
_TERM_GRACE_SEC = 2.0
_KILL_GRACE_SEC = 2.0


def _pgid_alive(pgid: int) -> bool:
    """Signal 0 to a whole process group: no actual signal is sent, but
    the OS still reports whether the group still has any member."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True  # fail safe: an unexpected errno means "not confirmed gone"


def _terminate_process_group(process: "subprocess.Popen[str]") -> bool:
    """TERM the whole group, reap the leader and give it a grace
    period, KILL any survivor, reap and grace again, and report
    whether every member is now CONFIRMED gone. `process.wait()` is
    called (not just a sleep) after each signal: an unreaped child
    that already exited is still a zombie, and a zombie still counts
    as a live process-group member to killpg's own existence check --
    without reaping, this would always see "still alive" and never
    confirm termination even for an already-dead leader. False means
    termination could not be confirmed (a signal failed for a reason
    other than "already gone", or some member -- e.g. a grandchild
    this process never directly parented and so can never reap --
    outlived SIGKILL) -- callers must treat that as a genuinely
    unresolved outcome, never a confirmed failure."""
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    try:
        process.wait(timeout=_TERM_GRACE_SEC)
    except subprocess.TimeoutExpired:
        pass
    if not _pgid_alive(pgid):
        return True
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    try:
        process.wait(timeout=_KILL_GRACE_SEC)
    except subprocess.TimeoutExpired:
        pass
    return _wait_group_gone(pgid, _KILL_GRACE_SEC)


def _wait_group_gone(pgid: int, limit_sec: float) -> bool:
    """Poll until no member of the group remains. Killed grandchildren
    are reaped by init (or the nearest subreaper), not by this process,
    so they can linger as zombies for a moment on a loaded host; one
    immediate check would report a group that is about to disappear as
    "not confirmed gone"."""
    deadline = time.monotonic() + limit_sec
    while _pgid_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


@dataclass
class ShellPublisher:
    argv_template: tuple[str, ...]
    dry_run_argv_template: tuple[str, ...]
    receipts_dir: Path
    timeout_sec: float = 900.0
    cwd: Path | None = None
    dry_run: bool = True
    env_passthrough: tuple[str, ...] = field(default_factory=lambda: _DEFAULT_ENV_PASSTHROUGH)

    def _receipt_path(self, key: str) -> Path:
        return self.receipts_dir / f"{key}.json"

    def _write_receipt(self, key: str, data: dict[str, object]) -> None:
        path = self._receipt_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _env(self) -> dict[str, str]:
        return {name: os.environ[name] for name in self.env_passthrough if name in os.environ}

    def _argv(self, target: str) -> list[str]:
        template = self.dry_run_argv_template if self.dry_run else self.argv_template
        return [part.format(target=target) for part in template]

    def publish(self, key: str, target: str, artifact_hash: str) -> dict[str, object]:
        """Idempotent per key: ANY existing receipt (any status at all)
        stops this from touching the host a second time. "done" returns
        the same {"id","status":"ok"} FakePublisher would; "failed",
        "started", or "unknown" all re-raise PublisherCrashed (matching
        FakePublisher's "ask again, get the same crash signal" shape)
        without running the command again -- an unresolved "started"/
        "unknown" receipt is never blindly redispatched, since the
        command may still be running or may already have taken effect."""
        existing = self.lookup(key)
        if existing is not None and existing.get("status") == "done":
            return {"id": key, "status": "ok"}
        if existing is not None:
            raise PublisherCrashed(key)

        argv = self._argv(target)
        started_ts = time.time()
        self._write_receipt(key, {
            "key": key, "target": target, "artifact_hash": artifact_hash,
            "dry_run": self.dry_run, "status": "started",
            "started_ts": started_ts, "ended_ts": None,
            "returncode": None, "tail": "",
        })
        try:
            process = subprocess.Popen(
                argv, cwd=(str(self.cwd) if self.cwd is not None else None),
                env=self._env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True,
            )
        except OSError as exc:
            self._write_receipt(key, {
                "key": key, "target": target, "artifact_hash": artifact_hash,
                "dry_run": self.dry_run, "status": "failed",
                "started_ts": started_ts, "ended_ts": time.time(),
                "returncode": None, "tail": f"publish_spawn_failed:{exc}",
            })
            raise PublisherCrashed(key) from exc

        try:
            stdout, stderr = process.communicate(timeout=self.timeout_sec)
            returncode: int | None = process.returncode
            tail = ((stdout or "") + (stderr or ""))[-_TAIL_CHARS:]
        except subprocess.TimeoutExpired:
            # start_new_session=True put this process in its own, fresh
            # process group (same pid), so killing that whole group also
            # reaches anything it spawned -- a plain process.kill() would
            # only reach the immediate child, leaving the real deploy
            # work (and its descendants) running past the timeout.
            confirmed_dead = _terminate_process_group(process)
            try:
                stdout, stderr = process.communicate(timeout=_KILL_GRACE_SEC)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            tail = (((stdout or "") + (stderr or ""))[-_TAIL_CHARS:]) or "publish_timeout"
            status = "failed" if confirmed_dead else "unknown"
            self._write_receipt(key, {
                "key": key, "target": target, "artifact_hash": artifact_hash,
                "dry_run": self.dry_run, "status": status,
                "started_ts": started_ts, "ended_ts": time.time(),
                "returncode": None, "tail": tail,
            })
            raise PublisherCrashed(key)

        self._write_receipt(key, {
            "key": key, "target": target, "artifact_hash": artifact_hash,
            "dry_run": self.dry_run, "status": ("done" if returncode == 0 else "failed"),
            "started_ts": started_ts, "ended_ts": time.time(),
            "returncode": returncode, "tail": tail,
        })
        if returncode != 0:
            raise PublisherCrashed(key)
        return {"id": key, "status": "ok"}

    def lookup(self, key: str) -> dict[str, object] | None:
        path = self._receipt_path(key)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"publisher receipt unreadable for {key!r}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError(f"publisher receipt malformed for {key!r}")
        return raw


__all__ = ["ShellPublisher"]
