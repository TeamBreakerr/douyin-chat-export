#!/usr/bin/env python3
"""Export chat data from SQLite to ChatLab v0.0.2 format (JSON/JSONL)."""
import base64
import json
import mimetypes
import os
import re
import time
import urllib.parse

from common import paths
from extractor.asr import ASRResult, QwenASRClient
from extractor.models import get_db
from extractor.voice_recognition import (
    ensure_transcription_table,
    load_cached_transcription,
    save_cached_transcription,
)

# DB msg_type → ChatLab message type
CHATLAB_TYPE_MAP = {
    1: 0,   # text → TEXT
    2: 5,   # emoji → EMOJI
    3: 1,   # image → IMAGE
    4: 24,  # share → SHARE
    5: 1,   # video → IMAGE (只有 poster，没有真视频流)
    0: 99,  # other → OTHER
}


_STICKER_HEX_RE = re.compile(r"-ts-([0-9a-fA-F]{4,})(?:\.[a-zA-Z0-9]{1,5})?$")


def _decode_sticker_name(url: str) -> str | None:
    """Recover a sticker's human-readable name from a Douyin IM CDN URL.

    URLs look like .../im-resource/<digits>-ts-<utf8-hex>?...
    where <utf8-hex> is the UTF-8 bytes of e.g. "续火花.png" as hex.
    Returns the decoded name without extension, or None on no match.
    """
    if not url:
        return None
    try:
        path = urllib.parse.urlparse(url).path
        last = path.rsplit("/", 1)[-1]
        m = _STICKER_HEX_RE.search(last)
        if not m:
            return None
        raw = bytes.fromhex(m.group(1))
        name = raw.decode("utf-8")
        # Strip a trailing extension like .png/.webp/.gif
        base, sep, ext = name.rpartition(".")
        if sep and base and len(ext) <= 4 and ext.isalnum():
            return base
        return name
    except (UnicodeDecodeError, ValueError):
        return None


def _emoji_text_label(content: str | None, media_url: str | None) -> str:
    """Pick a text label '[name]' for an emoji message.
    Prefers the URL-decoded name (most reliable), then the existing content,
    finally a generic placeholder.
    """
    name = _decode_sticker_name(media_url or "")
    if name:
        return f"[{name}]"
    c = (content or "").strip()
    if c and c != "[表情]":
        if c.startswith("[") and c.endswith("]"):
            return c
        return f"[{c}]"
    return "[表情]"


_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{\d+\}\}")


def _render_template_tips(obj: dict) -> str | None:
    """渲染抖音系统消息模板。
    例：{"tips":"{{1}}赞了你分享的 {{2}}","template":[{"key":1,"name":"对方"},{"key":2,"name":"视频X"}]}
    → "对方赞了你分享的 视频X"
    """
    tips = obj.get("tips") or obj.get("hint") or obj.get("title")
    if not tips:
        return None
    names = {}
    for it in obj.get("template") or []:
        if isinstance(it, dict) and it.get("key") is not None:
            names[it["key"]] = (it.get("name") or "").strip()
    out = tips
    for k, name in names.items():
        out = out.replace(f"{{{{{k}}}}}", name)
    out = _TEMPLATE_PLACEHOLDER_RE.sub("", out).strip()
    return out or None


def _system_message_text(content: str | None, cj: dict | None = None) -> str:
    """msg_type=0 系统消息的可读文本。
    - 文本（已是 [语音 X秒]）原样返回
    - JSON 模板渲染成 [系统] 前缀的可读文字（优先用完整 cj，content 在 DB 里被截 200 字符）
    - 无法识别的兜底为 [系统消息]
    """
    c = (content or "").strip()
    if c and not c.startswith("{"):
        return c
    obj = cj if isinstance(cj, dict) else None
    if not obj and c.startswith("{"):
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            obj = None
    if not obj:
        return "[系统消息]"
    # 一起看视频邀请 (aweType=9000) 优先处理：它带 title="邀你一起看视频"，
    # 会被 _render_template_tips 的 title 兜底 + 下面的 "看视频" 启发式双重误判。
    if obj.get("aweType") == 9000:
        title = (obj.get("title") or "").strip()
        return f"[一起看视频] {title}".strip() if title else "[一起看视频]"
    rendered = _render_template_tips(obj)
    if rendered:
        return f"[系统] {rendered}"
    if obj.get("aweType") == 193 or obj.get("tips") == "通话成功":
        return "[通话成功]"
    title = obj.get("title") or ""
    hint = obj.get("hint") or ""
    if "看视频" in title or "通话邀请" in hint:
        return "[视频通话邀请]"
    return "[系统消息]"


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


