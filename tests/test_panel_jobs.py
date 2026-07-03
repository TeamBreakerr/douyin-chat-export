"""Tests for control-panel job coordination fixes (race + stop-not-failure).

The route handlers are called directly (not over HTTP) to bypass the auth
middleware and the Playwright login probe.
"""
import asyncio
import sys

import pytest

from backend import control_panel as cp


@pytest.fixture
def isolated_scrape(tmp_path, monkeypatch):
    """Point the scrape log at a temp file and capture failure notifications so
    _run_scrape can be exercised without touching data/ or hitting Server酱."""
    monkeypatch.setattr(cp, "LOG_PATH", str(tmp_path / "scrape.log"))
    notified = []

    async def _fake_notify(title, desp):
        notified.append((title, desp))

    monkeypatch.setattr(cp, "_notify_on_failure", _fake_notify)
    return notified


class _FakeProc:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True


def test_stop_scrape_sets_stopped_flag(monkeypatch):
    proc = _FakeProc()
    monkeypatch.setitem(cp._scrape_state, "process", proc)
    monkeypatch.setitem(cp._scrape_state, "stopped", False)
    monkeypatch.setitem(cp._scrape_state, "status", "running")

    res = asyncio.run(cp.stop_scrape())
    assert res == {"status": "stopped"}
    assert proc.terminated is True
    assert cp._scrape_state["stopped"] is True   # _run_scrape will skip failure/notify
    assert cp._scrape_state["status"] == "idle"


def test_stop_scrape_when_not_running(monkeypatch):
    monkeypatch.setitem(cp._scrape_state, "process", None)
    res = asyncio.run(cp.stop_scrape())
    assert res == {"status": "not_running"}


def test_backfill_start_marks_running_synchronously(monkeypatch):
    # Prevent the real backfill from running; capture that a task was created.
    created = []
    monkeypatch.setattr(cp.asyncio, "create_task", lambda coro: created.append(coro) or coro.close())
    monkeypatch.setitem(cp._backfill_state, "status", "idle")

    res = asyncio.run(cp.backfill_start())
    assert res == {"status": "started"}
    # Status is 'running' immediately (before the coroutine runs) -> a second
    # concurrent POST would hit the 409 guard.
    assert cp._backfill_state["status"] == "running"


def test_backfill_start_conflict(monkeypatch):
    monkeypatch.setitem(cp._backfill_state, "status", "running")
    res = asyncio.run(cp.backfill_start())
    assert getattr(res, "status_code", None) == 409


def test_run_scrape_clears_stale_stop_flag_and_reports_failure(isolated_scrape, monkeypatch):
    # Simulate a stale 'stopped' flag left by an earlier manual Stop, then a
    # cron/manual scrape that genuinely fails. It must be reported as failed and
    # DO notify — not swallowed as '已停止'.
    monkeypatch.setitem(cp._scrape_state, "stopped", True)
    fail_cmd = [sys.executable, "-c", "import sys; sys.exit(2)"]

    async def scenario():
        await cp._run_scrape(fail_cmd)
        await asyncio.sleep(0.1)  # let the fire-and-forget notify task run

    asyncio.run(scenario())
    assert cp._scrape_state["status"] == "failed"
    assert cp._scrape_state["stopped"] is False   # reset at the top of _run_scrape
    assert "exit code" in cp._scrape_state["message"]
    assert len(isolated_scrape) == 1              # failure notification fired


def test_run_scrape_user_stop_reports_stopped_and_does_not_notify(isolated_scrape):
    # A genuine user Stop during the run: report '已停止', no failure notification.
    slow_cmd = [sys.executable, "-c", "import time; time.sleep(30)"]

    async def scenario():
        task = asyncio.create_task(cp._run_scrape(slow_cmd))
        for _ in range(200):
            if cp._scrape_state.get("process"):
                break
            await asyncio.sleep(0.02)
        await cp.stop_scrape()
        await task
        await asyncio.sleep(0.1)

    asyncio.run(scenario())
    assert cp._scrape_state["status"] == "idle"
    assert cp._scrape_state["message"] == "已停止"
    assert isolated_scrape == []                  # no notification on intentional stop
