"""Tests for the cookie-authenticated native voice recognition flow."""

import asyncio
import json

import pytest

from common.db import connect, upsert_voice_transcription
from extractor.voice_transcriber import (
    AUDIO_RECOGNITION_API,
    FETCH_RECOGNITION_EVAL_SCRIPT,
    VOICE_RECOGNITION_BATCH_SIZE,
    VOICE_MESSAGE_TYPE,
    VoiceRecognitionRequest,
    VoiceRequestError,
    VoiceTranscriber,
    backfill_sender_sec_uids,
    build_voice_request,
    is_voice_message,
    known_sender_sec_uids,
    pending_voice_rows,
    parse_recognition_response,
)
from tests.conftest import insert_conversation, insert_message


def _voice_content(*, remote_uri="voice-uri", skey="skey", duration=2300):
    return {
        "aweType": 0,
        "duration": duration,
        "resource_url": {
            "uri": remote_uri,
            "url_list": ["https://cdn.example/voice.mpeg"],
            "skey": skey,
        },
    }


def _voice_raw(remote_id, sec_uid="sec-sender", content=None):
    return json.dumps({
        "server_id": remote_id,
        "sender_sec_uid": sec_uid,
        "content_json": json.dumps(content or _voice_content()),
    }, ensure_ascii=False)


def _request(msg_id="m1", message_id="1001"):
    return VoiceRecognitionRequest(
        msg_id=msg_id,
        message_id=message_id,
        uri="voice-uri",
        sec_uid="sec-sender",
        uuid="self-uid",
        skey="skey",
        conv_short_id="123456",
    )


class FakePage:
    """Capture browser-side calls while emulating the endpoint response."""

    def __init__(self, response_factory, self_uuid="self-uid"):
        self.response_factory = response_factory
        self.self_uuid = self_uuid
        self.calls = []

    async def evaluate(self, script, arg=None):
        if "mainOptions" in script:
            return self.self_uuid
        if not arg or not isinstance(arg, list) or not arg or arg[0] != AUDIO_RECOGNITION_API:
            raise AssertionError(f"unexpected page evaluation: {script[:80]}")
        api, payload = arg
        self.calls.append((api, payload))
        return self.response_factory(payload)


def test_build_request_maps_existing_message_fields():
    message = {
        "msg_id": "srv_1001",
        "msg_type": 0,
        "raw_data": _voice_raw("1001"),
    }

    request = build_voice_request(
        message, conv_short_id="123456", self_uuid="self-uid"
    )

    assert request.msg_id == "srv_1001"
    assert request.as_payload() == {
        "uri": "voice-uri",
        "sec_uid": "sec-sender",
        "uuid": "self-uid",
        "message_id": "1001",
        "message_type": VOICE_MESSAGE_TYPE,
        "skey": "skey",
        "conv_short_id": "123456",
    }
    assert "credentials: 'include'" in FETCH_RECOGNITION_EVAL_SCRIPT
    assert "Content-Type': 'application/json'" in FETCH_RECOGNITION_EVAL_SCRIPT


def test_batch_size_matches_live_endpoint_limit():
    assert VOICE_RECOGNITION_BATCH_SIZE == 10


def test_build_request_prefers_canonical_uri_and_reports_missing_fields():
    message = {
        "msg_id": "m1",
        "msg_type": 0,
        "raw_data": _voice_raw("1001", content=_voice_content()),
    }
    assert build_voice_request(
        message, conv_short_id="c", self_uuid="u"
    ).uri == "voice-uri"

    missing = json.loads(_voice_raw("1001"))
    missing["sender_sec_uid"] = ""
    missing["content_json"] = json.dumps({
        "duration": 1,
        "resource_url": {"url_list": ["voice-uri"]},
    })
    with pytest.raises(VoiceRequestError, match="sec_uid|skey"):
        build_voice_request(
            {"msg_id": "m1", "msg_type": 0, "raw_data": json.dumps(missing)},
            conv_short_id="c",
            self_uuid="u",
        )


def test_build_request_keeps_missing_skey_as_an_empty_payload_field():
    message = {
        "msg_id": "m1",
        "msg_type": 0,
        "raw_data": _voice_raw(
            "1001",
            content={
                "duration": 1200,
                "resource_url": {"uri": "voice-uri", "url_list": []},
            },
        ),
    }

    request = build_voice_request(
        message, conv_short_id="short", self_uuid="self-uid"
    )

    assert request.skey == ""
    assert request.as_payload()["skey"] == ""