def _is_voice_message(cj: dict | None) -> bool:
    """Return whether a content_json object represents a Douyin voice message."""
    return bool(cj and cj.get("resource_url") and cj.get("duration"))


def _voice_label(cj: dict | None) -> str:
    try:
        dur_sec = round(float((cj or {}).get("duration") or 0) / 1000)
    except (TypeError, ValueError):
        dur_sec = 0
    return f"[语音 {dur_sec}秒]" if dur_sec else "[语音]"


def _voice_content(cj: dict | None, transcription: ASRResult | None) -> str:
    """Combine the stable voice label with an optional ASR result."""
    if not transcription or not transcription.text.strip():
        return _voice_label(cj)
    emotion = (
        f"[情绪: {transcription.emotion}] " if transcription.emotion else ""
    )
    return f"[语音转文本] {emotion}{transcription.text.strip()}"


def _local_media_path(media_dir: str, stored_path: str | None) -> str | None:
    """Resolve a DB media path written on either Windows or POSIX."""
    if not stored_path:
        return None
    value = os.fspath(stored_path)
    if os.path.isabs(value):
        return value if os.path.isfile(value) else None
    relative = value.replace("\\", os.sep).replace("/", os.sep)
    candidate = os.path.abspath(os.path.join(media_dir, relative))
    return candidate if os.path.isfile(candidate) else None


