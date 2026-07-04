"""Characterization tests for the ChatLab exporter transforms.

Locks the exact output strings/types the exporter produces, so the P4 refactor
(splitting the 240-line export() god-method into pure functions) cannot drift.
"""
import json

from extractor import exporter
from extractor.exporter import (
    ChatLabExporter,
    _build_reply_to,
    _decode_sticker_name,
    _emoji_text_label,
    _render_template_tips,
    _resolve_message,
    _system_message_text,
)
from tests.conftest import insert_conversation, insert_message


# ── _resolve_message branch logic (pure) ──

def _msg(**kw):
    base = {"msg_type": 1, "content": "", "media_url": None, "media_local_path": None}
    base.update(kw)
    return base


def test_resolve_text():
    content, ctype, stats = _resolve_message(_msg(content="hi"), None, "/tmp")
    assert (content, ctype) == ("hi", 0) and stats == {}


def test_resolve_voice_beats_everything():
    cj = {"resource_url": "x", "duration": 2500}
    content, ctype, stats = _resolve_message(_msg(msg_type=0), cj, "/tmp")
    assert content == "[语音 2秒]" and ctype == 0 and stats == {"voice": 1}


def test_resolve_video_type5():
    content, ctype, stats = _resolve_message(_msg(msg_type=5), {"duration": 10}, "/tmp")
    assert content == "[视频 10秒]" and ctype == 0 and stats == {"video": 1}


def test_resolve_image_url():
    content, ctype, stats = _resolve_message(
        _msg(msg_type=3, media_url="https://cdn/x.jpg"), None, "/tmp")
    assert content == "https://cdn/x.jpg" and ctype == 1 and stats == {"image": 1}


def test_resolve_share_overrides_via_itemid():
    cj = {"itemId": "42", "content_title": "T", "content_name": "A"}
    content, ctype, stats = _resolve_message(_msg(msg_type=1, content="{...}"), cj, "/tmp")
    assert ctype == 24 and stats == {"share": 1}
    assert content == "[分享视频] T | @A | https://www.douyin.com/video/42"


def test_build_reply_to():
    assert _build_reply_to(None) is None
    assert _build_reply_to('{"server_id":"1","nickname":"N","content":"C"}') == {
        "replyTo": "srv_1", "replyToAuthor": "N", "replyToContent": "C"}
    assert _build_reply_to("not json") is None


# ── pure helper functions ──

def test_decode_sticker_name_from_hex_url():
    # "haha" -> 68616861
    url = "https://p.douyinpic.com/im-resource/123-ts-68616861.png?x=1"
    assert _decode_sticker_name(url) == "haha"


def test_decode_sticker_name_no_match():
    assert _decode_sticker_name("https://cdn/plain.png") is None
    assert _decode_sticker_name("") is None


def test_emoji_text_label_prefers_decoded_name():
    url = "https://cdn/1-ts-68616861.webp"
    assert _emoji_text_label("whatever", url) == "[haha]"


def test_emoji_text_label_fallbacks():
    assert _emoji_text_label(None, None) == "[表情]"
    assert _emoji_text_label("[续火花]", None) == "[续火花]"
    assert _emoji_text_label("续火花", None) == "[续火花]"


def test_render_template_tips_substitutes_placeholders():
    obj = {"tips": "{{1}}赞了你分享的 {{2}}",
           "template": [{"key": 1, "name": "对方"}, {"key": 2, "name": "视频X"}]}
    assert _render_template_tips(obj) == "对方赞了你分享的 视频X"


def test_system_message_text_variants():
    assert _system_message_text("[语音 3秒]") == "[语音 3秒]"
    tmpl = {"tips": "{{1}}关注了你", "template": [{"key": 1, "name": "小明"}]}
    assert _system_message_text(None, tmpl) == "[系统] 小明关注了你"
    assert _system_message_text("{}", {}) == "[系统消息]"
    assert _system_message_text(None, {"aweType": 193}) == "[通话成功]"


def test_watch_together_not_mislabeled_as_video_call():
    # aweType=9000 "邀你一起看视频" must not fall into the "看视频" -> 视频通话 heuristic
    cj = {"aweType": 9000, "title": "邀你一起看视频", "sub_title": "加入和我一起看"}
    assert _system_message_text(None, cj) == "[一起看视频] 邀你一起看视频"
    assert _system_message_text(None, {"aweType": 9000}) == "[一起看视频]"


