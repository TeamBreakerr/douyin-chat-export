"""Tests for control-panel job coordination fixes (race + stop-not-failure).

The route handlers are called directly (not over HTTP) to bypass the auth
middleware and the Playwright login probe.
"""
import asyncio

from backend import control_panel as cp


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
