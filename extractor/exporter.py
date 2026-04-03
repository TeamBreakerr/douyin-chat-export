#!/usr/bin/env python3
"""Export chat data from SQLite to ChatLab v0.0.2 format (JSON/JSONL)."""
import base64
import json
import mimetypes
import os
import time

from extractor.models import get_db

# DB msg_type → ChatLab message type
CHATLAB_TYPE_MAP = {
    1: 0,   # text → TEXT
    2: 5,   # emoji → EMOJI
    3: 1,   # image → IMAGE
    4: 24,  # share → SHARE
    0: 99,  # other → OTHER
}


def _file_to_data_url(filepath: str) -> str | None:
    """Read a local file and return a data URL (base64 encoded)."""
    if not filepath or not os.path.isfile(filepath):
        return None

    ext = os.path.splitext(filepath)[1].lower()
    # 优先使用自定义映射（mimetypes 会把 .mpeg 识别为 video/mpeg）
    mime = {
            ".webp": "image/webp",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".mpeg": "audio/mpeg",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
    }.get(ext)
    if not mime:
        mime, _ = mimetypes.guess_type(filepath)
    if not mime:
        mime = "application/octet-stream"

    try:
        with open(filepath, "rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


def _detect_owner(conn) -> tuple[str, str]:
    """从数据库推断 owner。

    策略：
    1. participant_uids 中第一个 uid（提取时 curLoginUserInfo 排第一）
    2. 回退：出现在最多不同会话中的 sender_uid
    """
    # 策略 1: 从 participant_uids 取第一个 uid
    row = conn.execute(
        "SELECT participant_uids FROM conversations WHERE participant_uids != '[]' LIMIT 1"
    ).fetchone()
    if row:
        try:
            uids = json.loads(row[0])
            if uids:
                owner_uid = uids[0]
                user = conn.execute(
                    "SELECT nickname FROM users WHERE uid = ?", (owner_uid,)
                ).fetchone()
                owner_name = user[0] if user and user[0] else "我"
                return owner_uid, owner_name
        except (json.JSONDecodeError, IndexError):
            pass

    # 策略 2: 出现在最多会话中的 sender_uid
    rows = conn.execute("""
        SELECT sender_uid, COUNT(DISTINCT conv_id) as conv_count
        FROM messages WHERE sender_uid != ''
        GROUP BY sender_uid ORDER BY conv_count DESC LIMIT 1
    """).fetchall()
    if rows:
        owner_uid = rows[0][0]
        user = conn.execute(
            "SELECT nickname FROM users WHERE uid = ?", (owner_uid,)
        ).fetchone()
        owner_name = user[0] if user and user[0] else "我"
        return owner_uid, owner_name

    return "", "我"


def _get_content_json(msg) -> dict | None:
    """从 raw_data 中提取完整的 content_json。"""
    raw = msg["raw_data"]
    if not raw:
        return None
    try:
        raw_obj = json.loads(raw) if isinstance(raw, str) else raw
        cj_str = raw_obj.get("content_json", "")
        if cj_str:
            return json.loads(cj_str) if isinstance(cj_str, str) else cj_str
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


class ChatLabExporter:
    def __init__(self, conv_name: str = None, output_format: str = "jsonl"):
        self.conv_name = conv_name
        self.output_format = output_format  # "json" or "jsonl"

    def export(self, output_path: str):
        conn = get_db()

        # Detect owner
        owner_uid, owner_name = _detect_owner(conn)
        print(f"[*] 检测到 owner: {owner_name} ({owner_uid})")

        # Find conversation
        if self.conv_name:
            row = conn.execute(
                "SELECT conv_id, name FROM conversations WHERE name LIKE ?",
                (f"%{self.conv_name}%",),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT conv_id, name FROM conversations ORDER BY last_message_time DESC LIMIT 1"
            ).fetchone()

        if not row:
            print(f"[-] 未找到会话: {self.conv_name or '(any)'}")
            conn.close()
            return

        conv_id = row["conv_id"]
        conv_name = row["name"]
        print(f"[*] 导出会话: {conv_name} (ID: {conv_id})")

        # Load messages ordered by seq
        messages = conn.execute(
            "SELECT * FROM messages WHERE conv_id = ? ORDER BY seq ASC",
            (conv_id,),
        ).fetchall()

        print(f"[*] 共 {len(messages)} 条消息")

        # Build users map from DB
        users_map = {}
        users_rows = conn.execute("SELECT uid, nickname FROM users").fetchall()
        for u in users_rows:
            if u["uid"] and u["nickname"]:
                users_map[u["uid"]] = u["nickname"]

        # Collect members from messages
        members_map = {}
        for msg in messages:
            uid = msg["sender_uid"] or ""
            if uid and uid not in members_map:
                name = users_map.get(uid, "")
                if not name:
                    name = owner_name if uid == owner_uid else conv_name
                members_map[uid] = name

        # Media base dir
        media_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media")

        # Build ChatLab structure
        header = {
            "chatlab": {
                "version": "0.0.2",
                "exportedAt": int(time.time()),
                "generator": "douyin-chat-export",
            },
            "meta": {
                "name": f"与{conv_name}的对话",
                "platform": "douyin",
                "type": "private",
                "ownerId": owner_uid,
            },
        }

        members = []
        for uid, name in members_map.items():
            member = {"platformId": uid, "accountName": name}
            members.append(member)

        chatlab_messages = []
        image_count = 0
        image_embedded = 0
        voice_count = 0
        ref_count = 0

        for msg in messages:
            chatlab_type = CHATLAB_TYPE_MAP.get(msg["msg_type"], 99)
            content = msg["content"]
            cj = _get_content_json(msg)

            # 发送方：从 users_map 获取昵称
            uid = msg["sender_uid"] or ""
            display_name = users_map.get(uid, "")
            if not display_name:
                display_name = owner_name if uid == owner_uid else conv_name

            # 语音消息：msg_type=0 但有 resource_url + duration
            is_voice = False
            if cj and cj.get("resource_url") and cj.get("duration"):
                is_voice = True
                chatlab_type = 3  # AUDIO
                dur_sec = round(cj["duration"] / 1000)

                # 优先本地文件
                local_path = msg["media_local_path"]
                if local_path:
                    full_path = os.path.join(media_dir, local_path)
                    data_url = _file_to_data_url(full_path)
                    if data_url:
                        content = data_url
                        voice_count += 1
                    else:
                        # 本地文件不存在，用 CDN URL
                        urls = cj["resource_url"].get("url_list", [])
                        content = urls[0] if urls else f"[语音 {dur_sec}秒]"
                elif cj["resource_url"].get("url_list"):
                    content = cj["resource_url"]["url_list"][0]
                else:
                    content = f"[语音 {dur_sec}秒]"

            # 图片/表情：优先 CDN URL
            if not is_voice and chatlab_type in (1, 5):
                if msg["media_url"]:
                    content = msg["media_url"]
                    image_count += 1
                elif msg["media_local_path"] and os.path.isfile(
                    os.path.join(media_dir, msg["media_local_path"])
                ):
                    data_url = _file_to_data_url(
                        os.path.join(media_dir, msg["media_local_path"])
                    )
                    if data_url:
                        content = data_url
                        image_embedded += 1
                    else:
                        chatlab_type = 0
                    image_count += 1

            # 分享消息：附加 URL
            if chatlab_type == 24 and cj:
                item_id = cj.get("itemId", "")
                title = cj.get("content_title", "")
                author = cj.get("content_name", "")
                parts = []
                if title:
                    parts.append(title)
                if author:
                    parts.append(f"@{author}")
                if item_id:
                    parts.append(f"https://www.douyin.com/video/{item_id}")
                if parts:
                    content = " | ".join(parts)

            chatlab_msg = {
                "sender": uid,
                "accountName": display_name,
                "timestamp": msg["timestamp"] or 0,
                "type": chatlab_type,
                "content": content,
                "platformMessageId": msg["msg_id"],
            }

            # 引用/回复消息
            if msg["ref_msg"]:
                try:
                    ref = json.loads(msg["ref_msg"]) if isinstance(msg["ref_msg"], str) else msg["ref_msg"]
                    ref_info = {}
                    if ref.get("server_id"):
                        ref_info["replyTo"] = f"srv_{ref['server_id']}"
                    if ref.get("nickname"):
                        ref_info["replyToAuthor"] = ref["nickname"]
                    if ref.get("content"):
                        ref_info["replyToContent"] = ref["content"]
                    if ref_info:
                        chatlab_msg["replyTo"] = ref_info
                        ref_count += 1
                except (json.JSONDecodeError, TypeError):
                    pass

            chatlab_messages.append(chatlab_msg)

        conn.close()

        # Write output
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        if self.output_format == "json":
            output = {**header, "members": members, "messages": chatlab_messages}
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False)
            print(f"[+] JSON 导出完成: {output_path}")
        else:
            # JSONL format
            with open(output_path, "w", encoding="utf-8") as f:
                # Header line
                header_line = {"_type": "header", **header}
                f.write(json.dumps(header_line, ensure_ascii=False) + "\n")
                # Member lines
                for member in members:
                    member_line = {"_type": "member", **member}
                    f.write(json.dumps(member_line, ensure_ascii=False) + "\n")
                # Message lines
                for msg in chatlab_messages:
                    msg_line = {"_type": "message", **msg}
                    f.write(json.dumps(msg_line, ensure_ascii=False) + "\n")
            print(f"[+] JSONL 导出完成: {output_path}")

        print(f"  消息: {len(chatlab_messages)}")
        print(f"  成员: {len(members)}")
        if image_count:
            print(f"  图片/表情: {image_count} (嵌入 data URL: {image_embedded})")
        if voice_count:
            print(f"  语音: {voice_count} (嵌入 data URL)")
        if ref_count:
            print(f"  引用/回复: {ref_count}")
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"  文件大小: {size_mb:.1f} MB")
