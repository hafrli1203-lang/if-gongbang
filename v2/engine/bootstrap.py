"""engine.bootstrap -- install a project's declared dependencies the
FIRST time its source repo has none: `gb2
new`'s generated skeleton package.json lists a devDependency
(vitest), but nothing ever ran `npm install` for it -- a worktree only
copies node_modules from the source repo IF the source repo already
has one (tools/gb2's `_ensure_worktree`), and a brand-new app's source
repo never does. Every worker/bench run then fails before it can even
run a single test. This module is the one place that gap is closed;
it does not itself decide WHEN to run -- a caller (tools/gb2, outside
this task's owned files -- see the report for the one-line call site)
invokes `ensure_installed` once, right after the source repo's
package.json is written and before the first worker/bench run touches
it, and journals the result via `record_bootstrap`.

Never reinstalls an app's own node_modules once it exists -- that
directory is the owner's from then on, same as every other project
file; this only ever fires for a project that has none yet.

Security note (P1 finding: bootstrap bypassed install hardening): a
brand-new app's package.json comes from the owner's own `gb2 new`
scaffold, but `install_cmd` still runs an ordinary npm/pnpm/yarn
install against it, which can still run a dependency's lifecycle
script with this process's own ambient environment -- exactly the
supply-chain vector engine.installers.RealInstaller already closes for
every OTHER install in the engine. `ensure_installed` now runs through
the same engine.installers.run_install_cmd this process's other
installs use (--ignore-scripts, a minimal explicit env, a fresh empty
$HOME, and optional sandbox_mode/hidden_paths/net isolation), instead
of a bare subprocess.run -- see run_install_cmd's own docstring for
what each of those does."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from engine import sandbox
from engine.installers import run_install_cmd
from engine.ledger import Event, Ledger


@dataclass(frozen=True)
class BootstrapResult:
    ran: bool  # False: node_modules already present (or no install_cmd), nothing run
    ok: bool  # True iff ran is False, or ran is True and the command exited 0
    detail: str  # "" | "already_installed" | "no_install_cmd" | "install_failed:..."


def ensure_installed(repo: Path, install_cmd: tuple[str, ...], *,
                     timeout_sec: float = 600.0,
                     allow_lifecycle_scripts: bool = False,
                     sandbox_mode: str = "none",
                     hidden_paths: tuple[Path, ...] = (),
                     net: bool = True) -> BootstrapResult:
    """Run `install_cmd` inside `repo`, but only iff `repo/node_modules`
    does not exist yet. Never raises: a failed or unavailable install
    (offline, npm missing, timed out, sandbox unavailable) is reported
    in the result, not an exception, so a caller can decide whether to
    continue (a `gb2 doctor`-style report keeps surfacing node_modules
    as missing) or treat it as fatal for that one call.

    `allow_lifecycle_scripts`/`sandbox_mode`/`hidden_paths`/`net` are
    forwarded to engine.installers.run_install_cmd unchanged -- see its
    docstring. Defaults match that function's own (no sandbox, scripts
    suppressed for npm/pnpm/yarn), so an existing caller that passes
    none of them keeps the same no-sandbox behavior, just with the
    hardened env/--ignore-scripts applied."""
    if (repo / "node_modules").exists():
        return BootstrapResult(ran=False, ok=True, detail="already_installed")
    if not install_cmd:
        return BootstrapResult(ran=False, ok=False, detail="no_install_cmd")
    try:
        result = run_install_cmd(
            repo, install_cmd, timeout_sec=timeout_sec,
            allow_lifecycle_scripts=allow_lifecycle_scripts,
            sandbox_mode=sandbox_mode, hidden_paths=hidden_paths, net=net,
        )
    except (OSError, subprocess.TimeoutExpired, sandbox.SandboxUnavailable) as exc:
        return BootstrapResult(
            ran=True, ok=False,
            detail=f"install_failed:{type(exc).__name__}: {exc}",
        )
    if result.returncode == 0:
        return BootstrapResult(ran=True, ok=True, detail="")
    stdout = (result.stdout or b"").decode("utf-8", "replace")
    stderr = (result.stderr or b"").decode("utf-8", "replace")
    tail = (stdout + stderr)[-2000:]
    return BootstrapResult(ran=True, ok=False, detail=f"install_failed:{tail}")


def record_bootstrap(ledger: Ledger, project: str, result: BootstrapResult) -> int:
    """Append one dependency.installed event (an unknown kind to
    engine.ledger's own handlers -- stored with no derived-table
    effect, same as any other audit-only event kind; see
    Ledger.append's own docstring). Returns the new seq."""
    return ledger.append(Event(
        project=project, kind="dependency.installed", actor_kind="processor",
        actor_id="processor",
        payload={"ran": result.ran, "ok": result.ok, "detail": result.detail},
    ))


__all__ = ["BootstrapResult", "ensure_installed", "record_bootstrap"]
