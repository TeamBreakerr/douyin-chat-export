"""Database access layer for the web backend (read + delete queries).

The connection factory and schema live in `common.db`; the reader uses
`connect()` with foreign keys OFF (its two-step delete relies on no cascade).
"""
import json

from common.db import connect
from common.paths import DB_PATH  # re-exported for backward compatibility
from extractor.message_types import is_merged_forward_payload, is_video_note_payload


def get_db():
    return connect()


def find_referenced_video(msg_id):
    """A related_share_video contains a video ID, not a message ID.

    Locate the nearest preceding original share in the same conversation.
    """
    from .forwarded import content_json
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
        if not row:
            return None
        source = dict(row)
        video = content_json(source).get("related_share_video") or {}
        item_id = str(video.get("itemId") or "")
        if not item_id.isdigit():
            return None
        rows = conn.execute(
            "SELECT * FROM messages WHERE conv_id=? AND seq<? "
            "AND raw_data LIKE ? ORDER BY seq DESC",
            (source["conv_id"], source["seq"], f"%{item_id}%"),
        )
        for candidate in rows:
            message = dict(candidate)
            cj = content_json(message)
            if str(cj.get("itemId") or "") == item_id and not cj.get("related_share_video"):
                return message
        return None
    finally:
        conn.close()


