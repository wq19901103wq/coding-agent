import json
import os

import pytest

from agent.atomic_io import atomic_write_json, atomic_write_text


def test_atomic_write_json_replaces_complete_document(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"status": "running"})
    atomic_write_json(target, {"status": "completed", "resolved": True})

    assert json.loads(target.read_text()) == {"status": "completed", "resolved": True}
    assert not list(tmp_path.glob(".state.json.*"))


def test_atomic_write_keeps_previous_file_when_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    atomic_write_text(target, "old")

    def fail_replace(_source, _target):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated crash"):
        atomic_write_text(target, "new")

    assert target.read_text() == "old"
    assert not list(tmp_path.glob(".state.json.*"))
