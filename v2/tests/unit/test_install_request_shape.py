"""결함 58: a builder worker's install-request instruction
(engine.infrastructure.workers.prompts.build_prompt) and the owner-
approval gate that validates one (engine.application.owner_approval.
validate_pending) must describe/accept the SAME request shape for a
given manifest type -- both now read engine.domain.install_request_shape
so they cannot drift apart.

The round-trip tests below build a request that matches exactly what
build_prompt's own instruction text describes, then check it passes
validate_pending -- the thing that was broken before this fix (every
.json-manifest install request built as instructed was rejected with
manifest_section_not_allowed)."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from engine.domain.install_request_shape import (
    JSON_INSTALL_SECTIONS,
    install_request_shape_text,
    is_json_manifest,
)
from engine.owner import validate_pending
from engine.workers.base import WorkerSpec
from engine.workers.prompts import build_prompt
from tests.conftest import Workspace


def _with_manifest(workspace: Workspace, manifest: str) -> Workspace:
    project = replace(workspace.config.projects["alpha"], manifest=manifest)
    config = replace(workspace.config,
                      projects={**workspace.config.projects, "alpha": project})
    return replace(workspace, config=config)


def _spec(tmp_path: Path) -> WorkerSpec:
    copy_root = tmp_path / "copy"
    copy_root.mkdir()
    return WorkerSpec(
        project="alpha", attempt_id="J-alpha-TEST#1", generation=1,
        station="build", role="builder", copy_root=copy_root, workdir=copy_root,
        task="do the thing", result_path=tmp_path / "result.json",
        config_path=tmp_path / "engine.json",
    )


def test_is_json_manifest() -> None:
    assert is_json_manifest("package.json") is True
    assert is_json_manifest("requirements.txt") is False


def test_json_shape_text_names_only_the_allowed_sections() -> None:
    text = install_request_shape_text("package.json")
    for section in JSON_INSTALL_SECTIONS:
        assert section in text
    assert "add_lines" not in text


def test_line_list_shape_text_is_unchanged_add_lines() -> None:
    text = install_request_shape_text("requirements.txt")
    assert "add_lines" in text


def test_json_manifest_request_built_as_instructed_is_approved(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """The exact request shape build_prompt's own instruction text
    describes for a .json manifest -- a dependencies/devDependencies/
    optionalDependencies section -- must pass validate_pending, not be
    rejected with manifest_section_not_allowed."""
    spec = _spec(tmp_path)
    prompt_text = build_prompt(spec, ("src/",), (r"ls",), (), manifest="package.json")
    assert "dependencies" in prompt_text

    ws = _with_manifest(workspace, "package.json")
    request = {
        "kind": "install", "manifest": "package.json",
        "change": {"devDependencies": {"jsdom": "^24.0.0"}},
    }
    ok, reason = validate_pending(ws.config, "alpha", request)
    assert (ok, reason) == (True, "ok")


def test_line_list_manifest_request_built_as_instructed_is_approved(
    workspace: Workspace, tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    prompt_text = build_prompt(
        spec, ("src/",), (r"ls",), (), manifest="requirements.txt",
    )
    assert "add_lines" in prompt_text

    (workspace.alpha / "requirements.txt").write_text("", encoding="utf-8")
    ws = _with_manifest(workspace, "requirements.txt")
    request = {
        "kind": "install", "manifest": "requirements.txt",
        "change": {"add_lines": ["pkg==1.0.0"]},
    }
    ok, reason = validate_pending(ws.config, "alpha", request)
    assert (ok, reason) == (True, "ok")


def test_json_manifest_add_lines_shape_is_still_rejected(
    workspace: Workspace,
) -> None:
    """Security must not loosen: an add_lines-shaped request against a
    .json manifest is still rejected (it is not a dict of sections)."""
    ws = _with_manifest(workspace, "package.json")
    request = {
        "kind": "install", "manifest": "package.json",
        "change": {"add_lines": ["jsdom"]},
    }
    ok, reason = validate_pending(ws.config, "alpha", request)
    assert ok is False
    assert reason == "manifest_section_not_allowed"
