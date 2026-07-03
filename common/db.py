"""Shared SQLite storage core: connection factory, schema, and writer helpers.

Previously `backend/database.py` and `extractor/models.py` each defined their
own `DB_PATH` + `get_db`, with *divergent* pragmas — the reader (backend) left
foreign keys OFF (its two-step delete relies on no cascade), while the writer
(extractor) enabled WAL + foreign_keys. That divergence is preserved here
explicitly via `connect(foreign_keys=..., wal=...)` rather than being papered
over.

The DB path is read from `common.paths.DB_PATH` at call time so tests can
repoint it at a temp file with a single monkeypatch.
"""
import json
import os
import sqlite3

from common import paths


def connect(*, foreign_keys: bool = False, wal: bool = False) -> sqlite3.Connection:
    """Open a connection to the chat DB.

    Args:
        foreign_keys: enable PRAGMA foreign_keys=ON (writer side).
        wal: enable PRAGMA journal_mode=WAL (writer side).
    """
    conn = sqlite3.connect(paths.DB_PATH)
    conn.row_factory = sqlite3.Row
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(paths.DB_PATH), exist_ok=True)
    conn = connect(foreign_keys=True, wal=True)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            uid TEXT PRIMARY KEY,
            nickname TEXT,
            avatar_url TEXT,
            unique_id TEXT
        );

        CREATE TABLE IF NOT EXISTS conversations (
            conv_id TEXT PRIMARY KEY,
            conv_type INTEGER DEFAULT 1,
            name TEXT,
            participant_uids TEXT DEFAULT '[]',
            last_message_time INTEGER DEFAULT 0,
            message_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS messages (
            msg_id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            sender_uid TEXT,
            sender_name TEXT,
            content TEXT,
            msg_type INTEGER DEFAULT 1,
            media_url TEXT,
            media_local_path TEXT,
            timestamp INTEGER,
            seq INTEGER DEFAULT 0,
            raw_data TEXT,
            ref_msg TEXT,
            FOREIGN KEY (conv_id) REFERENCES conversations(conv_id)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_messages_seq ON messages(conv_id, seq);
        CREATE INDEX IF NOT EXISTS idx_messages_content ON messages(content);
    """)
    # 迁移：为旧数据库添加 ref_msg 列
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN ref_msg TEXT")
    except sqlite3.OperationalError:
        pass  # 列已存在
    # 迁移：为旧数据库添加 conversations.avatar_url 列
    try:
        conn.execute("ALTER TABLE conversations ADD COLUMN avatar_url TEXT")
    except sqlite3.OperationalError:
        pass  # 列已存在
    conn.commit()
    conn.close()


def upsert_user(conn, uid, nickname=None, avatar_url=None, unique_id=None):
    conn.execute(
        """INSERT INTO users (uid, nickname, avatar_url, unique_id)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(uid) DO UPDATE SET
             nickname=COALESCE(excluded.nickname, nickname),
             avatar_url=COALESCE(excluded.avatar_url, avatar_url),
             unique_id=COALESCE(excluded.unique_id, unique_id)""",
        (uid, nickname, avatar_url, unique_id),
    )


def upsert_conversation(conn, conv_id, conv_type=1, name=None, participant_uids=None, avatar_url=None):
    participants = json.dumps(participant_uids or [])
    conn.execute(
        """INSERT INTO conversations (conv_id, conv_type, name, participant_uids, avatar_url)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(conv_id) DO UPDATE SET
             conv_type=COALESCE(excluded.conv_type, conv_type),
             name=COALESCE(excluded.name, name),
             participant_uids=COALESCE(excluded.participant_uids, participant_uids),
             avatar_url=COALESCE(excluded.avatar_url, avatar_url)""",
        (conv_id, conv_type, name, participants, avatar_url),
    )


def update_conversation_stats(conn, conv_id):
    conn.execute(
        """UPDATE conversations SET
             message_count = (SELECT COUNT(*) FROM messages WHERE conv_id = ?),
             last_message_time = (SELECT MAX(timestamp) FROM messages WHERE conv_id = ?)
           WHERE conv_id = ?""",
        (conv_id, conv_id, conv_id),
    )
