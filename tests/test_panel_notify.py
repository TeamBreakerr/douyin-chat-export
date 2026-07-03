"""Unit tests for the extracted Server酱 notifier (backend/panel/notify.py)."""
from backend.panel import notify


def test_build_failure_desp_includes_reason_and_log_tail(tmp_path):
    log = tmp_path / "scrape.log"
    log.write_text("line1\nline2\nline3\n", encoding="utf-8")
    desp = notify.build_failure_desp("采集失败 (exit code 2)", str(log), tail=2)
    assert "**原因**: 采集失败 (exit code 2)" in desp
    assert "**失败时间**:" in desp
    assert "line2" in desp and "line3" in desp
    assert "line1" not in desp  # only the last 2 lines
    assert "```" in desp


def test_build_failure_desp_without_log():
    desp = notify.build_failure_desp("", None)
    assert "**原因**: 未知错误" in desp  # empty reason -> default
    assert "日志末尾" not in desp


def test_send_serverchan_parses_success(monkeypatch):
    import io

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"code":0,"message":"ok"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
    ok, msg = notify.send_serverchan_sync("KEY", "t", "d")
    assert ok is True


def test_send_serverchan_parses_legacy_errno(monkeypatch):
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"errno":0}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
    ok, _ = notify.send_serverchan_sync("KEY", "t", "d")
    assert ok is True


def test_send_serverchan_error_code(monkeypatch):
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"code":40001,"message":"bad key"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
    ok, msg = notify.send_serverchan_sync("KEY", "t", "d")
    assert ok is False
    assert "bad key" in msg
