"""Unit tests for engine.fakes.publisher and engine.fakes.installer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher, PublisherCrashed


def test_publisher_idempotent_by_key() -> None:
    pub = FakePublisher()
    first = pub.publish("R-1", "store", "hash1")
    second = pub.publish("R-1", "store", "hash1")
    assert first == {"id": "R-1", "status": "ok"}
    assert second == {"id": "R-1", "status": "ok"}
    assert len(pub.calls) == 1


def test_publisher_fail_after_effect_records_then_raises() -> None:
    pub = FakePublisher(fail_after_effect=True)
    with pytest.raises(PublisherCrashed):
        pub.publish("R-2", "store", "hash2")
    assert len(pub.calls) == 1
    assert pub.calls[0].key == "R-2"
    # the side effect happened even though the caller never got a response
    assert pub.lookup("R-2") == {"id": "R-2", "key": "R-2", "target": "store",
                                 "artifact_hash": "hash2", "status": "done"}


def test_publisher_lookup_fails_raises() -> None:
    pub = FakePublisher(lookup_fails=True)
    with pytest.raises(RuntimeError):
        pub.lookup("R-3")


def test_publisher_lookup_missing_key_returns_none() -> None:
    pub = FakePublisher()
    assert pub.lookup("R-missing") is None


def test_installer_merges_nested_dict_one_level(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"deps": {"a": "1"}}), encoding="utf-8")
    installer = FakeInstaller()
    installer.apply("R-1", manifest, {"deps": {"b": "2"}})
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data == {"deps": {"a": "1", "b": "2"}}
    assert installer.applied("R-1")


def test_installer_idempotent_by_key(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"deps": {}}), encoding="utf-8")
    installer = FakeInstaller()
    installer.apply("R-1", manifest, {"deps": {"a": "1"}})
    installer.apply("R-1", manifest, {"deps": {"b": "2"}})  # no-op: same key
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data == {"deps": {"a": "1"}}


def test_installer_fail_after_effect_writes_then_raises(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({}), encoding="utf-8")
    installer = FakeInstaller(fail_after_effect=True)
    with pytest.raises(RuntimeError):
        installer.apply("R-1", manifest, {"x": 1})
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data == {"x": 1}
    assert installer.applied("R-1")
