"""Unit tests for engine.publishers.ShellPublisher: argv construction,
dry-run vs real argv, the on-disk receipt (crash-safe lookup), timeout
and non-zero-exit handling. Never runs a real deploy command -- every
test wires its own tiny fake script instead."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from engine.fakes.publisher import PublisherCrashed
from engine.publishers import ShellPublisher


def _write_probe(tmp_path: Path) -> Path:
    """argv: <log_path> <exit_code> [...rest logged as one line]."""
    path = tmp_path / "probe.py"
    path.write_text(dedent("""\
        import sys
        from pathlib import Path
        log_path = Path(sys.argv[1])
        exit_code = int(sys.argv[2])
        log_path.write_text(" ".join(sys.argv[3:]), encoding="utf-8")
        raise SystemExit(exit_code)
    """), encoding="utf-8")
    return path


def _write_sleeper(tmp_path: Path) -> Path:
    path = tmp_path / "sleeper.py"
    path.write_text(dedent("""\
        import sys, time
        time.sleep(float(sys.argv[1]))
    """), encoding="utf-8")
    return path


def test_publish_runs_dry_run_argv_by_default_and_logs_argv(tmp_path: Path) -> None:
    probe = _write_probe(tmp_path)
    real_log = tmp_path / "real.log"
    dry_log = tmp_path / "dry.log"
    publisher = ShellPublisher(
        argv_template=(sys.executable, str(probe), str(real_log), "0", "{target}", "real"),
        dry_run_argv_template=(sys.executable, str(probe), str(dry_log), "0", "{target}", "check"),
        receipts_dir=tmp_path / "receipts",
        dry_run=True,
    )
    result = publisher.publish(key="R-1", target="search", artifact_hash="f" * 40)
    assert result == {"id": "R-1", "status": "ok"}
    assert not real_log.exists()
    assert dry_log.read_text(encoding="utf-8") == "search check"

    receipt = publisher.lookup("R-1")
    assert receipt is not None
    assert receipt["status"] == "done"
    assert receipt["dry_run"] is True
    assert receipt["returncode"] == 0
    assert receipt["target"] == "search"
    assert receipt["artifact_hash"] == "f" * 40
    assert receipt["started_ts"] <= receipt["ended_ts"]


def test_publish_runs_real_argv_when_dry_run_false(tmp_path: Path) -> None:
    probe = _write_probe(tmp_path)
    real_log = tmp_path / "real.log"
    publisher = ShellPublisher(
        argv_template=(sys.executable, str(probe), str(real_log), "0", "{target}", "real"),
        dry_run_argv_template=(sys.executable, str(probe), str(tmp_path / "dry.log"), "0"),
        receipts_dir=tmp_path / "receipts",
        dry_run=False,
    )
    publisher.publish(key="R-2", target="search", artifact_hash="a" * 40)
    assert real_log.read_text(encoding="utf-8") == "search real"
    receipt = publisher.lookup("R-2")
    assert receipt["dry_run"] is False


def test_publish_nonzero_exit_raises_publisher_crashed_and_records_failed_receipt(
    tmp_path: Path,
) -> None:
    probe = _write_probe(tmp_path)
    publisher = ShellPublisher(
        argv_template=(sys.executable, str(probe), str(tmp_path / "x.log"), "1"),
        dry_run_argv_template=(sys.executable, str(probe), str(tmp_path / "x.log"), "1"),
        receipts_dir=tmp_path / "receipts",
        dry_run=False,
    )
    with pytest.raises(PublisherCrashed):
        publisher.publish(key="R-3", target="search", artifact_hash="b" * 40)
    receipt = publisher.lookup("R-3")
    assert receipt["status"] == "failed"
    assert receipt["returncode"] == 1

    # Idempotent: calling again re-raises without re-running the script.
    with pytest.raises(PublisherCrashed):
        publisher.publish(key="R-3", target="search", artifact_hash="b" * 40)


def test_publish_timeout_raises_publisher_crashed(tmp_path: Path) -> None:
    sleeper = _write_sleeper(tmp_path)
    publisher = ShellPublisher(
        argv_template=(sys.executable, str(sleeper), "5"),
        dry_run_argv_template=(sys.executable, str(sleeper), "5"),
        receipts_dir=tmp_path / "receipts",
        dry_run=False, timeout_sec=0.2,
    )
    with pytest.raises(PublisherCrashed):
        publisher.publish(key="R-4", target="search", artifact_hash="c" * 40)
    receipt = publisher.lookup("R-4")
    assert receipt["status"] == "failed"
    assert receipt["tail"] == "publish_timeout"


def test_lookup_returns_none_before_any_publish(tmp_path: Path) -> None:
    publisher = ShellPublisher(
        argv_template=(), dry_run_argv_template=(), receipts_dir=tmp_path / "receipts",
    )
    assert publisher.lookup("R-never") is None


def test_receipt_written_before_dispatch_and_lookup_sees_started_mid_flight(
    tmp_path: Path,
) -> None:
    """Simulates a crash strictly between the "started" receipt write and
    the terminal rewrite (there is no step boundary inside publish()
    itself to inject a real crash at, so this calls the same private
    write the real code path uses, directly, white-box) -- lookup()
    must still return the receipt, with status "started"."""
    publisher = ShellPublisher(
        argv_template=(), dry_run_argv_template=(), receipts_dir=tmp_path / "receipts",
    )
    publisher._write_receipt("R-5", {
        "key": "R-5", "target": "search", "artifact_hash": "d" * 40,
        "dry_run": True, "status": "started",
        "started_ts": 1.0, "ended_ts": None, "returncode": None, "tail": "",
    })
    receipt = publisher.lookup("R-5")
    assert receipt is not None
    assert receipt["status"] == "started"
    assert receipt["ended_ts"] is None


def test_publish_second_call_after_done_does_not_rerun(tmp_path: Path) -> None:
    probe = _write_probe(tmp_path)
    log = tmp_path / "x.log"
    publisher = ShellPublisher(
        argv_template=(sys.executable, str(probe), str(log), "0", "first"),
        dry_run_argv_template=(sys.executable, str(probe), str(log), "0", "first"),
        receipts_dir=tmp_path / "receipts",
        dry_run=False,
    )
    publisher.publish(key="R-6", target="search", artifact_hash="e" * 40)
    assert log.read_text(encoding="utf-8") == "first"
    log.unlink()
    result = publisher.publish(key="R-6", target="search", artifact_hash="e" * 40)
    assert result == {"id": "R-6", "status": "ok"}
    assert not log.exists()  # script never ran a second time


def _write_grandchild_spawner(tmp_path: Path, marker: Path) -> Path:
    """A script that spawns a detached grandchild (its own new session,
    so killing only the direct child's pid would not reach it) which
    writes to `marker` after a short delay, then the script itself
    sleeps past the test's timeout."""
    path = tmp_path / "spawner.py"
    path.write_text(dedent(f"""\
        import subprocess, sys, time
        subprocess.Popen(
            [sys.executable, "-c",
             "import time; time.sleep(0.3); open({str(marker)!r}, 'w').write('alive')"],
        )
        time.sleep(5)
    """), encoding="utf-8")
    return path


def test_publish_timeout_kills_whole_process_group_not_just_the_leader(
    tmp_path: Path,
) -> None:
    """P1-6: subprocess.run(timeout=...) alone only kills the immediate
    child -- a grandchild it spawned (inheriting the same process
    group, since it is never detached into its own session) must also
    die when the publisher's own process-group kill fires, confirmed
    by the marker file the grandchild would otherwise still write."""
    marker = tmp_path / "grandchild_ran"
    spawner = _write_grandchild_spawner(tmp_path, marker)
    publisher = ShellPublisher(
        argv_template=(sys.executable, str(spawner)),
        dry_run_argv_template=(sys.executable, str(spawner)),
        receipts_dir=tmp_path / "receipts",
        dry_run=False, timeout_sec=0.1,
    )
    with pytest.raises(PublisherCrashed):
        publisher.publish(key="R-grandchild", target="search", artifact_hash="f" * 40)

    receipt = publisher.lookup("R-grandchild")
    assert receipt["status"] == "failed"
    # Give the grandchild the time it was scheduled to write the marker
    # at (0.3s after spawn) -- if it is still alive, it would have
    # written by now; it must not have, because the whole group died.
    import time as _time
    _time.sleep(0.6)
    assert not marker.exists()


def test_publish_timeout_records_unknown_when_termination_cannot_be_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-6: when the process group's termination cannot be confirmed,
    the receipt must say "unknown", never "failed" -- a confirmed
    failure and a genuinely unresolved outcome must stay distinguishable
    on disk."""
    import engine.publishers as publishers_module

    sleeper = _write_sleeper(tmp_path)
    monkeypatch.setattr(publishers_module, "_terminate_process_group", lambda process: False)
    publisher = ShellPublisher(
        argv_template=(sys.executable, str(sleeper), "5"),
        dry_run_argv_template=(sys.executable, str(sleeper), "5"),
        receipts_dir=tmp_path / "receipts",
        dry_run=False, timeout_sec=0.1,
    )
    with pytest.raises(PublisherCrashed):
        publisher.publish(key="R-unconfirmed", target="search", artifact_hash="a" * 40)
    receipt = publisher.lookup("R-unconfirmed")
    assert receipt["status"] == "unknown"


def test_publish_env_passthrough_includes_xdg_and_dbus(tmp_path: Path) -> None:
    """P1-7: a deploy command driving `systemctl --user` needs the
    user session bus context, not just PATH/HOME/LANG."""
    env_probe = tmp_path / "env_probe.py"
    env_log = tmp_path / "env.log"
    env_probe.write_text(dedent(f"""\
        import os
        with open({str(env_log)!r}, "w", encoding="utf-8") as f:
            for name in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "PATH"):
                f.write(f"{{name}}={{os.environ.get(name, '')}}\\n")
    """), encoding="utf-8")
    publisher = ShellPublisher(
        argv_template=(sys.executable, str(env_probe)),
        dry_run_argv_template=(sys.executable, str(env_probe)),
        receipts_dir=tmp_path / "receipts",
        dry_run=False,
    )
    old_xdg = os.environ.get("XDG_RUNTIME_DIR")
    old_dbus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    os.environ["XDG_RUNTIME_DIR"] = "/run/user/9999"
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/9999/bus"
    try:
        publisher.publish(key="R-env", target="search", artifact_hash="b" * 40)
    finally:
        if old_xdg is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = old_xdg
        if old_dbus is None:
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
        else:
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = old_dbus
    logged = env_log.read_text(encoding="utf-8")
    assert "XDG_RUNTIME_DIR=/run/user/9999" in logged
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/9999/bus" in logged


def test_publish_never_redispatches_a_started_receipt(tmp_path: Path) -> None:
    """P1-1: a receipt left at "started" (the ambiguous crash-mid-
    flight case) must raise PublisherCrashed again on the next publish()
    call WITHOUT running the command a second time."""
    probe = _write_probe(tmp_path)
    log = tmp_path / "x.log"
    publisher = ShellPublisher(
        argv_template=(sys.executable, str(probe), str(log), "0", "ran"),
        dry_run_argv_template=(sys.executable, str(probe), str(log), "0", "ran"),
        receipts_dir=tmp_path / "receipts",
        dry_run=False,
    )
    publisher._write_receipt("R-started", {
        "key": "R-started", "target": "search", "artifact_hash": "c" * 40,
        "dry_run": False, "status": "started",
        "started_ts": 1.0, "ended_ts": None, "returncode": None, "tail": "",
    })
    with pytest.raises(PublisherCrashed):
        publisher.publish(key="R-started", target="search", artifact_hash="c" * 40)
    assert not log.exists()  # the command was never actually run
    receipt = publisher.lookup("R-started")
    assert receipt["status"] == "started"  # untouched -- still the ambiguous receipt
