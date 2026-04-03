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

OWNER_UID = "4169768138712989"
OWNER_NAME = "TeamBreaker"


def _file_to_data_url(filepath: str) -> str | None:
    """Read a local file and return a data URL (base64 encoded)."""
    if not filepath or not os.path.isfile(filepath):
        return None

    mime, _ = mimetypes.guess_type(filepath)
    if not mime:
        ext = os.path.splitext(filepath)[1].lower()
        mime = {
            ".webp": "image/webp",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
        }.get(ext, "application/octet-stream")

    try:
        with open(filepath, "rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


class ChatLabExporter:
    def __init__(self, conv_name: str = None, output_format: str = "jsonl"):
        self.conv_name = conv_name
        self.output_format = output_format  # "json" or "jsonl"

    def export(self, output_path: str):
        conn = get_db()

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

        # Load messages ordered by timestamp
        messages = conn.execute(
            "SELECT * FROM messages WHERE conv_id = ? ORDER BY seq ASC",
            (conv_id,),
        ).fetchall()

        print(f"[*] 共 {len(messages)} 条消息")

        # Collect members
        members_map = {}
        for msg in messages:
            raw_name = msg["sender_name"] or ""
            if raw_name == "__self__":
                uid, name = OWNER_UID, OWNER_NAME
            else:
                uid = msg["sender_uid"] or ""
                name = raw_name if raw_name else conv_name
            if uid and uid not in members_map:
                members_map[uid] = name

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
                "ownerId": OWNER_UID,
            },
        }

        members = []
        for uid, name in members_map.items():
            member = {"platformId": uid, "accountName": name}
            members.append(member)

        chatlab_messages = []
        image_count = 0
        image_embedded = 0

        for msg in messages:
            chatlab_type = CHATLAB_TYPE_MAP.get(msg["msg_type"], 99)
            content = msg["content"]

            # 发送方：__self__ → OWNER_NAME，空/无名 → 会话名（对方）
            sender_name_raw = msg["sender_name"] or ""
            if sender_name_raw == "__self__":
                display_name = OWNER_NAME
                sender_id = OWNER_UID
            else:
                display_name = sender_name_raw if sender_name_raw else conv_name
                sender_id = msg["sender_uid"] or ""

            # 图片/表情：优先用 media_url（网络 URL），避免 base64 膨胀文件体积
            if chatlab_type in (1, 5):
                if msg["media_url"]:
                    content = msg["media_url"]
                    image_count += 1
                elif msg["media_local_path"] and os.path.isfile(msg["media_local_path"]):
                    data_url = _file_to_data_url(msg["media_local_path"])
                    if data_url:
                        content = data_url
                        image_embedded += 1
                    else:
                        chatlab_type = 0
                    image_count += 1

            chatlab_msg = {
                "sender": sender_id,
                "accountName": display_name,
                "timestamp": msg["timestamp"] or 0,
                "type": chatlab_type,
                "content": content,
                "platformMessageId": msg["msg_id"],
            }
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
            print(f"  图片: {image_count} (嵌入 data URL: {image_embedded})")
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"  文件大小: {size_mb:.1f} MB")