def test_build_request_always_uses_recognition_message_type_seven():
    message = {
        "msg_id": "m1",
        "msg_type": 0,
        "raw_data": json.dumps({
            "server_id": "1001",
            "sender_sec_uid": "sec-sender",
            "type_code": 17,
            "content_json": json.dumps(_voice_content()),
        }),
    }
    assert build_voice_request(
        message, conv_short_id="short", self_uuid="self-uid"
    ).as_payload()["message_type"] == VOICE_MESSAGE_TYPE
    assert _request().as_payload()["message_type"] == VOICE_MESSAGE_TYPE


def test_build_request_falls_back_to_tkey_for_legacy_voice_payload():
    message = {
        "msg_id": "m1",
        "msg_type": 0,
        "raw_data": _voice_raw(
            "1001",
            content={
                "duration": 1200,
                "tkey": "legacy-voice-key",
                "resource_url": {"url_list": []},
            },
        ),
    }

    request = build_voice_request(
        message, conv_short_id="short", self_uuid="self-uid"
    )

    assert request.uri == "legacy-voice-key"


def test_build_request_accepts_string_resource_url_legacy_payload():
    message = {
        "msg_id": "m1",
        "msg_type": 0,
        "raw_data": _voice_raw(
            "1001",
            content={"duration": 1200, "resource_url": "voice-uri"},
        ),
    }
    assert build_voice_request(
        message, conv_short_id="short", self_uuid="self-uid"
    ).uri == "voice-uri"


def test_image_resource_is_not_treated_as_voice():
    image = {
        "msg_id": "m1",
        "msg_type": 3,
        "raw_data": _voice_raw(
            "1001",
            content={
                "aweType": 2702,
                "duration": 1000,
                "resource_url": {
                    "origin_url_list": ["https://cdn.example/image.jpg"],
                },
            },
        ),
    }
    assert not is_voice_message(image)


def test_parse_response_correlates_out_of_order_items_and_empty_success():
    requests = [_request("m1", "1001"), _request("m2", "1002")]
    response = {
        "status": 200,
        "body": {
            "status_code": 0,
            "data": {
                "res_list": [
                    {"message_id": "1002", "text_result": "第二条", "message": "success"},
                    {"message_id": "1001", "text_result": "", "message": "success"},
                ]
            },
        },
    }

    parsed = parse_recognition_response(response, requests)

    assert parsed["m1"] == {
        "message_id": "1001",
        "status": "success",
        "text_result": "",
        "error": "",
    }
    assert parsed["m2"]["status"] == "success"
    assert parsed["m2"]["text_result"] == "第二条"


def test_parse_response_handles_live_recognition_results_wrapper():
    requests = [_request("m1", "1001"), _request("m2", "1002")]
    response = {
        "status": 200,
        "body": {
            "status_code": 0,
            "status_msg": "",
            "recognition_results": [
                {
                    "message_id": "1002",
                    "text_result": "第二条",
                    "uri": "voice-2",
                    "sec_uid": "sec-sender",
                    "uuid": "self-uid",
                },
                {
                    "message_id": "1001",
                    "text_result": "第一条",
                    "uri": "voice-1",
                    "sec_uid": "sec-sender",
                    "uuid": "self-uid",
                },
            ],
        },
    }

    parsed = parse_recognition_response(response, requests)

    assert parsed["m1"]["status"] == "success"
    assert parsed["m1"]["text_result"] == "第一条"
    assert parsed["m2"]["text_result"] == "第二条"


def test_parse_response_does_not_cache_transport_or_top_level_errors_as_text():
    requests = [_request("m1", "1001"), _request("m2", "1002")]
    responses = [
        {"status": 200, "body": {"error": "unauthorized"}},
        {
            "status": 403,
            "body": {
                "data": [{"message_id": "1001", "text_result": "错误文本"}],
            },
        },
        {
            "status": 200,
            "body": {
                "status_code": 403,
                "status_msg": "登录失效",
                "data": [{"message_id": "1001", "text_result": "错误文本"}],
            },
        },
    ]

    for response in responses:
        parsed = parse_recognition_response(response, requests)
        assert all(item["status"] == "failed" for item in parsed.values())
        assert all(item["text_result"] == "" for item in parsed.values())


