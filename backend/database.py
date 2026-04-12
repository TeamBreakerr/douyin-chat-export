"""Database access layer for the web backend."""
import sqlite3
import json
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "chat.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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

    if before_seq:
        # 加载更早的消息（向上滚动时调用）
        rows = conn.execute(
            """SELECT * FROM messages
               WHERE conv_id = ? AND seq < ?
               ORDER BY seq DESC
               LIMIT ?""",
            (conv_id, before_seq, page_size),
        ).fetchall()
        rows = list(reversed(rows))
    elif after_seq is not None:
        # 从指定 seq 开始向后加载（跳到开头时调用，after_seq=0 即从头）
        rows = conn.execute(
            """SELECT * FROM messages
               WHERE conv_id = ? AND seq > ?
               ORDER BY seq ASC
               LIMIT ?""",
            (conv_id, after_seq, page_size),
        ).fetchall()
    else:
        # 初始加载：最新的100条
        rows = conn.execute(
            """SELECT * FROM messages
               WHERE conv_id = ?
               ORDER BY seq DESC
               LIMIT ?""",
            (conv_id, page_size),
        ).fetchall()
        rows = list(reversed(rows))

    total = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE conv_id = ?", (conv_id,)
    ).fetchone()[0]

    conn.close()
    return [dict(r) for r in rows], total



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


def search_messages(query, page=1, page_size=50):
    conn = get_db()
    offset = (page - 1) * page_size

    rows = conn.execute(
        """SELECT m.*, c.name as conv_name,
                  COALESCE(u.nickname, m.sender_name, '') as sender_display_name
           FROM messages m
           JOIN conversations c ON m.conv_id = c.conv_id
           LEFT JOIN users u ON m.sender_uid = u.uid
           WHERE m.content LIKE ?
           ORDER BY m.seq DESC
           LIMIT ? OFFSET ?""",
        (f"%{query}%", page_size, offset),
    ).fetchall()

    total = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE content LIKE ?",
        (f"%{query}%",),
    ).fetchone()[0]

    conn.close()
    return [dict(r) for r in rows], total


def get_message(msg_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
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


def delete_conversation_messages(conv_id):
    """Delete all messages for a conversation (keep the conversation row)."""
    conn = get_db()
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
    msg_cur = conn.execute("DELETE FROM messages WHERE conv_id = ?", (conv_id,))
    msg_deleted = msg_cur.rowcount
    conv_cur = conn.execute("DELETE FROM conversations WHERE conv_id = ?", (conv_id,))
    conv_deleted = conv_cur.rowcount
    conn.commit()
    conn.close()
    return {"conversation_deleted": conv_deleted, "messages_deleted": msg_deleted}
