import asyncio
import json

from common.db import connect
from extractor.message_types import (
    is_merged_forward_payload,
    is_video_note_payload,
    message_type_code,
)
from extractor.web_scraper import WebChatScraper
from tests.conftest import insert_conversation


def test_video_note_with_generic_awe_type_is_detected_without_poster():
    payload = {"aweType": 0, "video": {"vid": "v123"}, "duration": 6.4}

    assert is_video_note_payload(payload)
    assert not is_merged_forward_payload(payload)
    assert message_type_code("video") == 5


def test_merged_forward_payload_is_detected_by_primary_and_legacy_shapes():
    assert is_merged_forward_payload({"aweType": 13600, "title": "聊天记录"})
    assert is_merged_forward_payload({"aweType": 0, "msg_ids": [{"msg_id": "1"}]})
    assert message_type_code("merged_forward") == 6


def test_plain_text_and_voice_are_not_misclassified():
    assert not is_video_note_payload({"aweType": 0, "text": "hello"})
    assert not is_merged_forward_payload({"aweType": 0, "text": "hello"})
    assert not is_video_note_payload(
        {"aweType": 0, "resource_url": {"url_list": ["voice"]}, "duration": 1200}
    )


def test_api_scrape_persists_video_and_merged_forward_types(temp_db):
    class OneBatchPage:
        async def evaluate(self, script, arg=None):
            if "window.__imApi.fetchBatch" not in script:
                return None
            return {
                "msgs": [
                    {
                        "server_id": "7001", "created_at_us": "1", "conv_id": "c1",
                        "sender_uid": "u1", "content_json": json.dumps(
                            {"aweType": 0, "video": {"vid": "v1"}, "duration": 8}
                        ),
                    },
                    {
                        "server_id": "7002", "created_at_us": "2", "conv_id": "c1",
                        "sender_uid": "u1", "content_json": json.dumps(
                            {"aweType": 13600, "title": "群聊记录", "msg_ids": []}
                        ),
                    },
                ],
                "hasMore": 0,
                "nextTs": "0",
            }

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

    scraper._download_voice_files = no_download
    scraper._download_image_files = no_download
    scraper._resolve_sender_identities = no_senders

    asyncio.run(scraper._api_fetch_all_messages("c1", "short-1"))

    rows = conn.execute(
        "SELECT msg_id, msg_type, content FROM messages ORDER BY seq"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("srv_7001", 5, "[视频 8秒]"),
        ("srv_7002", 6, "群聊记录"),
    ]
    conn.close()
