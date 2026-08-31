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


def test_full_scrape_reuses_successful_transcription_after_refetch(temp_db):
    """Replacing a message must not discard a successful recognition cache."""

    class ReplayedVoicePage:
        def __init__(self):
            self.recognition_calls = 0

        async def evaluate(self, script, arg=None):
            if "window.__imApi.fetchBatch" in script:
                return {
                    "msgs": [{
                        "server_id": "5001",
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
            if isinstance(arg, list) and arg and "/audio/recognition/" in arg[0]:
                self.recognition_calls += 1
                return {
                    "status": 200,
                    "body": {"recognition_results": [{
                        "message_id": "5001",
                        "text_result": "不应重新识别",
                    }]},
                }
            if "mainOptions" in script:
                return "owner"
            return None

    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "语音会话")
    insert_message(
        conn,
        "srv_5001",
        "c1",
        1,
        sender_uid="sender",
        content="[语音 2秒]",
        msg_type=0,
        raw_data=_voice_raw("5001"),
    )
    upsert_voice_transcription(
        conn, "srv_5001", "5001", "已缓存的转写", "success", updated_at=7
    )
    conn.commit()

    scraper = WebChatScraper(incremental=False)
    scraper._db_conn = conn
    scraper.page = ReplayedVoicePage()

    async def no_download(_messages):
        return None

    async def no_senders(_sec_by_uid):
        return None

    scraper._download_voice_files = no_download
    scraper._download_image_files = no_download
    scraper._resolve_sender_identities = no_senders

    backed_up = scraper._backup_conv_messages("c1")
    conn.execute("DELETE FROM messages WHERE conv_id = ?", ("c1",))
    conn.commit()

    asyncio.run(scraper._api_fetch_all_messages("c1", "short-1"))
    scraper._restore_conv_messages_if_empty("c1", backed_up)

    row = conn.execute(
        "SELECT status, text_result FROM voice_transcriptions WHERE msg_id='srv_5001'"
    ).fetchone()
    assert tuple(row) == ("success", "已缓存的转写")
    assert scraper.page.recognition_calls == 0
    conn.close()


def test_api_scrape_skips_transcription_when_no_new_page_rows(temp_db):
    """An empty incremental fetch does not rescan historical messages."""

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

    async def transcribe(*args, **kwargs):
        calls.append((args, kwargs))
        return {"voices": 0, "cached": 0, "requested": 0,
                "succeeded": 0, "failed": 0, "skipped": 0}

    scraper._transcribe_voice_messages = transcribe
    asyncio.run(scraper._api_fetch_all_messages("c1", "short-1", incremental=True))

    assert calls == []
    conn.close()


def test_incremental_scrape_transcribes_only_newly_inserted_voices(temp_db):
    """Old voices seen while catching up stay with the history backfill task."""

    def api_voice(server_id):
        return {
            "server_id": server_id,
            "created_at_us": server_id,
            "type_code": 17,
            "sender_uid": "sender",
            "sender_sec_uid": "sec-sender",
            "conv_id": "c1",
            "content_json": json.dumps({
                "aweType": 0,
                "resource_url": {"uri": f"voice-{server_id}"},
                "duration": 1000,
            }),
        }

    class MixedBatchPage:
        async def evaluate(self, script, arg=None):
            if "window.__imApi.fetchBatch" in script:
                return {
                    "msgs": [api_voice("6102"), api_voice("6101")],
                    "hasMore": 0,
                    "nextTs": "0",
                }
            return None

    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    insert_message(
        conn, "srv_6101", "c1", 1, content="[语音 1秒]", msg_type=0,
        sender_uid="sender", raw_data=_voice_raw("6101"),
    )
    conn.commit()

    scraper = WebChatScraper(incremental=True)
    scraper._db_conn = conn
    scraper.page = MixedBatchPage()

    async def no_download(_messages):
        return None

    async def no_senders(_sec_by_uid):
        return None

    calls = []

    async def transcribe(conv_id, short_id, *, message_ids=None):
        calls.append((conv_id, short_id, message_ids))
        return {"voices": 1, "cached": 0, "requested": 1,
                "succeeded": 1, "failed": 0, "skipped": 0}

    scraper._download_voice_files = no_download
    scraper._download_image_files = no_download
    scraper._resolve_sender_identities = no_senders
    scraper._transcribe_voice_messages = transcribe

    asyncio.run(
        scraper._api_fetch_all_messages("c1", "short-1", incremental=True)
    )

    assert calls == [("c1", "short-1", ["srv_6102"])]
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

    async def transcribe(conv_id, short_id, *, message_ids=None):
        hook_calls.append((conv_id, short_id, message_ids))
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
    assert hook_calls == [("c1", "short-1", ["srv_6001"])]
    conn.close()


def test_history_backfill_uses_local_voice_candidates_without_message_fetch(
    temp_db, monkeypatch
):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "123456", "语音会话")
    insert_message(
        conn, "voice", "123456", 1, content="[语音]", msg_type=0,
        sender_uid="sender",
        raw_data=_voice_raw("8001", sec_uid=""),
    )
    insert_message(
        conn,
        "text",
        "123456",
        2,
        sender_uid="sender",
        content="普通消息",
        msg_type=1,
        raw_data=json.dumps({"sender_sec_uid": "sec-sender"}),
    )
    conn.commit()

    class BackfillPage:
        async def evaluate(self, script, arg=None):
            if "mainOptions" in script:
                return "self-uid"
            assert isinstance(arg, list)
            payload = arg[1][0]
            return {
                "status": 200,
                "body": {"recognition_results": [{
                    "message_id": payload["message_id"],
                    "text_result": "历史补充完成",
                }]},
            }

    async def no_navigation():
        return None

    async def no_sleep(_seconds):
        return None

    scraper = WebChatScraper()
    scraper._db_conn = conn
    scraper.page = BackfillPage()
    scraper.navigate_to_chat = no_navigation
    monkeypatch.setattr("extractor.web_scraper.asyncio.sleep", no_sleep)

    stats = asyncio.run(scraper.backfill_voice_transcriptions())

    assert stats["voices"] == 1
    assert stats["succeeded"] == 1
    assert conn.execute(
        "SELECT text_result FROM voice_transcriptions WHERE msg_id='voice'"
    ).fetchone()[0] == "历史补充完成"
    stored_raw = json.loads(
        conn.execute(
            "SELECT raw_data FROM messages WHERE msg_id='voice'"
        ).fetchone()[0]
    )
    assert stored_raw["sender_sec_uid"] == "sec-sender"
    assert conn.execute(
        "SELECT COUNT(*) FROM voice_transcriptions WHERE msg_id='text'"
    ).fetchone()[0] == 0
    conn.close()


def test_sec_uid_lookup_stops_after_target_senders_are_found(temp_db):
    class LookupPage:
        def __init__(self):
            self.lookup_calls = 0

        async def evaluate(self, script, arg=None):
            if "window.__imApi.fetchBatch" in script:
                self.lookup_calls += 1
                return {
                    "msgs": [
                        {"conv_id": "123456", "sender_uid": "sender-a", "sender_sec_uid": "sec-a"},
                        {"conv_id": "123456", "sender_uid": "sender-b", "sender_sec_uid": "sec-b"},
                    ],
                    "hasMore": 1,
                    "nextTs": "900",
                }
            return None

    page = LookupPage()
    scraper = WebChatScraper()
    scraper.page = page
    mapping, stats = asyncio.run(
        scraper._lookup_sender_sec_uids(
            "123456", "123456", {"sender-a", "sender-b"}
        )
    )

    assert mapping == {"sender-a": "sec-a", "sender-b": "sec-b"}
    assert stats == {"target_senders": 2, "pages": 1, "matched": 2}
    assert page.lookup_calls == 1


def test_history_backfill_remote_lookup_only_fetches_sender_identity(
    temp_db, monkeypatch
):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "123456", "语音会话")
    insert_message(
        conn,
        "voice",
        "123456",
        1,
        sender_uid="sender",
        content="[语音]",
        msg_type=0,
        raw_data=_voice_raw("8002", sec_uid=""),
    )
    conn.commit()

    class BackfillPage:
        def __init__(self):
            self.lookup_calls = 0
            self.recognition_payloads = []

        async def evaluate(self, script, arg=None):
            if "window.__imApi.fetchBatch" in script:
                self.lookup_calls += 1
                return {
                    "msgs": [{
                        "conv_id": "123456",
                        "sender_uid": "sender",
                        "sender_sec_uid": "sec-remote",
                    }],
                    "hasMore": 1,
                    "nextTs": "900",
                }
            if "mainOptions" in script:
                return "self-uid"
            if isinstance(arg, list) and arg and "/audio/recognition/" in arg[0]:
                self.recognition_payloads.extend(arg[1])
                return {
                    "status": 200,
                    "body": {"recognition_results": [{
                        "message_id": arg[1][0]["message_id"],
                        "text_result": "远程补充完成",
                    }]},
                }
            return None

    async def no_navigation():
        return None

    async def no_sleep(_seconds):
        return None

    page = BackfillPage()
    scraper = WebChatScraper()
    scraper._db_conn = conn
    scraper.page = page
    scraper.navigate_to_chat = no_navigation
    monkeypatch.setattr("extractor.web_scraper.asyncio.sleep", no_sleep)

    stats = asyncio.run(scraper.backfill_voice_transcriptions())

    assert page.lookup_calls == 1
    assert page.recognition_payloads[0]["sec_uid"] == "sec-remote"
    assert stats["succeeded"] == 1
    stored_raw = json.loads(
        conn.execute("SELECT raw_data FROM messages WHERE msg_id='voice'").fetchone()[0]
    )
    assert stored_raw["sender_sec_uid"] == "sec-remote"
    conn.close()
