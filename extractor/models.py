"""SQLite writer helpers for the extractor.

The storage core (schema, connection factory, upsert helpers) now lives in
`common.db`; this module is a thin compatibility layer that keeps the historical
import surface (`from extractor.models import get_db, init_db, upsert_user, ...`)
working for the scraper, exporter, video downloader, and the tools/ probes.
"""
from common import db as _db
from common.paths import DB_PATH  # re-exported for backward compatibility

# Writer connections enable WAL + foreign keys (unchanged behavior).
from common.db import init_db, upsert_user, upsert_conversation, update_conversation_stats


def get_db():
    return _db.connect(foreign_keys=True, wal=True)


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


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
