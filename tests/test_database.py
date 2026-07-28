"""Characterization tests for the reader DB pagination semantics.

The before_seq/after_seq branch behavior (incl. the before_seq=0 truthiness
quirk and after_seq=0 'from the beginning' rule) and the whole-conversation
`total` are observable behavior the frontend scroller depends on.
"""
from tests.conftest import insert_conversation, insert_message


def _seed(temp_db):
    import backend.database as database
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话", last_message_time=10)
    for i in range(1, 6):
        insert_message(conn, f"m{i}", "c1", i, content=f"msg{i}", timestamp=i)
    conn.commit()
    conn.close()


def test_initial_load_returns_chronological(temp_db):
    import backend.database as database
    _seed(temp_db)
    items, total = database.get_messages("c1", page_size=100)
    assert [m["seq"] for m in items] == [1, 2, 3, 4, 5]
    assert total == 5


def test_before_seq_loads_older_chronological(temp_db):
    import backend.database as database
    _seed(temp_db)
    items, total = database.get_messages("c1", page_size=100, before_seq=3)
    assert [m["seq"] for m in items] == [1, 2]
    assert total == 5  # total is always the whole-conversation count


def test_before_seq_zero_is_treated_as_no_param(temp_db):
    # Documented quirk: `if before_seq:` truthiness → before_seq=0 falls through
    # to the default (initial) branch rather than filtering seq < 0.
    import backend.database as database
    _seed(temp_db)
    items, _ = database.get_messages("c1", page_size=100, before_seq=0)
    assert [m["seq"] for m in items] == [1, 2, 3, 4, 5]


def test_after_seq_zero_loads_from_beginning(temp_db):
    import backend.database as database
    _seed(temp_db)
    items, _ = database.get_messages("c1", page_size=100, after_seq=0)
    assert [m["seq"] for m in items] == [1, 2, 3, 4, 5]


def test_after_seq_positive_loads_forward_ascending(temp_db):
    import backend.database as database
    _seed(temp_db)
    items, _ = database.get_messages("c1", page_size=100, after_seq=2)
    assert [m["seq"] for m in items] == [3, 4, 5]


def test_page_size_limit(temp_db):
    import backend.database as database
    _seed(temp_db)
    items, total = database.get_messages("c1", page_size=2)
    # initial load = newest page (seq 4,5) reversed to chronological
    assert [m["seq"] for m in items] == [4, 5]
    assert total == 5


def test_search_joins_conv_and_sender_name(temp_db):
    import backend.database as database
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话名")
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('u1','昵称')")
    insert_message(conn, "m1", "c1", 1, sender_uid="u1", content="hello world")
    conn.commit()
    conn.close()
    items, total = database.search_messages("hello")
    assert total == 1
    assert items[0]["conv_name"] == "会话名"
    assert items[0]["sender_display_name"] == "昵称"


def test_delete_conversation_removes_messages(temp_db):
    import backend.database as database
    _seed(temp_db)
    res = database.delete_conversation("c1")
    assert res == {"conversation_deleted": 1, "messages_deleted": 5}
    assert database.get_conversation("c1") is None


# ── New reader queries: by-date / range / daily stats ──

def _seed_dated(temp_db):
    import backend.database as database
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话", last_message_time=10)
    # 2025-07-27 23:30 +08 / 2025-07-28 00:30 +08 / 2025-07-28 21:00 +08 / 2025-07-29 00:01 +08
    stamps = [1753630200, 1753633800, 1753707600, 1753718460]
    for i, ts in enumerate(stamps, start=1):
        insert_message(conn, f"d{i}", "c1", i, content=f"msg{i}", timestamp=ts)
    conn.commit()
    conn.close()


def test_by_date_uses_tz_boundary(temp_db):
    import backend.database as database
    _seed_dated(temp_db)
    items = database.get_messages_by_date("c1", "2025-07-28", tz_hours=8)
    assert [m["seq"] for m in items] == [2, 3]
    # UTC 界定时，00:30 +08 落回 07-27
    items_utc = database.get_messages_by_date("c1", "2025-07-27", tz_hours=0)
    assert [m["seq"] for m in items_utc] == [1, 2]


def test_by_date_empty_day(temp_db):
    import backend.database as database
    _seed_dated(temp_db)
    assert database.get_messages_by_date("c1", "2024-01-01") == []


def test_range_is_inclusive_ascending(temp_db):
    import backend.database as database
    _seed_dated(temp_db)
    items = database.get_messages_range("c1", 2, 3)
    assert [m["seq"] for m in items] == [2, 3]


def test_daily_stats_groups_by_local_date(temp_db):
    import backend.database as database
    _seed_dated(temp_db)
    stats = database.get_daily_stats("c1", tz_hours=8)
    assert stats == [
        {"date": "2025-07-27", "count": 1},
        {"date": "2025-07-28", "count": 2},
        {"date": "2025-07-29", "count": 1},
    ]