def test_parse_response_treats_success_message_metadata_as_non_error():
    parsed = parse_recognition_response(
        {
            "status": 200,
            "body": {
                "status_code": 0,
                "status_msg": "success",
                "data": [{
                    "message_id": "1001",
                    "text_result": "正常文本",
                    "message": "success",
                }],
            },
        },
        [_request("m1", "1001")],
    )

    assert parsed["m1"]["status"] == "success"
    assert parsed["m1"]["text_result"] == "正常文本"


def test_parse_response_accepts_http_like_success_status_in_body():
    parsed = parse_recognition_response(
        {
            "status": 200,
            "body": {
                "status": 200,
                "recognition_results": [{
                    "message_id": "1001",
                    "text_result": "body status 也成功",
                }],
            },
        },
        [_request("m1", "1001")],
    )
    assert parsed["m1"]["status"] == "success"


def test_parse_response_requires_string_text_and_rejects_mixed_unknown_ids():
    requests = [_request("m1", "1001"), _request("m2", "1002")]
    parsed = parse_recognition_response(
        {
            "status": 200,
            "body": {
                "status_code": 0,
                "recognition_results": [
                    {"message_id": "1001", "text_result": None},
                    {"message_id": "unknown", "text_result": "不应错配"},
                ],
            },
        },
        requests,
    )

    assert parsed["m1"]["status"] == "failed"
    assert parsed["m1"]["text_result"] == ""
    assert parsed["m2"]["status"] == "failed"
    assert parsed["m2"]["text_result"] == ""


def test_parse_response_handles_positional_and_http_failures():
    requests = [_request("m1", "1001"), _request("m2", "1002")]
    positional = parse_recognition_response(
        {"status": 200, "body": {"data": [
            {"text_result": "第一条"}, {"text_result": "第二条"}
        ]}},
        requests,
    )
    assert positional["m1"]["text_result"] == "第一条"
    assert positional["m2"]["text_result"] == "第二条"

    failed = parse_recognition_response(
        {"status": 403, "body": {"status_code": 1, "status_msg": "登录失效"}},
        requests,
    )
    assert all(item["status"] == "failed" for item in failed.values())
    assert "登录失效" in failed["m1"]["error"]


def test_transcriber_batches_persists_results_and_reuses_success_cache(temp_db):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    for i in range(5):
        insert_message(
            conn,
            f"m{i}",
            "c1",
            i + 1,
            sender_uid="sender",
            content="[语音]",
            msg_type=0,
            raw_data=_voice_raw(str(1000 + i)),
        )
    upsert_voice_transcription(conn, "m0", "1000", "已缓存", "success", updated_at=1)
    conn.commit()

    def response(payload):
        return {
            "status": 200,
            "body": {
                "status_code": 0,
                "data": [
                    {"message_id": item["message_id"],
                     "text_result": f"转写-{item['message_id']}"}
                    for item in payload
                ],
            },
        }

    page = FakePage(response)
    transcriber = VoiceTranscriber(page, conn, batch_size=2)
    first = asyncio.run(transcriber.transcribe_conversation("c1", "short-1"))

    assert first == {
        "voices": 5,
        "cached": 1,
        "requested": 4,
        "succeeded": 4,
        "failed": 0,
        "skipped": 0,
    }
    assert [len(payload) for _api, payload in page.calls] == [2, 2]
    assert all(api == AUDIO_RECOGNITION_API for api, _payload in page.calls)
    assert conn.execute(
        "SELECT text_result FROM voice_transcriptions WHERE msg_id='m3'"
    ).fetchone()[0] == "转写-1003"

    second = asyncio.run(transcriber.transcribe_conversation("c1", "short-1"))
    assert second["cached"] == 5
    assert second["requested"] == 0
    assert len(page.calls) == 2


def test_transcriber_can_limit_work_to_incremental_message_ids(temp_db):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    for i in range(3):
        insert_message(
            conn, f"m{i}", "c1", i + 1, content="[语音]", msg_type=0,
            raw_data=_voice_raw(str(1500 + i)),
        )
    conn.commit()

    page = FakePage(lambda payload: {
        "status": 200,
        "body": {"data": [{
            "message_id": payload[0]["message_id"],
            "text_result": "只处理本批",
        }]},
    })
    stats = asyncio.run(
        VoiceTranscriber(page, conn).transcribe_conversation(
            "c1", "short-1", message_ids=["m2"]
        )
    )

    assert stats == {
        "voices": 1,
        "cached": 0,
        "requested": 1,
        "succeeded": 1,
        "failed": 0,
        "skipped": 0,
    }
    assert conn.execute(
        "SELECT COUNT(*) FROM voice_transcriptions"
    ).fetchone()[0] == 1
    assert page.calls[0][1][0]["message_id"] == "1502"
    conn.close()


