"""Database, viewer, exporter, and rollback integration for transcriptions."""

import asyncio
import json

from common.db import connect, upsert_voice_transcription
from extractor.web_scraper import WebChatScraper
from tests.conftest import insert_conversation, insert_message
from tests.test_voice_transcriber import _voice_raw


def _seed_voice(temp_db):
    conn = connect(foreign_keys=True)
    insert_conversation(
        conn,
        "c1",
        "语音会话",
        participant_uids='["owner","sender"]',
        last_message_time=10,
    )
    insert_message(
        conn,
        "m1",
        "c1",
        1,
        sender_uid="sender",
        content="[语音 2秒]",
        msg_type=0,
        raw_data=_voice_raw("5001"),
        timestamp=10,
    )
    upsert_voice_transcription(conn, "m1", "5001", "这是语音转写", "success", updated_at=7)
    conn.commit()
    return conn


def test_reader_search_and_single_message_include_transcription(temp_db):
    conn = _seed_voice(temp_db)
    conn.close()

    import backend.database as database

    items, total = database.get_messages("c1")
    assert total == 1
    assert items[0]["voice_transcription"] == "这是语音转写"
    assert items[0]["voice_transcription_status"] == "success"

    matches, match_total = database.search_messages("转写")
    assert match_total == 1
    assert matches[0]["msg_id"] == "m1"
    assert database.get_message("m1")["voice_transcription"] == "这是语音转写"


def test_exporter_uses_transcription_but_keeps_voice_label(temp_db, tmp_path):
    conn = _seed_voice(temp_db)
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('owner', '我')")
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('sender', '对方')")
    conn.commit()
    conn.close()

    from extractor.exporter import ChatLabExporter

    output = tmp_path / "voice.jsonl"
    ChatLabExporter(conv_name="语音会话").export(str(output))
    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    message = next(line for line in lines if line.get("_type") == "message")
    assert message["content"] == "[语音 2秒] 这是语音转写"


def test_delete_conversation_also_deletes_transcription(temp_db):
    conn = _seed_voice(temp_db)
    conn.close()

    import backend.database as database

    assert database.delete_conversation("c1")["messages_deleted"] == 1
    conn = connect()
    assert conn.execute("SELECT COUNT(*) FROM voice_transcriptions").fetchone()[0] == 0
    conn.close()


def test_delete_conversation_messages_also_deletes_transcription(temp_db):
    conn = _seed_voice(temp_db)
    conn.close()

    import backend.database as database

    assert database.delete_conversation_messages("c1") == 1
    conn = connect()
    assert conn.execute("SELECT COUNT(*) FROM voice_transcriptions").fetchone()[0] == 0
    conn.close()


def test_full_scrape_rollback_restores_transcription(temp_db):
    conn = _seed_voice(temp_db)
    scraper = WebChatScraper(incremental=False)
    scraper._db_conn = conn

    backed_up = scraper._backup_conv_messages("c1")
    conn.execute("DELETE FROM messages WHERE conv_id = ?", ("c1",))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM voice_transcriptions").fetchone()[0] == 0

    scraper._restore_conv_messages_if_empty("c1", backed_up)

    row = conn.execute(
        "SELECT status, text_result FROM voice_transcriptions WHERE msg_id='m1'"
    ).fetchone()
    assert tuple(row) == ("success", "这是语音转写")
    conn.close()


def test_api_scrape_runs_transcription_hook_even_when_no_new_page_rows(temp_db):
    """An incremental run can backfill old voice rows after the fetch is empty."""

    class EmptyApiPage:
        async def evaluate(self, script, arg=None):
            if "window.__imApi.fetchBatch" in script:
                return {"msgs": [], "hasMore": 0, "nextTs": "0"}
            return None

    conn = connect(foreign_keys=True)
    scraper = WebChatScraper(incremental=True)
    scraper._db_conn = conn
    scraper.page = EmptyApiPage()
    calls = []

    async def transcribe(conv_id, short_id):
        calls.append((conv_id, short_id))
        return {"voices": 0, "cached": 0, "requested": 0,
                "succeeded": 0, "failed": 0, "skipped": 0}

    scraper._transcribe_voice_messages = transcribe
    asyncio.run(scraper._api_fetch_all_messages("c1", "short-1", incremental=True))

    assert calls == [("c1", "short-1")]
    conn.close()


def test_api_scrape_classifies_awe_type_zero_voice_before_text(temp_db):
    """The web voice payload uses aweType=0 and must remain msg_type=0."""

    class OneBatchPage:
        async def evaluate(self, script, arg=None):
            if "window.__imApi.fetchBatch" in script:
                return {
                    "msgs": [{
                        "server_id": "6001",
                        "created_at_us": "100",
                        "type_code": 17,
                        "sender_uid": "sender",
                        "sender_sec_uid": "sec-sender",
                        "conv_id": "c1",
                        "content_json": json.dumps({
                            "aweType": 0,
                            "resource_url": {
                                "uri": "voice-uri",
                                "url_list": ["https://cdn.example/voice.mpeg"],
                            },
                            "duration": 2300,
                        }),
                    }],
                    "hasMore": 0,
                    "nextTs": "0",
                }
            return None

    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    conn.commit()
    scraper = WebChatScraper(incremental=False)
    scraper._db_conn = conn
    scraper.page = OneBatchPage()

    async def no_download(_messages):
        return None

    async def no_senders(_sec_by_uid):
        return None

    hook_calls = []

    async def transcribe(conv_id, short_id):
        hook_calls.append((conv_id, short_id))
        return {"voices": 1, "cached": 0, "requested": 0,
                "succeeded": 0, "failed": 0, "skipped": 0}

    scraper._download_voice_files = no_download
    scraper._download_image_files = no_download
    scraper._resolve_sender_identities = no_senders
    scraper._transcribe_voice_messages = transcribe

    asyncio.run(scraper._api_fetch_all_messages("c1", "short-1"))

    row = conn.execute(
        "SELECT msg_type, content, raw_data FROM messages WHERE msg_id='srv_6001'"
    ).fetchone()
    assert row[0:2] == (0, "[语音 2秒]")
    assert json.loads(row[2])["type_code"] == 17
    assert hook_calls == [("c1", "short-1")]
    conn.close()
