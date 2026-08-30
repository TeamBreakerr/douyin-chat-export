"""全量抓取的"先删后抓"窗口期保护。

全量模式会先 DELETE 掉会话的全部历史消息并 commit，然后才去抓。抓取失败时
（滚动回退读不出内容、重新加载后找不到会话、中途抛异常）历史记录就永久没了。
这里锁定：抓到 0 条就还原，抓到东西就保持"先删"的语义（清掉已撤回的消息）。
"""
from common.db import connect, upsert_conversation
from extractor.web_scraper import WebChatScraper

from tests.conftest import insert_message

CONV = "conv_1"


def _scraper_with(conn):
    s = WebChatScraper(incremental=False)
    s._db_conn = conn
    return s


def _seed(conn, n=3):
    upsert_conversation(conn, CONV, name="会话")
    for i in range(1, n + 1):
        insert_message(conn, f"m{i}", CONV, seq=i, content=f"旧消息{i}")
    conn.commit()


def test_restores_history_when_scrape_saves_nothing(temp_db):
    """抓取一条都没拿到 → 旧消息必须回来（否则一次失败就清空历史）。"""
    conn = connect(foreign_keys=True)
    _seed(conn, 3)
    s = _scraper_with(conn)

    backed_up = s._backup_conv_messages(CONV)
    assert backed_up == 3

    # 模拟全量模式：删掉旧消息并 commit，然后抓取失败（一条没存）
    conn.execute("DELETE FROM messages WHERE conv_id = ?", (CONV,))
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM messages WHERE conv_id = ?", (CONV,)).fetchone()[0] == 0

    s._restore_conv_messages_if_empty(CONV, backed_up)

    rows = conn.execute(
        "SELECT msg_id, content FROM messages WHERE conv_id = ? ORDER BY seq", (CONV,)
    ).fetchall()
    assert [r[1] for r in rows] == ["旧消息1", "旧消息2", "旧消息3"]
    # 会话统计也要跟着回来，否则前端显示 0 条
    assert conn.execute(
        "SELECT message_count FROM conversations WHERE conv_id = ?", (CONV,)
    ).fetchone()[0] == 3


def test_does_not_restore_when_scrape_succeeded(temp_db):
    """抓到了新数据 → 保持"先删"语义，已撤回的旧消息不能被还原回来。"""
    conn = connect(foreign_keys=True)
    _seed(conn, 3)
    s = _scraper_with(conn)

    backed_up = s._backup_conv_messages(CONV)
    conn.execute("DELETE FROM messages WHERE conv_id = ?", (CONV,))
    insert_message(conn, "new1", CONV, seq=1, content="重新抓到的消息")
    conn.commit()

    s._restore_conv_messages_if_empty(CONV, backed_up)

    rows = conn.execute(
        "SELECT content FROM messages WHERE conv_id = ?", (CONV,)).fetchall()
    assert [r[0] for r in rows] == ["重新抓到的消息"]


def test_no_backup_no_restore(temp_db):
    """会话本来就是空的 → 什么都不做。"""
    conn = connect(foreign_keys=True)
    upsert_conversation(conn, CONV, name="空会话")
    conn.commit()
    s = _scraper_with(conn)

    assert s._backup_conv_messages(CONV) == 0
    s._restore_conv_messages_if_empty(CONV, 0)  # 不应抛异常

    assert conn.execute(
        "SELECT COUNT(*) FROM messages WHERE conv_id = ?", (CONV,)).fetchone()[0] == 0


def test_repeated_empty_backups_clean_up_voice_temp_table(temp_db):
    """An empty full-scrape backup must not poison the next run."""
    conn = connect(foreign_keys=True)
    upsert_conversation(conn, CONV, name="空会话")
    conn.commit()
    s = _scraper_with(conn)

    assert s._backup_conv_messages(CONV) == 0
    s._restore_conv_messages_if_empty(CONV, 0)

    # The second backup uses the same connection and should be able to create
    # both temporary tables again without an "already exists" error.
    assert s._backup_conv_messages(CONV) == 0
    s._restore_conv_messages_if_empty(CONV, 0)

    temp_tables = conn.execute(
        "SELECT name FROM sqlite_temp_master WHERE type='table'"
    ).fetchall()
    assert temp_tables == []