def _voice_audio_path(msg, cj: dict | None, media_dir: str) -> str | None:
    """Find the local audio downloaded by ``WebChatScraper``.

    New rows carry ``media_local_path=voice/<server id>.mpeg``.  The filename
    fallback also supports older databases where the audio exists but that
    column was not populated.
    """
    stored = _local_media_path(media_dir, msg["media_local_path"])
    if stored:
        return stored
    if not _is_voice_message(cj):
        return None

    msg_id = str(msg["msg_id"] or "")
    stem = msg_id[4:] if msg_id.startswith("srv_") else msg_id
    if not stem:
        return None
    voice_dir = os.path.join(media_dir, "voice")
    for ext in (".mpeg", ".mp3", ".wav", ".m4a", ".mp4", ".aac", ".ogg", ".flac", ".webm"):
        candidate = os.path.join(voice_dir, stem + ext)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def _resolve_message(
    msg,
    cj: dict | None,
    media_dir: str,
    voice_transcription: ASRResult | None = None,
) -> tuple:
    """Decide the ChatLab content + type for one DB message.

    Returns (content, chatlab_type, stats) where stats is a dict of counter
    increments ({'voice':1}, {'image':1,'image_embedded':1}, ...). The ordering
    of the voice/video/emoji/image and share/system branches is load-bearing and
    matches the original inline loop exactly (see test_exporter).
    """
    msg_type = msg["msg_type"]
    content = msg["content"]
    chatlab_type = CHATLAB_TYPE_MAP.get(msg_type, 99)
    stats: dict = {}

    # 语音消息：msg_type=0 但有 resource_url + duration。音频本身不嵌入
    # ChatLab；启用 ASR 时在稳定标签后附上转写，失败仍保留原标签。
    is_voice = False
    if _is_voice_message(cj):
        is_voice = True
        chatlab_type = 0  # TEXT
        content = _voice_content(cj, voice_transcription)
        stats["voice"] = 1
        if voice_transcription and voice_transcription.text.strip():
            stats["voice_transcribed"] = 1
        if voice_transcription and voice_transcription.emotion:
            stats["voice_emotion"] = 1

    # 视频消息：msg_type=5 (新分类) 或 cj.video.vid 兜底（老数据）
    is_video = False
    if not is_voice and ((msg_type == 5) or (cj and cj.get("video", {}).get("vid"))):
        is_video = True
        try:
            dur_sec = round(float((cj or {}).get("duration") or 0))
        except (TypeError, ValueError):
            dur_sec = 0
        content = f"[视频 {dur_sec}秒]" if dur_sec else "[视频]"
        chatlab_type = 0  # TEXT —— ChatLab 没有 video 类型，poster 用 IMAGE 另放
        stats["video"] = 1

    # 表情：用文字标签代替 URL — CDN 早晚过期，URL 对 LLM 也没意义。
    if not is_voice and not is_video and chatlab_type == 5:
        content = _emoji_text_label(content, msg["media_url"])
        stats["emoji"] = 1
    # 图片：优先 CDN URL，本地文件 fallback 为 base64
    elif not is_voice and not is_video and chatlab_type == 1:
        if msg["media_url"]:
            content = msg["media_url"]
            stats["image"] = 1
        else:
            local_path = _local_media_path(media_dir, msg["media_local_path"])
            data_url = _file_to_data_url(local_path) if local_path else None
            if data_url:
                content = data_url
                stats["image_embedded"] = 1
            elif local_path:
                chatlab_type = 0
            if local_path:
                stats["image"] = 1

    # 分享消息：以 cj 的形态判断（含 itemId），不依赖 msg_type。
    # 不要放宽到 aweType / content_title 等字段 —— 表情消息的 cj 也带这些。
    if not is_voice and not is_video and cj and cj.get("itemId"):
        item_id = cj.get("itemId", "")
        title = (cj.get("content_title") or "").strip()
        author = (cj.get("content_name") or "").strip()
        parts = []
        if title:
            parts.append(title)
        if author:
            parts.append(f"@{author}")
        if item_id:
            parts.append(f"https://www.douyin.com/video/{item_id}")
        content = "[分享视频] " + " | ".join(parts) if parts else "[分享视频]"
        chatlab_type = 24  # SHARE，统一类型
        stats["share"] = 1
    # 系统消息（msg_type=0 但不是语音 / 不是 share / 不是 video）
    elif not is_voice and not is_video and msg_type == 0:
        content = _system_message_text(content, cj)
        if chatlab_type == 99:
            chatlab_type = 0  # TEXT
        stats["system"] = 1

    # 最终兜底：还是 JSON 的内容统一收敛
    if isinstance(content, str) and content.startswith("{") and content.endswith("}"):
        content = "[分享内容]"

    return content, chatlab_type, stats


def _build_reply_to(ref_msg_raw) -> dict | None:
    """Build the ChatLab replyTo block from a message's stored ref_msg JSON."""
    if not ref_msg_raw:
        return None
    try:
        ref = json.loads(ref_msg_raw) if isinstance(ref_msg_raw, str) else ref_msg_raw
        ref_info = {}
        if ref.get("server_id"):
            ref_info["replyTo"] = f"srv_{ref['server_id']}"
        if ref.get("nickname"):
            ref_info["replyToAuthor"] = ref["nickname"]
        if ref.get("content"):
            ref_info["replyToContent"] = ref["content"]
        return ref_info or None
    except (json.JSONDecodeError, TypeError):
        return None


def _positive_number(value, default, cast, label):
    """Parse a positive CLI/environment number with a safe default."""
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = cast(value)
    except (TypeError, ValueError):
        print(f"[!] {label}={value!r} 无效，使用默认值 {default}")
        return default
    if parsed <= 0:
        print(f"[!] {label} 必须大于 0，使用默认值 {default}")
        return default
    return parsed


