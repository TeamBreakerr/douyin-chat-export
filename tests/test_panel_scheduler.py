"""Unit tests for the extracted cron engine (backend/panel/scheduler.py)."""
from datetime import datetime, timedelta

from backend.panel import scheduler as s


def test_parse_cron_valid_and_invalid():
    assert s.parse_cron("0 6 * * *") is not None
    assert s.parse_cron("bad") is None          # wrong field count
    assert s.parse_cron("0 6 * *") is None       # 4 fields
    assert s.parse_cron("99 6 * * *") is None     # minute out of range -> empty set


def test_expand_field_forms():
    assert s.expand_cron_field("*", 0, 5) == {0, 1, 2, 3, 4, 5}
    assert s.expand_cron_field("*/2", 0, 6) == {0, 2, 4, 6}
    assert s.expand_cron_field("1-3", 0, 9) == {1, 2, 3}
    assert s.expand_cron_field("1,3,5", 0, 9) == {1, 3, 5}
    assert s.expand_cron_field("2-8/3", 0, 10) == {2, 5, 8}


def test_convert_dow_sunday_zero_to_python():
    # cron 0=Sunday -> python weekday 6; cron 1=Monday -> python 0
    assert s.convert_dow({0}) == {6}
    assert s.convert_dow({1}) == {0}
    assert s.convert_dow({0, 1, 6}) == {6, 0, 5}


def test_next_cron_run_every_minute_is_next_minute():
    parsed = s.parse_cron("* * * * *")
    ts = s.next_cron_run(parsed)
    expected = (datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=1))
    # within a couple minutes of the next minute boundary
    assert abs(ts - expected.timestamp()) <= 120


def test_next_cron_run_specific_hour_in_future():
    parsed = s.parse_cron("0 6 * * *")  # 06:00 daily
    ts = s.next_cron_run(parsed)
    dt = datetime.fromtimestamp(ts)
    assert dt.hour == 6 and dt.minute == 0
    assert ts > datetime.now().timestamp()