def get_conversations(search=None, page=1, page_size=50):
    conn = get_db()
    offset = (page - 1) * page_size

    if search:
        rows = conn.execute(
            """SELECT * FROM conversations
               WHERE name LIKE ?
               ORDER BY last_message_time DESC
               LIMIT ? OFFSET ?""",
            (f"%{search}%", page_size, offset),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE name LIKE ?",
            (f"%{search}%",),
        ).fetchone()[0]
    else:
        rows = conn.execute(
            """SELECT * FROM conversations
               ORDER BY last_message_time DESC
               LIMIT ? OFFSET ?""",
            (page_size, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]

    conn.close()
    return [dict(r) for r in rows], total


def get_conversation(conv_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM conversations WHERE conv_id = ?", (conv_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_messages(conv_id, page_size=100, before_seq=None, after_seq=None):
    conn = get_db()
    message_select = """SELECT m.*,
                              vt.text_result AS voice_transcription,
                              vt.status AS voice_transcription_status,
                              vt.error AS voice_transcription_error
                       FROM messages m
                       LEFT JOIN voice_transcriptions vt ON vt.msg_id = m.msg_id
                       WHERE m.conv_id = ?"""

    if before_seq:
        # 加载更早的消息（向上滚动时调用）
        rows = conn.execute(
            message_select + " AND m.seq < ? ORDER BY m.seq DESC LIMIT ?",
            (conv_id, before_seq, page_size),
        ).fetchall()
        rows = list(reversed(rows))
    elif after_seq is not None:
        # 从指定 seq 开始向后加载（跳到开头时调用，after_seq=0 即从头）
        rows = conn.execute(
            message_select + " AND m.seq > ? ORDER BY m.seq ASC LIMIT ?",
            (conv_id, after_seq, page_size),
        ).fetchall()
    else:
        # 初始加载：最新的100条
        rows = conn.execute(
            message_select + " ORDER BY m.seq DESC LIMIT ?",
            (conv_id, page_size),
        ).fetchall()
        rows = list(reversed(rows))

    total = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE conv_id = ?", (conv_id,)
    ).fetchone()[0]

    conn.close()
    return [dict(r) for r in rows], total



def get_messages_by_date(conv_id, date_str, tz_hours=8, limit=5000):
    """某个自然日（按 tz_hours 时区界定）内的全部消息，按 seq 升序。

    date_str: "YYYY-MM-DD"。limit 是防御性上限，正常单日消息量远低于它。
    """
    offset_sec = int(tz_hours * 3600)
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM messages
           WHERE conv_id = ?
             AND date(timestamp + ?, 'unixepoch') = ?
           ORDER BY seq ASC
           LIMIT ?""",
        (conv_id, offset_sec, date_str, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_messages_range(conv_id, start_seq, end_seq, limit=1000):
    """闭区间 [start_seq, end_seq] 的消息，按 seq 升序。"""
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM messages
           WHERE conv_id = ? AND seq >= ? AND seq <= ?
           ORDER BY seq ASC
           LIMIT ?""",
        (conv_id, start_seq, end_seq, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_daily_stats(conv_id, tz_hours=8):
    """按自然日（tz_hours 时区）统计消息量：[{date, count}, ...] 升序。"""
    offset_sec = int(tz_hours * 3600)
    conn = get_db()
    rows = conn.execute(
        """SELECT date(timestamp + ?, 'unixepoch') AS date, COUNT(*) AS count
           FROM messages
           WHERE conv_id = ?
           GROUP BY date
           ORDER BY date ASC""",
        (offset_sec, conv_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_senders(conv_id):
    """获取会话中的所有发送者 UID 及消息数量。"""
    conn = get_db()
    rows = conn.execute(
        """SELECT sender_uid, COUNT(*) as msg_count
           FROM messages WHERE conv_id = ?
           GROUP BY sender_uid ORDER BY msg_count DESC""",
        (conv_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_messages(query="", page=1, page_size=50, *, conv_id=None,
                    start_time=None, end_time=None, media_type=None):
    """Search text/transcripts with optional conversation, time and media filters.

    Time bounds are [start_time, end_time), so adjacent dates never overlap.
    """
    raw = "CASE WHEN json_valid(m.raw_data) THEN m.raw_data ELSE '{}' END"
    content = f"json_extract({raw}, '$.content_json')"
    cj = f"CASE WHEN json_valid({content}) THEN {content} ELSE '{{}}' END"
    clauses, params = [], []
    if query:
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        fields = ["m.content", "vt.text_result", *[
            f"json_extract({cj}, '$.{key}')"
            for key in ("content_title", "aweme_title", "comment", "text")
        ]]
        clauses.append("(" + " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in fields) + ")")
        params.extend([pattern] * len(fields))
    if conv_id is not None:
        clauses.append("m.conv_id = ?")
        params.append(conv_id)
    if start_time is not None:
        clauses.append("m.timestamp >= ?")
        params.append(start_time)
    if end_time is not None:
        clauses.append("m.timestamp < ?")
        params.append(end_time)
    # Legacy video rows were stored as images/text. Inspect the preserved JSON
    # as well as the local file; malformed/truncated JSON must not break search.
    video = f"""(m.msg_type = 5 OR COALESCE(lower(m.media_local_path) LIKE '%.mp4', 0)
                OR (m.msg_type IN (1, 3) AND json_extract({cj}, '$.video.vid') IS NOT NULL))"""
    if media_type == "image":
        clauses.append(f"m.msg_type = 3 AND NOT {video}")
    elif media_type == "video":
        clauses.append(video)
    elif media_type == "media":
        clauses.append(f"(m.msg_type = 3 OR {video})")
    elif media_type is not None:
        raise ValueError("未知媒体类型")
    where = " AND ".join(clauses) or "1=1"
    joins = """FROM messages m
               JOIN conversations c ON m.conv_id = c.conv_id
               LEFT JOIN users u ON m.sender_uid = u.uid
               LEFT JOIN voice_transcriptions vt ON vt.msg_id = m.msg_id"""
    conn = get_db()
    try:
        rows = conn.execute(
            f"""SELECT m.*, c.name as conv_name,
                       COALESCE(u.nickname, m.sender_name, '') as sender_display_name,
                       vt.text_result AS voice_transcription,
                       vt.status AS voice_transcription_status,
                       vt.error AS voice_transcription_error
                {joins} WHERE {where}
                ORDER BY m.seq DESC, m.msg_id DESC LIMIT ? OFFSET ?""",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        total = conn.execute(f"SELECT COUNT(*) {joins} WHERE {where}", params).fetchone()[0]
        return [dict(r) for r in rows], total
    finally:
        conn.close()


def get_message(msg_id):
    conn = get_db()
    row = conn.execute(
        """SELECT m.*,
                  vt.text_result AS voice_transcription,
                  vt.status AS voice_transcription_status,
                  vt.error AS voice_transcription_error
           FROM messages m
           LEFT JOIN voice_transcriptions vt ON vt.msg_id = m.msg_id
           WHERE m.msg_id = ?""",
        (msg_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user(uid):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_users():
    conn = get_db()
    rows = conn.execute("SELECT * FROM users").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats():
    conn = get_db()
    stats = {
        "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
    }
    conn.close()
    return stats


def _preserved_content_json(row):
    """Read the original content payload without modifying raw_data."""
    try:
        raw = json.loads(row["raw_data"] or "{}")
    except (TypeError, ValueError):
        raw = {}
    value = raw.get("content_json") if isinstance(raw, dict) else None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = None
    return value if isinstance(value, dict) else {}


def _video_note_content(payload):
    try:
        duration = round(float(payload.get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0
    return f"[视频 {duration}秒]" if duration else "[视频]"


def _merged_forward_content(payload):
    return payload.get("title") or "[聊天记录]"


def _message_type_cleanup_plan():
    """Return recognized historical rows and their normalized values."""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT msg_id, content, msg_type, raw_data
               FROM messages
               WHERE (raw_data LIKE '%video%' AND raw_data LIKE '%vid%')
                  OR raw_data LIKE '%13600%'
                  OR raw_data LIKE '%list_content%'
                  OR raw_data LIKE '%msg_ids%'"""
        ).fetchall()
    finally:
        conn.close()

    plan = {"video_notes": [], "merged_forwards": []}
    for row in rows:
        payload = _preserved_content_json(row)
        if is_video_note_payload(payload):
            plan["video_notes"].append(
                (row["msg_id"], 5, _video_note_content(payload), row["msg_type"], row["content"])
            )
        elif is_merged_forward_payload(payload):
            plan["merged_forwards"].append(
                (row["msg_id"], 6, _merged_forward_content(payload), row["msg_type"], row["content"])
            )
    return plan


def preview_message_type_cleanup():
    """Preview the idempotent video/merged-forward normalization."""
    result = {}
    for key, rows in _message_type_cleanup_plan().items():
        need_update = sum(
            current_type != wanted_type or current_content != wanted_content
            for _, wanted_type, wanted_content, current_type, current_content in rows
        )
        result[key] = {
            "total": len(rows),
            "need_update": need_update,
            "already_clean": len(rows) - need_update,
        }
    return result


def cleanup_message_types():
    """Normalize known rows while preserving their original raw_data exactly."""
    plan = _message_type_cleanup_plan()
    result = {}
    conn = get_db()
    try:
        for key, rows in plan.items():
            updates = [
                (wanted_type, wanted_content, msg_id)
                for msg_id, wanted_type, wanted_content, current_type, current_content in rows
                if current_type != wanted_type or current_content != wanted_content
            ]
            conn.executemany(
                "UPDATE messages SET msg_type = ?, content = ? WHERE msg_id = ?",
                updates,
            )
            result[key] = {"updated": len(updates), "skipped": len(rows) - len(updates)}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return result


def delete_conversation_messages(conv_id):
    """Delete all messages for a conversation (keep the conversation row)."""
    conn = get_db()
    conn.execute(
        """DELETE FROM voice_transcriptions
           WHERE msg_id IN (SELECT msg_id FROM messages WHERE conv_id = ?)""",
        (conv_id,),
    )
    cur = conn.execute("DELETE FROM messages WHERE conv_id = ?", (conv_id,))
    deleted = cur.rowcount
    conn.execute(
        "UPDATE conversations SET message_count = 0, last_message_time = 0 WHERE conv_id = ?",
        (conv_id,),
    )
    conn.commit()
    conn.close()
    return deleted


def delete_conversation(conv_id):
    """Delete a conversation and all its messages."""
    conn = get_db()
    conn.execute(
        """DELETE FROM voice_transcriptions
           WHERE msg_id IN (SELECT msg_id FROM messages WHERE conv_id = ?)""",
        (conv_id,),
    )
    msg_cur = conn.execute("DELETE FROM messages WHERE conv_id = ?", (conv_id,))
    msg_deleted = msg_cur.rowcount
    conv_cur = conn.execute("DELETE FROM conversations WHERE conv_id = ?", (conv_id,))
    conv_deleted = conv_cur.rowcount
    conn.commit()
    conn.close()
    return {"conversation_deleted": conv_deleted, "messages_deleted": msg_deleted}