class ChatLabExporter:
    def __init__(
        self,
        conv_name: str = None,
        output_format: str = "jsonl",
        *,
        asr_url: str | None = None,
        asr_language: str | None = None,
        asr_prompt: str | None = None,
        asr_timeout: float | None = None,
        asr_batch_size: int | None = None,
    ):
        self.conv_name = conv_name
        self.output_format = output_format  # "json" or "jsonl"
        # Explicit empty URL disables ASR even when the environment has a URL.
        self.asr_url = (
            os.environ.get("QWEN_ASR_URL", "") if asr_url is None else asr_url
        ).strip()
        self.asr_language = (
            os.environ.get("QWEN_ASR_LANGUAGE", "Chinese")
            if asr_language is None
            else asr_language
        ).strip() or None
        self.asr_prompt = (
            os.environ.get("QWEN_ASR_PROMPT", "")
            if asr_prompt is None
            else asr_prompt
        ).strip()
        self.asr_timeout = _positive_number(
            os.environ.get("QWEN_ASR_TIMEOUT") if asr_timeout is None else asr_timeout,
            300.0,
            float,
            "QWEN_ASR_TIMEOUT",
        )
        self.asr_batch_size = _positive_number(
            os.environ.get("QWEN_ASR_BATCH_SIZE")
            if asr_batch_size is None
            else asr_batch_size,
            10,
            int,
            "QWEN_ASR_BATCH_SIZE",
        )
        self.last_stats: dict = {}

    def _transcribe_voice_messages(
        self, message_contexts: list[tuple], media_dir: str
    ) -> tuple[dict[str, ASRResult], dict]:
        stats = {
            "enabled": bool(self.asr_url),
            "voice_messages": 0,
            "local_audio": 0,
            "cached": 0,
            "transcribed": 0,
            "empty": 0,
            "failed": 0,
            "missing": 0,
        }
        by_message_id: dict[str, ASRResult] = {}
        file_to_messages: dict[str, list[str]] = {}
        cache_conn = get_db()
        ensure_transcription_table(cache_conn)
        try:
            for msg, cj in message_contexts:
                if not _is_voice_message(cj):
                    continue
                stats["voice_messages"] += 1
                msg_id = str(msg["msg_id"])
                audio_path = _voice_audio_path(msg, cj, media_dir)
                cached = load_cached_transcription(cache_conn, msg_id, audio_path)
                if cached:
                    by_message_id[msg_id] = cached
                    stats["cached"] += 1
                    continue
                if not self.asr_url:
                    continue
                if not audio_path:
                    stats["missing"] += 1
                    continue
                normalized = os.path.abspath(audio_path)
                file_to_messages.setdefault(normalized, []).append(msg_id)
                stats["local_audio"] += 1

            if not self.asr_url:
                return by_message_id, stats

            if not file_to_messages:
                if stats["voice_messages"] and not stats["cached"]:
                    print("[!] 已启用语音转写，但没有找到本地语音文件")
                return by_message_id, stats

            print(
                f"[*] Qwen3-ASR: 正在转写 {len(file_to_messages)} 个本地语音文件 "
                f"({self.asr_url})"
            )
            try:
                with QwenASRClient(
                    self.asr_url,
                    language=self.asr_language,
                    prompt=self.asr_prompt,
                    timeout=self.asr_timeout,
                    batch_size=self.asr_batch_size,
                ) as client:
                    results, errors = client.transcribe_files(file_to_messages)
            except Exception as exc:
                # ASR is an optional enrichment. Preserve a usable export if its
                # URL/config/client fails unexpectedly.
                message = f"ASR 初始化失败: {exc}"
                errors = {path: message for path in file_to_messages}
                results = {}

            for path, message_ids in file_to_messages.items():
                result = results.get(path)
                if result and result.text.strip():
                    for msg_id in message_ids:
                        by_message_id[msg_id] = result
                        save_cached_transcription(
                            cache_conn, msg_id, result, path, self.asr_url
                        )
                    cache_conn.commit()
                    stats["transcribed"] += len(message_ids)
                elif result:
                    stats["empty"] += len(message_ids)
                else:
                    stats["failed"] += len(message_ids)

            # Log unique file failures, capped so a dead server cannot flood a
            # control-panel log for a large conversation.
            for index, (path, reason) in enumerate(errors.items()):
                if index >= 10:
                    print(f"  [ASR] 另有 {len(errors) - 10} 个失败未逐条显示")
                    break
                print(f"  [ASR] {os.path.basename(path)} 转写失败: {reason}")

            return by_message_id, stats
        finally:
            cache_conn.commit()
            cache_conn.close()

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

        # Media base dir + content_json are resolved once.  Closing the DB
        # before a potentially long remote ASR call avoids holding a reader
        # connection for the duration of model inference.
        media_dir = paths.MEDIA_DIR
        message_contexts = [(msg, _get_content_json(msg)) for msg in messages]
        conn.close()
        voice_transcriptions, asr_stats = self._transcribe_voice_messages(
            message_contexts, media_dir
        )

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
        emoji_count = 0
        voice_count = 0
        voice_transcribed = 0
        voice_emotion = 0
        video_count = 0
        system_count = 0
        share_normalized = 0
        ref_count = 0

        for msg, cj in message_contexts:

            # 发送方：从 users_map 获取昵称
            uid = msg["sender_uid"] or ""
            display_name = users_map.get(uid, "")
            if not display_name:
                display_name = owner_name if uid == owner_uid else conv_name

            content, chatlab_type, stats = _resolve_message(
                msg,
                cj,
                media_dir,
                voice_transcriptions.get(str(msg["msg_id"])),
            )
            voice_count += stats.get("voice", 0)
            voice_transcribed += stats.get("voice_transcribed", 0)
            voice_emotion += stats.get("voice_emotion", 0)
            video_count += stats.get("video", 0)
            emoji_count += stats.get("emoji", 0)
            image_count += stats.get("image", 0)
            image_embedded += stats.get("image_embedded", 0)
            share_normalized += stats.get("share", 0)
            system_count += stats.get("system", 0)

            chatlab_msg = {
                "sender": uid,
                "accountName": display_name,
                "timestamp": msg["timestamp"] or 0,
                "type": chatlab_type,
                "content": content,
                "platformMessageId": msg["msg_id"],
            }

            # 引用/回复消息
            reply_to = _build_reply_to(msg["ref_msg"])
            if reply_to:
                chatlab_msg["replyTo"] = reply_to
                ref_count += 1

            chatlab_messages.append(chatlab_msg)

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
        if emoji_count:
            print(f"  表情: {emoji_count} (转为文字标签)")
        if voice_count:
            if asr_stats["enabled"] or asr_stats["cached"]:
                details = [f"已识别 {voice_transcribed}"]
                if asr_stats["cached"]:
                    details.append(f"复用缓存 {asr_stats['cached']}")
                if asr_stats["transcribed"]:
                    details.append(f"本次识别 {asr_stats['transcribed']}")
                if asr_stats["empty"]:
                    details.append(f"空结果 {asr_stats['empty']}")
                if asr_stats["failed"]:
                    details.append(f"失败 {asr_stats['failed']}")
                if asr_stats["missing"]:
                    details.append(f"缺少本地文件 {asr_stats['missing']}")
                if voice_emotion:
                    details.append(f"含情绪 {voice_emotion}")
                print(f"  语音: {voice_count} ({', '.join(details)})")
            else:
                print(f"  语音: {voice_count} (未启用 ASR，转为文字标签)")
        if video_count:
            print(f"  视频: {video_count} (转为文字标签 + 封面图)")
        if system_count:
            print(f"  系统消息: {system_count} (模板渲染为文字)")
        if share_normalized:
            print(f"  分享视频: {share_normalized} (含 type=1 错分类的)")
        if ref_count:
            print(f"  引用/回复: {ref_count}")
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"  文件大小: {size_mb:.1f} MB")
        self.last_stats = {
            "messages": len(chatlab_messages),
            "voice": voice_count,
            "voice_transcribed": voice_transcribed,
            "voice_emotion": voice_emotion,
            "asr": asr_stats,
        }
