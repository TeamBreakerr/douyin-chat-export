"""Homegrown 5-field cron parser + next-run finder for the control panel.

Pure functions (no shared state) — extracted from control_panel.py so they can
be unit-tested in isolation. The panel's cron avoids a croniter dependency and
supports the common subset: '*', '*/n', 'a-b', 'a-b/n', 'a,b,c'.
"""
import time


def parse_cron(expr: str) -> list | None:
    """Parse a 5-field cron expression. Returns list of 5 sets or None."""
    fields = expr.strip().split()
    if len(fields) != 5:
        return None
    ranges = [
        (0, 59),   # minute
        (0, 23),   # hour
        (1, 31),   # day of month
        (1, 12),   # month
        (0, 6),    # day of week (0=Sun)
    ]
    result = []
    for field, (lo, hi) in zip(fields, ranges):
        try:
            values = expand_cron_field(field, lo, hi)
            if not values:
                return None
            result.append(values)
        except Exception:
            return None
    return result


def expand_cron_field(field: str, lo: int, hi: int) -> set:
    """Expand a single cron field like '*/5', '1,3,5', '0-12', '*'."""
    values = set()
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            step = int(step)
            if base == "*":
                start = lo
            elif "-" in base:
                start = int(base.split("-")[0])
            else:
                start = int(base)
            for v in range(start, hi + 1, step):
                if lo <= v <= hi:
                    values.add(v)
        elif "-" in part:
            a, b = part.split("-", 1)
            for v in range(int(a), int(b) + 1):
                if lo <= v <= hi:
                    values.add(v)
        elif part == "*":
            values.update(range(lo, hi + 1))
        else:
            v = int(part)
            if lo <= v <= hi:
                values.add(v)
    return values


def next_cron_run(parsed: list) -> float:
    """Find next datetime matching the cron fields."""
    from datetime import datetime, timedelta
    now = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=1)
    minutes, hours, days, months, dow = parsed
    # Search up to 366 days ahead
    for _ in range(366 * 24 * 60):
        if (now.month in months and now.day in days and
                now.hour in hours and now.minute in minutes and
                now.weekday() in convert_dow(dow)):
            return now.timestamp()
        now += timedelta(minutes=1)
    return time.time() + 86400  # fallback: 1 day


def convert_dow(cron_dow: set) -> set:
    """Convert cron day-of-week (0=Sun) to Python weekday (0=Mon)."""
    mapping = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
    return {mapping.get(d, d) for d in cron_dow}
