"""Shared test fixtures.

These tests are *characterization* tests: they lock the current observable
behavior of the code before/after the refactor. They never touch the real
data/ directory — every fixture points the DB at a temp file.
"""
import os
import sys

import pytest

# Ensure the repo root is importable (so `import backend...`, `import extractor...` work)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Create an isolated chat.db with the real schema and repoint the single
    source of truth (common.paths.DB_PATH) at it. Both the reader (backend) and
    writer (extractor) resolve the path from there at call time."""
    import common.paths as paths
    import extractor.models as models

    db_path = str(tmp_path / "chat.db")
    monkeypatch.setattr(paths, "DB_PATH", db_path)
    models.init_db()
    return db_path


def insert_conversation(conn, conv_id, name, participant_uids="[]",
                        last_message_time=0, conv_type=1):
    conn.execute(
        "INSERT INTO conversations (conv_id, conv_type, name, participant_uids, "
        "last_message_time, message_count) VALUES (?,?,?,?,?,0)",
        (conv_id, conv_type, name, participant_uids, last_message_time),
    )


def insert_message(conn, msg_id, conv_id, seq, *, sender_uid="u1",
                   sender_name="", content="", msg_type=1, media_url=None,
                   media_local_path=None, timestamp=0, raw_data=None, ref_msg=None):
    """Insert a message row using the *real* writer column set (seq + ref_msg),
    which extractor.models.insert_message omits."""
    conn.execute(
        "INSERT OR IGNORE INTO messages (msg_id, conv_id, sender_uid, sender_name, "
        "content, msg_type, media_url, media_local_path, timestamp, seq, raw_data, ref_msg) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (msg_id, conv_id, sender_uid, sender_name, content, msg_type, media_url,
         media_local_path, timestamp, seq, raw_data, ref_msg),
    )