def test_transcriber_retries_failed_item_without_repeating_success(temp_db):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    for i in range(2):
        insert_message(
            conn,
            f"m{i}",
            "c1",
            i + 1,
            content="[语音]",
            msg_type=0,
            raw_data=_voice_raw(str(2000 + i)),
        )
    conn.commit()

    attempts = {"count": 0}

    def response(payload):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return {"status": 200, "body": {"data": [
                {"message_id": "2000", "text_result": "成功"},
                {"message_id": "2001", "error_code": 9, "message": "暂时失败"},
            ]}}
        return {"status": 200, "body": {"data": [
            {"message_id": "2001", "text_result": "重试成功"},
        ]}}

    page = FakePage(response)
    transcriber = VoiceTranscriber(page, conn, batch_size=2)
    first = asyncio.run(transcriber.transcribe_conversation("c1", "short-1", "me"))
    assert (first["succeeded"], first["failed"]) == (1, 1)
    assert conn.execute(
        "SELECT status, error FROM voice_transcriptions WHERE msg_id='m1'"
    ).fetchone()[0:2] == ("failed", "暂时失败")

    second = asyncio.run(transcriber.transcribe_conversation("c1", "short-1", "me"))
    assert (second["requested"], second["succeeded"], second["failed"]) == (1, 1, 0)
    assert page.calls[1][1][0]["message_id"] == "2001"


def test_transcriber_marks_missing_self_uuid_without_call(temp_db):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    insert_message(
        conn,
        "m1",
        "c1",
        1,
        content="[语音]",
        msg_type=0,
        raw_data=_voice_raw("3001"),
    )
    conn.commit()
    page = FakePage(lambda _payload: None, self_uuid="")

    stats = asyncio.run(VoiceTranscriber(page, conn).transcribe_conversation("c1", "short"))

    assert stats["skipped"] == 1
    assert stats["requested"] == 0
    assert page.calls == []
    row = conn.execute(
        "SELECT status, error FROM voice_transcriptions WHERE msg_id='m1'"
    ).fetchone()
    assert row[0] == "skipped"
    assert "uuid" in row[1]


def test_transcriber_does_not_use_participant_uid_as_uuid(temp_db):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话", participant_uids='["owner-uid", "sender"]')
    insert_message(
        conn,
        "m1",
        "c1",
        1,
        content="[语音]",
        msg_type=0,
        raw_data=_voice_raw("4001"),
    )
    conn.commit()

    page = FakePage(lambda _payload: None, self_uuid="")
    stats = asyncio.run(VoiceTranscriber(page, conn).transcribe_conversation("c1", "short"))

    assert stats["skipped"] == 1
    assert stats["requested"] == 0
    assert page.calls == []


def test_transcriber_backfills_legacy_text_typed_voice_row(temp_db):
    """Rows saved by the old aweType=0 classifier remain discoverable."""
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    insert_message(
        conn,
        "m1",
        "c1",
        1,
        content="[语音 2秒]",
        msg_type=1,
        raw_data=_voice_raw("5001"),
    )
    conn.commit()

    page = FakePage(lambda payload: {
        "status": 200,
        "body": {"recognition_results": [{
            "message_id": payload[0]["message_id"],
            "text_result": "旧记录也能识别",
        }]},
    })
    stats = asyncio.run(
        VoiceTranscriber(page, conn).transcribe_conversation("c1", "short")
    )

    assert stats["voices"] == 1
    assert stats["succeeded"] == 1
    assert conn.execute(
        "SELECT text_result FROM voice_transcriptions WHERE msg_id='m1'"
    ).fetchone()[0] == "旧记录也能识别"


