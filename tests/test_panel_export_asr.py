"""Control-panel contract tests for ASR export settings."""
import asyncio

import pytest

from backend import control_panel as cp


@pytest.fixture(autouse=True)
def restore_job_states():
    export_state = dict(cp._export_state)
    recognition_state = dict(cp._voice_recognition_state)
    yield
    cp._export_state.clear()
    cp._export_state.update(export_state)
    cp._voice_recognition_state.clear()
    cp._voice_recognition_state.update(recognition_state)


def test_export_rejects_enabled_asr_without_url(monkeypatch):
    monkeypatch.setitem(cp._export_state, "status", "idle")
    request = cp.ExportRequest(transcribe_voice=True, asr_url="")

    response = asyncio.run(cp.start_export(request))

    assert response.status_code == 400
    assert cp._export_state["status"] == "idle"


def test_export_persists_and_passes_normalized_asr_settings(monkeypatch):
    monkeypatch.setitem(cp._export_state, "status", "idle")
    saved = {}
    called = {}
    monkeypatch.setattr(cp, "_load_config", lambda: {"custom_filters": []})
    monkeypatch.setattr(cp, "_save_config", lambda cfg: saved.update(cfg))

    def fake_export(*args):
        called["args"] = args
        cp._export_state["status"] = "completed"
        cp._export_state["message"] = "ok"

    monkeypatch.setattr(cp, "_do_export", fake_export)
    request = cp.ExportRequest(
        format="json",
        conversations=["测试会话"],
        transcribe_voice=True,
        asr_url="http://asr.test:8111/v1/audio/transcriptions",
        asr_language="Chinese",
        asr_prompt="人名：小明",
    )

    response = asyncio.run(cp.start_export(request))

    assert response["status"] == "completed"
    assert called["args"] == (
        "json",
        "",
        ["测试会话"],
        "http://asr.test:8111",
        "Chinese",
        "人名：小明",
        10,
    )
    assert saved["export_selected"] == ["测试会话"]
    assert saved["asr_enabled"] is True
    assert saved["asr_url"] == "http://asr.test:8111"
    assert saved["asr_language"] == "Chinese"
    assert saved["asr_prompt"] == "人名：小明"
    assert saved["asr_use_batch"] is True
    assert saved["asr_batch_size"] == 10


def test_standalone_recognition_starts_background_job_and_clamps_batch(monkeypatch):
    monkeypatch.setitem(cp._voice_recognition_state, "status", "idle")
    monkeypatch.setitem(cp._export_state, "status", "idle")
    saved = {}
    created = []
    monkeypatch.setattr(cp, "_load_config", lambda: {})
    monkeypatch.setattr(cp, "_save_config", lambda cfg: saved.update(cfg))

    def fake_create_task(coro):
        created.append(coro)
        coro.close()
        return "task"

    monkeypatch.setattr(cp.asyncio, "create_task", fake_create_task)
    request = cp.VoiceRecognitionRequest(
        conversations=["识别会话"],
        asr_url="http://asr.test:8111/v1/audio/transcriptions",
        use_batch=True,
        batch_size=999,
    )

    response = asyncio.run(cp.start_voice_recognition(request))

    assert response == {"status": "started"}
    assert len(created) == 1
    assert cp._voice_recognition_state["status"] == "running"
    assert saved["recognition_selected"] == ["识别会话"]
    assert saved["asr_url"] == "http://asr.test:8111"
    assert saved["asr_use_batch"] is True
    assert saved["asr_batch_size"] == 10


def test_recognition_progress_state_contains_counts_and_current(monkeypatch):
    monkeypatch.setitem(cp._voice_recognition_state, "status", "running")
    cp._update_voice_recognition_progress({
        "phase": "recognizing",
        "total": 12,
        "done": 7,
        "ok": 5,
        "failed": 1,
        "empty": 0,
        "missing": 1,
        "skipped": 0,
        "current": "7659828680696448563.mpeg",
    })

    state = cp._voice_recognition_payload()
    assert state["done"] == 7 and state["total"] == 12
    assert state["ok"] == 5 and state["failed"] == 1 and state["missing"] == 1
    assert "7659828680696448563.mpeg" in state["message"]
