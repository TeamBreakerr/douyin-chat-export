"""Server酱 (sct.ftqq.com) failure notifications for the control panel.

Extracted from control_panel.py. send_serverchan_sync/build_failure_desp are
pure/blocking; notify_on_failure is the fire-and-forget async wrapper that reads
the sendkey from the shared panel config.
"""
import asyncio
import json
import os

from common import config


def send_serverchan_sync(sendkey: str, title: str, desp: str) -> tuple[bool, str]:
    """Blocking POST to Server酱. Returns (ok, message)."""
    import urllib.request
    import urllib.parse

    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
                # Server酱 turbo returns {"code":0,...}; legacy returns {"errno":0,...}
                code = payload.get("code", payload.get("errno", -1))
                if code == 0:
                    return True, "已发送"
                return False, f"Server酱返回错误: {payload.get('message') or body[:200]}"
            except json.JSONDecodeError:
                return False, f"Server酱响应非 JSON: {body[:200]}"
    except Exception as e:
        return False, f"请求失败: {e}"


def build_failure_desp(reason: str, log_path: str | None = None, tail: int = 20) -> str:
    """Markdown body for failure notifications: timestamp, reason, last N log lines."""
    import datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts = [
        f"**失败时间**: {ts}",
        f"**原因**: {reason or '未知错误'}",
    ]
    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            log_excerpt = "".join(lines[-tail:]).rstrip()
            if log_excerpt:
                parts.append("**日志末尾**:\n```\n" + log_excerpt + "\n```")
        except Exception:
            pass
    return "\n\n".join(parts)


async def notify_on_failure(title: str, desp: str) -> None:
    """Fire-and-forget notification. Reads sendkey from config; silently no-ops if not set."""
    cfg = config.load_config()
    sendkey = (cfg.get("notify_serverchan_key") or "").strip()
    if not sendkey:
        return
    try:
        ok, msg = await asyncio.to_thread(send_serverchan_sync, sendkey, title, desp)
        if not ok:
            print(f"[!] 通知发送失败: {msg}")
    except Exception as e:
        print(f"[!] 通知发送异常: {e}")