def test_pending_voice_rows_filter_local_db_candidates(temp_db):
    """Historical backfill starts from DB voice candidates, not every message."""
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "语音会话")
    insert_message(
        conn, "voice-pending", "c1", 1, content="[语音]", msg_type=0,
        raw_data=_voice_raw("7001"),
    )
    insert_message(
        conn, "voice-done", "c1", 2, content="[语音]", msg_type=0,
        raw_data=_voice_raw("7002"),
    )
    upsert_voice_transcription(conn, "voice-done", "7002", "已完成", "success")
    insert_message(
        conn, "image", "c1", 3, content="[图片]", msg_type=3,
        raw_data=json.dumps({"content_json": json.dumps({
            "resource_url": {"origin_url_list": ["image"]},
        })}),
    )
    conn.commit()

    rows = pending_voice_rows(conn)

    assert [row["msg_id"] for row in rows] == ["voice-pending"]
    conn.close()


def test_backfill_sender_sec_uids_uses_unique_local_sender_mapping(temp_db):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    insert_message(
        conn,
        "known",
        "c1",
        1,
        sender_uid="sender",
        content="普通消息",
        msg_type=1,
        raw_data=json.dumps({"sender_sec_uid": "sec-sender"}),
    )
    insert_message(
        conn,
        "voice",
        "c1",
        2,
        sender_uid="sender",
        content="[语音]",
        msg_type=0,
        raw_data=_voice_raw("7003", sec_uid=""),
    )
    conn.commit()

    rows = conn.execute("SELECT * FROM messages WHERE msg_id = 'voice'").fetchall()
    unique, ambiguous = known_sender_sec_uids(conn)
    assert unique == {"sender": "sec-sender"}
    assert ambiguous == set()

    enriched, stats = backfill_sender_sec_uids(conn, rows)

    assert stats == {
        "checked": 1,
        "missing": 1,
        "mapped_sender_uids": 1,
        "updated": 1,
        "unresolved": 0,
        "ambiguous": 0,
    }
    assert enriched[0]["sender_sec_uid"] == "sec-sender"
    stored = json.loads(
        conn.execute(
            "SELECT raw_data FROM messages WHERE msg_id = 'voice'"
        ).fetchone()[0]
    )
    assert stored["sender_sec_uid"] == "sec-sender"
    conn.close()


def test_backfill_sender_sec_uids_leaves_ambiguous_mapping_unresolved(temp_db):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    for seq, sec_uid in ((1, "sec-a"), (2, "sec-b")):
        insert_message(
            conn,
            f"known-{seq}",
            "c1",
            seq,
            sender_uid="sender",
            content="普通消息",
            msg_type=1,
            raw_data=json.dumps({"sender_sec_uid": sec_uid}),
        )
    insert_message(
        conn,
        "voice",
        "c1",
        3,
        sender_uid="sender",
        content="[语音]",
        msg_type=0,
        raw_data=_voice_raw("7004", sec_uid=""),
    )
    conn.commit()

    rows = conn.execute("SELECT * FROM messages WHERE msg_id = 'voice'").fetchall()
    enriched, stats = backfill_sender_sec_uids(conn, rows)

    assert stats["missing"] == 1
    assert stats["mapped_sender_uids"] == 0
    assert stats["updated"] == 0
    assert stats["unresolved"] == 1
    assert stats["ambiguous"] == 1
    assert "sender_sec_uid" not in enriched[0]
    conn.close()


def test_backfill_sender_sec_uids_handles_multiple_group_senders(temp_db):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "group", "群聊")
    for seq, sender_uid, sec_uid in (
        (1, "sender-a", "sec-a"),
        (2, "sender-b", "sec-b"),
    ):
        insert_message(
            conn,
            f"known-{seq}",
            "group",
            seq,
            sender_uid=sender_uid,
            content="普通消息",
            msg_type=1,
            raw_data=json.dumps({"sender_sec_uid": sec_uid}),
        )
    for seq, sender_uid, remote_id in (
        (3, "sender-a", "7101"),
        (4, "sender-b", "7102"),
    ):
        insert_message(
            conn,
            f"voice-{seq}",
            "group",
            seq,
            sender_uid=sender_uid,
            content="[语音]",
            msg_type=0,
            raw_data=_voice_raw(remote_id, sec_uid=""),
        )
    conn.commit()

    rows = conn.execute(
        "SELECT * FROM messages WHERE msg_id LIKE 'voice-%' ORDER BY seq"
    ).fetchall()
    enriched, stats = backfill_sender_sec_uids(conn, rows)

    assert stats["mapped_sender_uids"] == 2
    assert stats["updated"] == 2
    assert [row["sender_sec_uid"] for row in enriched] == ["sec-a", "sec-b"]
    conn.close()
