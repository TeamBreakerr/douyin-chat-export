"""Tests for common.config (load/save + password hash)."""
import json

import common.paths as paths
from common import config


def test_missing_file_returns_fresh_independent_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CONFIG_PATH", str(tmp_path / "nope.json"))
    a = config.load_config()
    assert a == {"custom_filters": [], "schedule": ""}
    # Mutating one result's nested list must not leak into the next call.
    a["custom_filters"].append("x")
    b = config.load_config()
    assert b["custom_filters"] == []
    assert config.get_password_hash() is None  # never raises FileNotFoundError


def test_corrupt_file_falls_back_to_defaults(tmp_path, monkeypatch, capsys):
    p = tmp_path / "panel_config.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(paths, "CONFIG_PATH", str(p))
    assert config.load_config() == {"custom_filters": [], "schedule": ""}
    assert "损坏" in capsys.readouterr().out  # warns only on genuine corruption


def test_save_then_load_roundtrip_and_password(tmp_path, monkeypatch):
    p = tmp_path / "panel_config.json"
    monkeypatch.setattr(paths, "CONFIG_PATH", str(p))
    config.save_config({"schedule": "0 6 * * *", "password_hash": "abc"})
    assert json.loads(p.read_text(encoding="utf-8"))["schedule"] == "0 6 * * *"
    assert config.load_config()["schedule"] == "0 6 * * *"
    assert config.get_password_hash() == "abc"
