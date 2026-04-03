"""SQLite database models and initialization."""
import sqlite3
import json
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "chat.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
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


def upsert_conversation(conn, conv_id, conv_type=1, name=None, participant_uids=None):
    participants = json.dumps(participant_uids or [])
    conn.execute(
        """INSERT INTO conversations (conv_id, conv_type, name, participant_uids)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(conv_id) DO UPDATE SET
             conv_type=COALESCE(excluded.conv_type, conv_type),
             name=COALESCE(excluded.name, name),
             participant_uids=COALESCE(excluded.participant_uids, participant_uids)""",
        (conv_id, conv_type, name, participants),
    )


def insert_message(conn, msg_id, conv_id, sender_uid, sender_name, content,
                    msg_type=1, media_url=None, media_local_path=None,
                    timestamp=None, raw_data=None):
    conn.execute(
        """INSERT OR IGNORE INTO messages
           (msg_id, conv_id, sender_uid, sender_name, content, msg_type,
            media_url, media_local_path, timestamp, raw_data)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (msg_id, conv_id, sender_uid, sender_name, content, msg_type,
         media_url, media_local_path, timestamp, raw_data),
    )


def update_conversation_stats(conn, conv_id):
    conn.execute(
        """UPDATE conversations SET
             message_count = (SELECT COUNT(*) FROM messages WHERE conv_id = ?),
             last_message_time = (SELECT MAX(timestamp) FROM messages WHERE conv_id = ?)
           WHERE conv_id = ?""",
        (conv_id, conv_id, conv_id),
    )


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
