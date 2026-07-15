"""Persistent standalone voice-recognition workflow tests."""
import json
import os

from extractor import voice_recognition as vr
from extractor.asr import ASRResult
from extractor.exporter import ChatLabExporter
from tests.conftest import insert_conversation, insert_message


def _raw(cj: dict) -> str:
    return json.dumps({"content_json": json.dumps(cj)}, ensure_ascii=False)


def _seed_voice(temp_db, tmp_path, monkeypatch):
    import common.paths as paths
    import extractor.models as models

    media_dir = tmp_path / "media"
    voice_dir = media_dir / "voice"
    voice_dir.mkdir(parents=True)
    audio = voice_dir / "10001.mpeg"
    audio.write_bytes(b"douyin voice")
    monkeypatch.setattr(paths, "MEDIA_DIR", str(media_dir))

    conn = models.get_db()
    insert_conversation(
        conn, "c1", "识别会话", participant_uids='["owner"]', last_message_time=10
    )
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('owner','我')")
    insert_message(
        conn,
        "srv_10001",
        "c1",
        1,
        sender_uid="owner",
        msg_type=0,
        media_local_path="voice/10001.mpeg",
        raw_data=_raw({"resource_url": {"url_list": ["x"]}, "duration": 3000}),
    )
    conn.commit()
    conn.close()
    return audio


def test_standalone_recognition_persists_progress_and_export_reuses_cache(
    temp_db, tmp_path, monkeypatch
):
    audio = _seed_voice(temp_db, tmp_path, monkeypatch)
    progress = []
    constructed = []

    class FakeClient:
        def __init__(self, server_url, **kwargs):
            constructed.append((server_url, kwargs["batch_size"]))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def transcribe_files(self, paths, progress_cb=None):
            path = os.path.abspath(list(paths)[0])
            result = ASRResult(text="这是持久化识别文本。", language="Chinese")
            if progress_cb:
                progress_cb({"path": path, "result": result, "error": None,
                             "done": 1, "total": 1})
            return {path: result}, {}

    monkeypatch.setattr(vr, "QwenASRClient", FakeClient)
    result = vr.recognize_voice_messages(
        conversations=["识别会话"],
        asr_url="http://asr.test:8111",
        batch_size=50,
        progress_cb=progress.append,
    )

    assert constructed == [("http://asr.test:8111", 10)]
    assert result["total"] == 1 and result["done"] == 1 and result["ok"] == 1
    assert progress[-1]["phase"] == "completed"

    # A second standalone run skips the valid cache without constructing a client.
    constructed.clear()
    second = vr.recognize_voice_messages(
        conversations=["识别会话"], asr_url="http://asr.test:8111"
    )
    assert second["skipped"] == 1 and second["done"] == 1
    assert constructed == []

    # Export reuses the saved result even when "recognize during export" is off.
    out = tmp_path / "cached.json"
    exporter = ChatLabExporter(
        conv_name="识别会话", output_format="json", asr_url=""
    )
    exporter.export(str(out))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["messages"][0]["content"] == "[语音转文本] 这是持久化识别文本。"
    assert exporter.last_stats["asr"]["cached"] == 1

    # Changing the source invalidates the fingerprint and requires recognition.
    audio.write_bytes(b"changed voice payload")
    conn = vr.get_db()
    assert vr.load_cached_transcription(conn, "srv_10001", str(audio)) is None
    conn.close()


def test_force_recognition_does_not_skip_cache(temp_db, tmp_path, monkeypatch):
    audio = _seed_voice(temp_db, tmp_path, monkeypatch)
    conn = vr.get_db()
    vr.save_cached_transcription(
        conn, "srv_10001", ASRResult(text="旧文本"), str(audio), "http://old"
    )
    conn.commit()
    conn.close()
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            calls.append(1)

        def __enter__(self): return self
        def __exit__(self, *args): pass

        def transcribe_files(self, paths, progress_cb=None):
            path = os.path.abspath(list(paths)[0])
            result = ASRResult(text="新文本")
            progress_cb({"path": path, "result": result, "error": None,
                         "done": 1, "total": 1})
            return {path: result}, {}

    monkeypatch.setattr(vr, "QwenASRClient", FakeClient)
    result = vr.recognize_voice_messages(
        conversations=["识别会话"], asr_url="http://new", force=True
    )
    assert calls == [1]
    assert result["ok"] == 1 and result["skipped"] == 0
