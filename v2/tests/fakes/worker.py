"""Scripted fake worker: performs actions through the policy only."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from engine.domain.policy import Action, Actor, Decision, Policy


@dataclass
class FakeWorker:
    project: str
    actor: Actor
    policy: Policy
    log: list[tuple[Action, Decision]] = field(default_factory=list)

    def write(self, rel_path: str, content: str) -> Decision:
        """Policy-checked write inside the worker's copy_root. Writes the
        file only when allowed. Records (action, decision)."""
        root = self.actor.copy_root if self.actor.copy_root is not None else Path(".")
        path = root / rel_path
        action = Action(tool="write", path=path)
        decision = self.policy.check(self.project, self.actor, action)
        self.log.append((action, decision))
        if decision.allowed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return decision

    def read(self, path: Path) -> tuple[Decision, str | None]:
        action = Action(tool="read", path=path)
        decision = self.policy.check(self.project, self.actor, action)
        self.log.append((action, decision))
        if decision.allowed and path.exists():
            return decision, path.read_text(encoding="utf-8")
        return decision, None

    def act(self, action: Action) -> Decision:
        """Generic policy-checked action (terminal/mcp/browser/publish...)
        with no side effect beyond the log."""
        decision = self.policy.check(self.project, self.actor, action)
        self.log.append((action, decision))
        return decision
