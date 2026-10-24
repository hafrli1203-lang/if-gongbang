"""Shared fixtures: two fake projects (alpha, beta) as real git repos, an
engine config, a created ledger with both projects registered."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from engine.config import EngineConfig, ProjectConfig
from engine.ledger import Ledger

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
    "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
}


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], check=True,
                         capture_output=True, text=True, env=GIT_ENV)
    return out.stdout.strip()


def make_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (path / ".gitignore").write_text("node_modules/\ndist/\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


ALPHA_FILES = {
    "src/app.py": "print('alpha')\n",
    "package.json": '{"name": "alpha", "scripts": {"test": "pytest"}}\n',
    "tests/bench/cases.json": "[]\n",
    "docs/readme.md": "alpha docs\n",
}
BETA_FILES = {
    "lib/main.py": "print('beta')\n",
    "package.json": '{"name": "beta", "scripts": {"build": "make"}}\n',
    "dist/bundle.js": "built\n",  # ignored by .gitignore; build artifact
}

STATION_OWNERS = {
    "register": {"tester": ("tests/bench/",)},
    "facts": {"fact_checker": ("facts.md",)},
    "plan": {"planner": ("plan.md", "tools.md")},
    "design": {"designer": ("outputs/design/",)},
    "build": {"builder": ("src/", "lib/", "docs/", "package.json")},
    "review": {"reviewer": ("review.md",)},
}


@dataclass
class Workspace:
    root: Path
    config: EngineConfig
    ledger: Ledger
    alpha: Path
    beta: Path


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    alpha = make_repo(tmp_path / "repos" / "alpha", ALPHA_FILES)
    beta = make_repo(tmp_path / "repos" / "beta", BETA_FILES)
    home = tmp_path / "home"
    secrets = tmp_path / "secrets"
    home.mkdir()
    secrets.mkdir()
    (secrets / "prod.key").write_text("SECRET\n", encoding="utf-8")
    config = EngineConfig(
        ledger_path=tmp_path / "ledger.sqlite",
        home=home,
        secrets_dir=secrets,
        projects={
            "alpha": ProjectConfig("alpha", alpha, excludes=("node_modules/",)),
            "beta": ProjectConfig("beta", beta, excludes=("node_modules/",),
                                  artifact_paths=("dist/",)),
        },
        station_owners=STATION_OWNERS,
        protected_paths=(".gitignore", "package-lock.json", ".claude/"),
        hook_timeout_sec=0.2,
    )
    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("alpha")
    ledger.register_project("beta")
    return Workspace(tmp_path, config, ledger, alpha, beta)


_LIVE_CLIS = {"claude", "codex"}


@pytest.fixture(autouse=True)
def _no_live_ai_cli(monkeypatch: pytest.MonkeyPatch):
    """Refuse to start a real claude/codex worker from any test unless
    GB2_LIVE=1 is set. Every worker (station and review) is started
    through engine.procs.spawn, so one guard there covers all of them,
    whatever module-import order a test run happens to have."""
    import os

    from engine import procs

    if os.environ.get("GB2_LIVE") == "1":
        yield
        return
    real_spawn = procs.spawn

    def guarded(ledger, project, attempt_id, station, argv, cwd, **kwargs):
        if any(Path(str(a)).name in _LIVE_CLIS for a in argv):
            raise RuntimeError(
                f"test tried to start a live AI CLI ({station}); set GB2_LIVE=1 to allow")
        return real_spawn(ledger, project, attempt_id, station, argv, cwd, **kwargs)

    monkeypatch.setattr(procs, "spawn", guarded)
    yield
