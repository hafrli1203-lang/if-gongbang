"""Test-only stand-in for engine.plan.Plan/Scene/Task (worker A's file,
not yet merged into this branch -- see
(the full-orbit contract). Field
names/shapes copied verbatim from that contract so these tests keep
working unchanged once engine.plan lands and this stub is dropped.
Nothing in engine/acceptance.py or engine/handoff.py imports this file
-- both modules only duck-type a Plan-shaped object (see their
`if TYPE_CHECKING` guards), so they never need engine.plan to exist to
be importable or usable."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scene:
    id: str
    actor_does: str
    app_shows: str
    where: str
    cmd: tuple[str, ...]


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    detail: str
    owns: tuple[str, ...]
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    goal: str
    ui: bool
    scenes: tuple[Scene, ...]
    tasks: tuple[Task, ...]
    acceptance: tuple[str, ...]
