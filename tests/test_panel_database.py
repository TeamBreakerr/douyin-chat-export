"""Tests for full SQLite export/import controls."""

import asyncio
import sqlite3

from backend import control_panel as cp
from common import paths
from common.db import connect, upsert_conversation
from tests.conftest import insert_message


def _reset_job_states(monkeypatch):
    monkeypatch.setitem(cp._scrape_state, "status", "idle")
    monkeypatch.setitem(cp._backfill_state, "status", "idle")
    monkeypatch.setitem(cp._video_backfill_state, "status", "idle")
    monkeypatch.setitem(cp._export_state, "status", "idle")


def test_database_export_is_a_consistent_sqlite_file(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", str(tmp_path))
    _reset_job_states(monkeypatch)

    conn = connect(foreign_keys=True)
    upsert_conversation(conn, "c1", name="会话")
    insert_message(conn, "m1", "c1", 1, content="消息")
    conn.commit()
    conn.close()

    output_path = cp._do_database_export()

    exported = sqlite3.connect(output_path)
    assert exported.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert exported.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert exported.execute("SELECT name FROM conversations").fetchone()[0] == "会话"
    exported.close()


def test_database_import_validates_and_replaces_current_file(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", str(tmp_path))
    _reset_job_states(monkeypatch)

    conn = connect(foreign_keys=True)
    upsert_conversation(conn, "current", name="当前")
    insert_message(conn, "current-message", "current", 1, content="当前数据")
    conn.commit()
    conn.close()

    imported_path = cp._do_database_export()
    conn = connect(foreign_keys=True)
    upsert_conversation(conn, "extra", name="额外")
    insert_message(conn, "extra-message", "extra", 1, content="应被覆盖")
    conn.commit()
    conn.close()

    class FakeRequest:
        headers = {"content-type": "application/octet-stream"}

        def __init__(self, payload):
            self.payload = payload

        async def stream(self):
            yield self.payload

    payload = asyncio.run(cp.import_database(FakeRequest(open(imported_path, "rb").read())))

    assert payload["status"] == "ok"
    assert payload["backup_file"]
    assert (tmp_path / payload["backup_file"]).exists()

    conn = sqlite3.connect(paths.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    assert conn.execute("SELECT name FROM conversations").fetchone()[0] == "当前"
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    conn.close()


def test_database_import_rejects_invalid_file(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", str(tmp_path))
    _reset_job_states(monkeypatch)

    class FakeRequest:
        headers = {"content-type": "application/octet-stream"}

        async def stream(self):
            yield b"not a sqlite database"

    response = asyncio.run(cp.import_database(FakeRequest()))

    assert response.status_code == 400
    assert "数据库校验失败" in response.body.decode("utf-8")