# ── full export against a synthetic DB ──

def _raw(cj: dict) -> str:
    return json.dumps({"content_json": json.dumps(cj)}, ensure_ascii=False)


def _export_lines(temp_db, tmp_path, conv_name):
    out = str(tmp_path / "export.jsonl")
    ChatLabExporter(conv_name=conv_name, output_format="jsonl").export(out)
    with open(out, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_full_export_message_transforms(temp_db, tmp_path):
    import extractor.models as models
    conn = models.get_db()
    insert_conversation(conn, "c1", "测试会话",
                        participant_uids='["owner","other"]', last_message_time=100)
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('owner','我方')")
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('other','对方')")

    insert_message(conn, "m1", "c1", 1, sender_uid="owner", content="你好", msg_type=1)
    insert_message(conn, "m2", "c1", 2, sender_uid="other", msg_type=2,
                   media_url="https://cdn/9-ts-68616861.png", content="[表情]")
    insert_message(conn, "m3", "c1", 3, sender_uid="owner", msg_type=3,
                   media_url="https://cdn/pic.jpg")
    share_cj = {"itemId": "7777", "content_title": "标题", "content_name": "作者"}
    insert_message(conn, "m4", "c1", 4, sender_uid="other", msg_type=1,
                   content="漏出来的json", raw_data=_raw(share_cj))
    voice_cj = {"resource_url": "https://cdn/v.mpeg", "duration": 3000}
    insert_message(conn, "m5", "c1", 5, sender_uid="owner", msg_type=0,
                   raw_data=_raw(voice_cj))
    sys_cj = {"tips": "{{1}}赞了你", "template": [{"key": 1, "name": "对方"}]}
    insert_message(conn, "m6", "c1", 6, sender_uid="other", msg_type=0,
                   content="{}", raw_data=_raw(sys_cj))
    video_cj = {"video": {"vid": "v1"}, "duration": 12}
    insert_message(conn, "m7", "c1", 7, sender_uid="owner", msg_type=5,
                   raw_data=_raw(video_cj))
    insert_message(conn, "m8", "c1", 8, sender_uid="other", content="回复内容", msg_type=1,
                   ref_msg=json.dumps({"server_id": "123456789012345",
                                       "nickname": "对方", "content": "原文"}))
    conn.commit()
    conn.close()

    lines = _export_lines(temp_db, tmp_path, "测试会话")
    msgs = {m["platformMessageId"]: m for m in lines if m.get("_type") == "message"}
    header = next(l for l in lines if l.get("_type") == "header")

    assert header["chatlab"]["version"] == "0.0.2"
    assert header["meta"]["ownerId"] == "owner"

    assert msgs["m1"]["type"] == 0 and msgs["m1"]["content"] == "你好"
    assert msgs["m2"]["type"] == 5 and msgs["m2"]["content"] == "[haha]"
    assert msgs["m3"]["type"] == 1 and msgs["m3"]["content"] == "https://cdn/pic.jpg"
    assert msgs["m4"]["type"] == 24
    assert msgs["m4"]["content"] == "[分享视频] 标题 | @作者 | https://www.douyin.com/video/7777"
    assert msgs["m5"]["type"] == 0 and msgs["m5"]["content"] == "[语音 3秒]"
    assert msgs["m6"]["type"] == 0 and msgs["m6"]["content"] == "[系统] 对方赞了你"
    assert msgs["m7"]["type"] == 0 and msgs["m7"]["content"] == "[视频 12秒]"
    assert msgs["m8"]["replyTo"] == {
        "replyTo": "srv_123456789012345",
        "replyToAuthor": "对方",
        "replyToContent": "原文",
    }


def test_export_json_format_shape(temp_db, tmp_path):
    import extractor.models as models
    conn = models.get_db()
    insert_conversation(conn, "c1", "会话J", participant_uids='["owner"]')
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('owner','我')")
    insert_message(conn, "m1", "c1", 1, sender_uid="owner", content="hi", msg_type=1)
    conn.commit()
    conn.close()

    out = str(tmp_path / "e.json")
    ChatLabExporter(conv_name="会话J", output_format="json").export(out)
    data = json.loads(open(out, encoding="utf-8").read())
    assert data["chatlab"]["version"] == "0.0.2"
    assert isinstance(data["members"], list)
    assert data["messages"][0]["content"] == "hi"
