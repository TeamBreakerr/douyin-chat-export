"""Control panel for managing scraper, viewer, and export."""
import asyncio
import json
import os
import sys
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel

from backend import database

control_router = APIRouter(prefix="/panel")

# The panel single-page app lives in backend/panel/static/panel.html and is
# loaded once at import; panel_page() serves it verbatim.
_PANEL_HTML_PATH = os.path.join(os.path.dirname(__file__), "panel", "static", "panel.html")
with open(_PANEL_HTML_PATH, encoding="utf-8") as _f:
    PANEL_HTML = _f.read()

# ── Persistent config (saved to data/panel_config.json) ──
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "panel_config.json")


def _load_config():
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # Don't let a corrupted config file block service startup —
            # fall back to defaults and let the next save overwrite it.
            print(f"[!] panel_config.json 损坏 ({e})，使用默认配置")
            return {"custom_filters": [], "schedule": ""}
    return {"custom_filters": [], "schedule": ""}


def _save_config(cfg):
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    # Atomic write: serialize to tmp file then rename, so a crash mid-write
    # (e.g. UnicodeEncodeError on Windows gbk) can't leave a half-written file.
    tmp_path = _CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    os.replace(tmp_path, _CONFIG_PATH)


# ── Scrape job state ──
_scrape_state = {
    "status": "idle",  # idle | running | completed | failed
    "started_at": None,
    "finished_at": None,
    "message": "",
    "process": None,
}

# ── Export state ──
_export_state = {
    "status": "idle",
    "file_path": None,
    "message": "",
}

# ── Scheduler state ──
_scheduler_state = {
    "enabled": False,
    "schedule": "",
    "task": None,
    "next_run": None,
}

# ── Conversation discovery (refresh conv list) state ──
_discover_state = {
    "status": "idle",  # idle | running | completed | failed
    "message": "",
    "process": None,
    "started_at": None,
    "finished_at": None,
}

# ── Media backfill state ──
_backfill_state = {
    "status": "idle",  # idle | running | completed | failed
    "total": 0,
    "done": 0,
    "ok": 0,
    "failed": 0,
    "message": "",
    "started_at": None,
    "finished_at": None,
}

_video_backfill_state = {
    "status": "idle",
    "total": 0,
    "done": 0,
    "ok": 0,
    "failed": 0,
    "skipped": 0,
    "message": "",
    "started_at": None,
    "finished_at": None,
}

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "scrape.log")
DISCOVER_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "discover.log")
CONV_LIST_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "conversations_list.json")


async def restore_schedule_on_startup():
    """从 panel_config.json 恢复定时任务（容器重启后自动恢复）。"""
    cfg = _load_config()
    cron = cfg.get("schedule", "").strip()
    if not cron:
        return
    parsed = _parse_cron(cron)
    if not parsed:
        print(f"[scheduler] 配置中的 cron 表达式无效: {cron}", flush=True)
        return
    next_run = _next_cron_run(parsed)
    _scheduler_state["enabled"] = True
    _scheduler_state["schedule"] = cron
    _scheduler_state["next_run"] = next_run
    _scheduler_state["task"] = asyncio.create_task(
        _cron_loop(parsed, incremental=True)
    )
    from datetime import datetime
    next_str = datetime.fromtimestamp(next_run).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[scheduler] 已恢复定时任务: {cron}, 下次执行: {next_str}", flush=True)


class ScrapeRequest(BaseModel):
    incremental: bool = True
    filter: str = ""
    conversations: list[str] | None = None  # selected nicknames; overrides filter


class ExportRequest(BaseModel):
    format: str = "jsonl"
    filter: str = ""
    conversations: list[str] | None = None  # selected nicknames; overrides filter


class ScheduleRequest(BaseModel):
    enabled: bool
    cron: str = ""  # cron expression: "0 0 * * *" or shorthand
    incremental: bool = True
    conversations: list[str] | None = None  # selected nicknames for scheduled scrape


class CustomFilterAction(BaseModel):
    action: str  # "add" | "remove"
    value: str


class CookieImportRequest(BaseModel):
    cookies: str  # JSON array from DevTools or "key=value; key=value" string


class PasswordRequest(BaseModel):
    password: str = ""  # empty = remove password


class SelectedUpdate(BaseModel):
    section: str  # "scraper" | "export" | "schedule"
    conversations: list[str]


@control_router.post("/api/password")
async def set_password(req: PasswordRequest):
    import hashlib
    cfg = _load_config()
    if req.password:
        cfg["password_hash"] = hashlib.sha256(req.password.encode()).hexdigest()
        _save_config(cfg)
        return {"status": "ok", "message": "密码已设置"}
    else:
        cfg.pop("password_hash", None)
        _save_config(cfg)
        return {"status": "ok", "message": "密码已清除"}


@control_router.get("/api/password/status")
async def password_status():
    cfg = _load_config()
    return {"has_password": bool(cfg.get("password_hash"))}


# ── Notifications (Server酱 / sct.ftqq.com) ──

class NotifyKeyRequest(BaseModel):
    sendkey: str = ""  # empty = remove


def _send_serverchan_sync(sendkey: str, title: str, desp: str) -> tuple[bool, str]:
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


def _build_failure_desp(reason: str, log_path: str | None = None, tail: int = 20) -> str:
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


async def _notify_on_failure(title: str, desp: str) -> None:
    """Fire-and-forget notification. Reads sendkey from config; silently no-ops if not set."""
    cfg = _load_config()
    sendkey = (cfg.get("notify_serverchan_key") or "").strip()
    if not sendkey:
        return
    try:
        ok, msg = await asyncio.to_thread(_send_serverchan_sync, sendkey, title, desp)
        if not ok:
            print(f"[!] 通知发送失败: {msg}")
    except Exception as e:
        print(f"[!] 通知发送异常: {e}")


@control_router.post("/api/notify/serverchan")
async def set_notify_key(req: NotifyKeyRequest):
    cfg = _load_config()
    key = req.sendkey.strip()
    if key:
        cfg["notify_serverchan_key"] = key
        _save_config(cfg)
        return {"status": "ok", "message": "SendKey 已保存"}
    cfg.pop("notify_serverchan_key", None)
    _save_config(cfg)
    return {"status": "ok", "message": "SendKey 已清除"}


@control_router.get("/api/notify/serverchan/status")
async def notify_status():
    cfg = _load_config()
    return {"has_key": bool(cfg.get("notify_serverchan_key"))}


@control_router.post("/api/notify/test")
async def notify_test():
    cfg = _load_config()
    sendkey = (cfg.get("notify_serverchan_key") or "").strip()
    if not sendkey:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "未配置 SendKey"},
        )
    ok, msg = await asyncio.to_thread(
        _send_serverchan_sync, sendkey,
        "抖音聊天导出 · 测试通知",
        "如果你收到这条消息，说明 Server酱 配置正常。",
    )
    return {"status": "ok" if ok else "error", "message": msg}


# ── Media download toggle + backfill ──

class DownloadImagesToggle(BaseModel):
    enabled: bool


@control_router.get("/api/config/download-images")
async def get_download_images():
    return {"enabled": bool(_load_config().get("download_images"))}


@control_router.post("/api/config/download-images")
async def set_download_images(req: DownloadImagesToggle):
    cfg = _load_config()
    cfg["download_images"] = bool(req.enabled)
    _save_config(cfg)
    return {"status": "ok", "enabled": cfg["download_images"]}


@control_router.get("/api/media/backfill/status")
async def backfill_status():
    return {
        "status": _backfill_state["status"],
        "total": _backfill_state["total"],
        "done": _backfill_state["done"],
        "ok": _backfill_state["ok"],
        "failed": _backfill_state["failed"],
        "message": _backfill_state["message"],
        "started_at": _backfill_state["started_at"],
        "finished_at": _backfill_state["finished_at"],
    }


@control_router.post("/api/media/backfill")
async def backfill_start():
    if _backfill_state["status"] == "running":
        return JSONResponse({"error": "Backfill already running"}, status_code=409)
    asyncio.create_task(_run_backfill())
    return {"status": "started"}


async def _run_backfill():
    """Download all historical image/emoji media that has a URL but no local file.

    - 表情 (msg_type=2): 直接下载 media_url
    - 图片 (msg_type=3): 从 raw_data 取 origin_url + skey，AES-GCM 解密后保存
    """
    from extractor.web_scraper import _save_emoji, _save_image
    from backend.database import get_db

    _backfill_state.update({
        "status": "running", "total": 0, "done": 0, "ok": 0, "failed": 0,
        "message": "扫描数据库...", "started_at": time.time(), "finished_at": None,
    })
    try:
        media_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media")
        img_dir = os.path.join(media_root, "images")
        emoji_dir = os.path.join(media_root, "emoji")
        os.makedirs(img_dir, exist_ok=True)
        os.makedirs(emoji_dir, exist_ok=True)

        conn = get_db()
        rows = conn.execute(
            "SELECT msg_id, msg_type, media_url, raw_data FROM messages "
            "WHERE msg_type IN (2, 3) "
            "AND (media_local_path IS NULL OR media_local_path = '')"
        ).fetchall()
        _backfill_state["total"] = len(rows)
        _backfill_state["message"] = f"待下载 {len(rows)} 条"

        loop = asyncio.get_event_loop()
        for msg_id, msg_type, url, raw in rows:
            rel = None
            try:
                if msg_type == 2:
                    if url:
                        rel = await loop.run_in_executor(None, _save_emoji, url, emoji_dir)
                elif msg_type == 3:
                    try:
                        data = json.loads(raw) if raw else {}
                        cj = json.loads(data.get("content_json") or "{}")
                    except Exception:
                        cj = {}
                    ru = cj.get("resource_url") or {}
                    skey = ru.get("skey")
                    origin = (ru.get("origin_url_list") or [None])[0]
                    if skey and origin:
                        rel = await loop.run_in_executor(
                            None, _save_image, origin, skey, str(msg_id), img_dir,
                        )
                if rel:
                    conn.execute(
                        "UPDATE messages SET media_local_path = ? WHERE msg_id = ?",
                        (rel, msg_id),
                    )
                    conn.commit()
                    _backfill_state["ok"] += 1
                else:
                    _backfill_state["failed"] += 1
            except Exception:
                _backfill_state["failed"] += 1
            _backfill_state["done"] += 1
        conn.close()

        _backfill_state["status"] = "completed"
        _backfill_state["message"] = f"完成: 成功 {_backfill_state['ok']}，失败 {_backfill_state['failed']}"
    except Exception as e:
        _backfill_state["status"] = "failed"
        _backfill_state["message"] = f"错误: {e}"
    finally:
        _backfill_state["finished_at"] = time.time()


# ── 视频回填：调 batch_play_info 解析签名 URL 后落地 mp4 ──

@control_router.get("/api/media/videos/status")
async def video_backfill_status():
    return {
        "status": _video_backfill_state["status"],
        "total": _video_backfill_state["total"],
        "done": _video_backfill_state["done"],
        "ok": _video_backfill_state["ok"],
        "failed": _video_backfill_state["failed"],
        "skipped": _video_backfill_state["skipped"],
        "message": _video_backfill_state["message"],
        "started_at": _video_backfill_state["started_at"],
        "finished_at": _video_backfill_state["finished_at"],
    }


@control_router.get("/api/media/videos/pending")
async def video_backfill_pending():
    # Reuse the same Python filter as the backfill itself so the count matches
    # what will actually be processed (excludes text replies that quote a video).
    from extractor.video_downloader import pending_videos
    from backend.database import get_db
    conn = get_db()
    rows = pending_videos(conn)
    conn.close()
    return {"pending": len(rows)}


@control_router.post("/api/media/videos/backfill")
async def video_backfill_start():
    if _video_backfill_state["status"] == "running":
        return JSONResponse({"error": "video backfill already running"}, status_code=409)
    asyncio.create_task(_run_video_backfill())
    return {"status": "started"}


async def _run_video_backfill():
    from extractor.video_downloader import backfill as run_backfill
    _video_backfill_state.update({
        "status": "running", "total": 0, "done": 0, "ok": 0, "failed": 0,
        "skipped": 0, "message": "启动浏览器解析视频 URL...",
        "started_at": time.time(), "finished_at": None,
    })

    def _cb(p):
        _video_backfill_state["total"] = p.get("total", _video_backfill_state["total"])
        _video_backfill_state["ok"] = p.get("ok", 0)
        _video_backfill_state["failed"] = p.get("fail", 0)
        _video_backfill_state["skipped"] = p.get("skipped", 0)
        _video_backfill_state["done"] = (
            _video_backfill_state["ok"] + _video_backfill_state["failed"] + _video_backfill_state["skipped"]
        )
        cur = p.get("current", "")
        _video_backfill_state["message"] = (
            f"已下载 {_video_backfill_state['ok']}，失败 {_video_backfill_state['failed']}，"
            f"跳过 {_video_backfill_state['skipped']} / {_video_backfill_state['total']}（{cur[-12:] if cur else ''}）"
        )

    try:
        result = await run_backfill(progress_cb=_cb)
        _video_backfill_state["status"] = "completed"
        _video_backfill_state["message"] = (
            f"完成：成功 {result['ok']}，失败 {result['fail']}，跳过 {result['skipped']} / {result['total']}"
        )
    except Exception as e:
        _video_backfill_state["status"] = "failed"
        _video_backfill_state["message"] = f"错误: {e}"
    finally:
        _video_backfill_state["finished_at"] = time.time()


@control_router.get("", response_class=HTMLResponse)
@control_router.get("/", response_class=HTMLResponse)
async def panel_page():
    return PANEL_HTML


@control_router.get("/api/status")
async def panel_status():
    stats = database.get_stats()
    from backend.database import get_db
    conn = get_db()
    row = conn.execute("SELECT MAX(last_message_time) FROM conversations").fetchone()
    last_time = row[0] if row and row[0] else 0
    convs = conn.execute("SELECT name FROM conversations ORDER BY last_message_time DESC").fetchall()
    conn.close()

    cfg = _load_config()

    return {
        "conversations": stats["conversations"],
        "messages": stats["messages"],
        "users": stats["users"],
        "last_message_time": last_time,
        "conversation_names": [c[0] for c in convs if c[0]],
        "custom_filters": cfg.get("custom_filters", []),
        "scrape": {
            "status": _scrape_state["status"],
            "started_at": _scrape_state["started_at"],
            "finished_at": _scrape_state["finished_at"],
            "message": _scrape_state["message"],
        },
        "export": {
            "status": _export_state["status"],
            "file_path": _export_state["file_path"],
            "message": _export_state["message"],
        },
        "scheduler": {
            "enabled": _scheduler_state["enabled"],
            "schedule": _scheduler_state["schedule"],
            "next_run": _scheduler_state["next_run"],
        },
    }


@control_router.post("/api/scrape")
async def start_scrape(req: ScrapeRequest):
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "Scrape already running"}, status_code=409)

    probe = await _probe_login_state()
    if not probe["has_cookies"]:
        return JSONResponse(
            {"error": "未检测到登录态，请先扫码登录或导入 Cookie"},
            status_code=400,
        )

    # Selected conversations (checkbox list) take precedence over free-text filter
    effective_filter = ",".join(req.conversations) if req.conversations else req.filter

    cmd = [sys.executable, "-u", "extract.py"]
    if req.incremental:
        cmd.append("--incremental")
    if effective_filter:
        cmd.extend(["--filter", effective_filter])
    if _load_config().get("download_images"):
        cmd.append("--download-images")

    _scrape_state["status"] = "running"
    _scrape_state["started_at"] = time.time()
    _scrape_state["finished_at"] = None
    _scrape_state["message"] = f"{'增量' if req.incremental else '全量'}采集"
    if req.conversations:
        _scrape_state["message"] += f" ({len(req.conversations)} 个会话)"
    elif req.filter:
        _scrape_state["message"] += f" (过滤: {req.filter})"

    # Persist selection so it's remembered next time
    if req.conversations is not None:
        cfg = _load_config()
        cfg["scraper_selected"] = list(req.conversations)
        _save_config(cfg)

    asyncio.create_task(_run_scrape(cmd))
    return {"status": "started", "message": _scrape_state["message"]}


async def _run_scrape(cmd):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "w") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.dirname(os.path.dirname(__file__)),
            )
            _scrape_state["process"] = proc
            await proc.wait()

        if proc.returncode == 0:
            _scrape_state["status"] = "completed"
            _scrape_state["message"] = "采集完成"
        else:
            _scrape_state["status"] = "failed"
            _scrape_state["message"] = f"采集失败 (exit code {proc.returncode})"
    except Exception as e:
        _scrape_state["status"] = "failed"
        _scrape_state["message"] = f"采集错误: {e}"
    finally:
        _scrape_state["finished_at"] = time.time()
        _scrape_state["process"] = None
        if _scrape_state["status"] == "failed":
            asyncio.create_task(_notify_on_failure(
                "抖音聊天导出 · 采集失败",
                _build_failure_desp(_scrape_state["message"], LOG_PATH),
            ))


@control_router.get("/api/scrape/log")
async def scrape_log(lines: int = 50):
    if not os.path.exists(LOG_PATH):
        return {"log": ""}
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"log": "".join(tail)}
    except Exception:
        return {"log": ""}


@control_router.get("/api/conversations/refresh/log")
async def discover_log(lines: int = 80):
    if not os.path.exists(DISCOVER_LOG_PATH):
        return {"log": ""}
    try:
        with open(DISCOVER_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"log": "".join(tail)}
    except Exception:
        return {"log": ""}


@control_router.post("/api/scrape/stop")
async def stop_scrape():
    proc = _scrape_state.get("process")
    if proc and proc.returncode is None:
        proc.terminate()
        _scrape_state["status"] = "idle"
        _scrape_state["message"] = "已停止"
        return {"status": "stopped"}
    return {"status": "not_running"}


@control_router.post("/api/custom-filter")
async def manage_custom_filter(req: CustomFilterAction):
    cfg = _load_config()
    filters = cfg.get("custom_filters", [])
    if req.action == "add" and req.value and req.value not in filters:
        filters.append(req.value)
    elif req.action == "remove" and req.value in filters:
        filters.remove(req.value)
    cfg["custom_filters"] = filters
    _save_config(cfg)
    return {"custom_filters": filters}


# ── Conversation discovery / selection ────────────────────────────

def _read_conv_list():
    if not os.path.exists(CONV_LIST_PATH):
        return {"discovered_at": 0, "items": []}
    try:
        with open(CONV_LIST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"discovered_at": 0, "items": []}


_login_probe_lock = asyncio.Lock()


async def _probe_login_state() -> dict:
    """Single source of truth for whether the persistent profile is logged in.

    Always launches Chromium against `_USER_DATA_DIR` and reads cookies via
    Playwright. This intentionally goes through the same code path Chromium
    uses internally so we never disagree with what the actual scraper sees
    (whatever path the cookies DB lives at, WAL checkpoints, format
    migrations — all handled by Chromium itself).

    Returns one of:
        {"status": "logged_in",  "has_cookies": True}
        {"status": "expired",    "has_cookies": False}
        {"status": "no_profile", "has_cookies": False}
        {"status": "error",      "has_cookies": False, "message": "..."}

    Serialized via a module-level lock so the badge poll and the
    refresh/scrape preconditions can't race to launch two Chromium
    instances on the same profile (which would lock-conflict).
    """
    async with _login_probe_lock:
        has_profile = os.path.isdir(_USER_DATA_DIR) and os.listdir(_USER_DATA_DIR)
        if not has_profile:
            return {"status": "no_profile", "has_cookies": False}
        try:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            try:
                ctx = await pw.chromium.launch_persistent_context(
                    _USER_DATA_DIR, headless=True,
                    viewport={"width": 1400, "height": 900}, locale="zh-CN",
                    args=["--disable-blink-features=AutomationControlled"],
                )
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
                    await asyncio.sleep(2)
                    cookies = await ctx.cookies("https://www.douyin.com")
                    cookie_names = {c["name"] for c in cookies}
                    has_login = "sessionid" in cookie_names
                    return {
                        "status": "logged_in" if has_login else "expired",
                        "has_cookies": has_login,
                    }
                finally:
                    await ctx.close()
            finally:
                await pw.stop()
        except Exception as e:
            return {"status": "error", "has_cookies": False, "message": str(e)}


@control_router.post("/api/conversations/refresh")
async def refresh_conversations():
    """Run a lightweight scrape that only enumerates the conversation list."""
    if _discover_state["status"] == "running":
        return JSONResponse({"error": "Refresh already running"}, status_code=409)
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "Scraper is running — stop it first"}, status_code=409)

    # Pre-check: don't spawn the 3-minute browser wait if we already know
    # there's no usable session. Uses the same Playwright probe as the
    # login badge so the two never disagree.
    probe = await _probe_login_state()
    if not probe["has_cookies"]:
        return JSONResponse(
            {"error": "未检测到登录态，请先扫码登录或导入 Cookie"},
            status_code=400,
        )

    _discover_state["status"] = "running"
    _discover_state["message"] = "正在加载会话列表..."
    _discover_state["started_at"] = time.time()
    _discover_state["finished_at"] = None

    cmd = [sys.executable, "-u", "extract.py", "--list-conversations"]
    asyncio.create_task(_run_discover(cmd))
    return {"status": "started"}


async def _run_discover(cmd):
    proc = None
    try:
        os.makedirs(os.path.dirname(DISCOVER_LOG_PATH), exist_ok=True)
        with open(DISCOVER_LOG_PATH, "w") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.dirname(os.path.dirname(__file__)),
            )
            _discover_state["process"] = proc
            await proc.wait()

        if proc.returncode == 0:
            data = _read_conv_list()
            count = len(data.get("items", []))
            _discover_state["status"] = "completed"
            _discover_state["message"] = f"发现 {count} 个会话"
        elif proc.returncode == 2:
            _discover_state["status"] = "failed"
            _discover_state["message"] = "未检测到登录态，请先扫码或导入 Cookie"
        else:
            _discover_state["status"] = "failed"
            _discover_state["message"] = f"刷新失败 (exit {proc.returncode})"
    except Exception as e:
        _discover_state["status"] = "failed"
        _discover_state["message"] = f"刷新错误: {e}"
        # Best-effort: kill any lingering subprocess so it doesn't pin the state
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
    finally:
        _discover_state["finished_at"] = time.time()
        _discover_state["process"] = None
        # Defensive: ensure status is never left at "running" when this coroutine exits
        if _discover_state["status"] == "running":
            _discover_state["status"] = "failed"
            _discover_state["message"] = _discover_state["message"] or "刷新中断"


@control_router.get("/api/conversations/refresh/status")
async def refresh_status():
    data = _read_conv_list()
    return {
        "status": _discover_state["status"],
        "message": _discover_state["message"],
        "started_at": _discover_state["started_at"],
        "finished_at": _discover_state["finished_at"],
        "discovered_at": data.get("discovered_at", 0),
        "items": data.get("items", []),
    }


@control_router.post("/api/conversations/refresh/stop")
async def refresh_stop():
    proc = _discover_state.get("process")
    if proc and proc.returncode is None:
        proc.terminate()
        _discover_state["status"] = "idle"
        _discover_state["message"] = "已停止"
        return {"status": "stopped"}
    # No live process — if state is still "running", force-reset (was stuck)
    if _discover_state["status"] == "running":
        _discover_state["status"] = "idle"
        _discover_state["message"] = "已重置"
        _discover_state["finished_at"] = time.time()
        return {"status": "reset"}
    return {"status": "not_running"}


@control_router.get("/api/conversations/selected")
async def get_selected():
    cfg = _load_config()
    return {
        "scraper": cfg.get("scraper_selected", []),
        "export": cfg.get("export_selected", []),
        "schedule": cfg.get("schedule_selected", []),
    }


@control_router.post("/api/conversations/selected")
async def set_selected(req: SelectedUpdate):
    if req.section not in ("scraper", "export", "schedule"):
        return JSONResponse({"error": "invalid section"}, status_code=400)
    cfg = _load_config()
    cfg[f"{req.section}_selected"] = list(req.conversations)
    _save_config(cfg)
    return {"status": "ok", "selected": cfg[f"{req.section}_selected"]}


@control_router.post("/api/schedule")
async def set_schedule(req: ScheduleRequest):
    # Cancel existing scheduled task
    if _scheduler_state["task"] and not _scheduler_state["task"].done():
        _scheduler_state["task"].cancel()
        _scheduler_state["task"] = None

    _scheduler_state["enabled"] = req.enabled
    _scheduler_state["schedule"] = req.cron if req.enabled else ""
    _scheduler_state["next_run"] = None

    # Always persist the schedule selection so the cron loop + UI stay in sync
    cfg = _load_config()
    if req.conversations is not None:
        cfg["schedule_selected"] = list(req.conversations)

    if req.enabled and req.cron:
        parsed = _parse_cron(req.cron)
        if not parsed:
            return JSONResponse({"error": "无效的 cron 表达式（分 时 日 月 周）"}, status_code=400)

        next_run = _next_cron_run(parsed)
        _scheduler_state["next_run"] = next_run
        _scheduler_state["task"] = asyncio.create_task(
            _cron_loop(parsed, req.incremental)
        )
        cfg["schedule"] = req.cron
        _save_config(cfg)
        return {"status": "enabled", "cron": req.cron, "next_run": next_run}

    cfg["schedule"] = ""
    _save_config(cfg)
    return {"status": "disabled"}


def _parse_cron(expr: str) -> list | None:
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
            values = _expand_cron_field(field, lo, hi)
            if not values:
                return None
            result.append(values)
        except Exception:
            return None
    return result


def _expand_cron_field(field: str, lo: int, hi: int) -> set:
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


def _next_cron_run(parsed: list) -> float:
    """Find next datetime matching the cron fields."""
    from datetime import datetime, timedelta
    now = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=1)
    minutes, hours, days, months, dow = parsed
    # Search up to 366 days ahead
    for _ in range(366 * 24 * 60):
        if (now.month in months and now.day in days and
                now.hour in hours and now.minute in minutes and
                now.weekday() in _convert_dow(dow)):
            return now.timestamp()
        now += timedelta(minutes=1)
    return time.time() + 86400  # fallback: 1 day


def _convert_dow(cron_dow: set) -> set:
    """Convert cron day-of-week (0=Sun) to Python weekday (0=Mon)."""
    mapping = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
    return {mapping.get(d, d) for d in cron_dow}


async def _cron_loop(parsed: list, incremental: bool):
    """Run scrape on cron schedule."""
    try:
        while True:
            next_run = _next_cron_run(parsed)
            _scheduler_state["next_run"] = next_run
            wait_secs = next_run - time.time()
            if wait_secs > 0:
                await asyncio.sleep(wait_secs)
            if _scrape_state["status"] != "running":
                cmd = [sys.executable, "-u", "extract.py"]
                if incremental:
                    cmd.append("--incremental")
                cfg = _load_config()
                if cfg.get("download_images"):
                    cmd.append("--download-images")
                # Preferred: schedule_selected (checkbox picks).
                # Fallback: custom_filters (legacy).
                # Fallback: all DB conversations (scrape everything we know).
                filters = cfg.get("schedule_selected") or cfg.get("custom_filters") or []
                if not filters:
                    from backend.database import get_db
                    conn = get_db()
                    convs = conn.execute("SELECT name FROM conversations WHERE name IS NOT NULL AND name != ''").fetchall()
                    conn.close()
                    filters = [c[0] for c in convs]
                if filters:
                    cmd.extend(["--filter", ",".join(filters)])
                _scrape_state["status"] = "running"
                _scrape_state["started_at"] = time.time()
                _scrape_state["finished_at"] = None
                filter_desc = f" (过滤: {','.join(filters[:5])}{'...' if len(filters) > 5 else ''})" if filters else " (全部会话)"
                _scrape_state["message"] = f"定时{'增量' if incremental else '全量'}采集{filter_desc}"
                await _run_scrape(cmd)
            # Wait at least 61 seconds to avoid re-trigger in same minute
            await asyncio.sleep(61)
    except asyncio.CancelledError:
        pass


@control_router.post("/api/export")
async def start_export(req: ExportRequest):
    if _export_state["status"] == "running":
        return JSONResponse({"error": "Export already running"}, status_code=409)

    _export_state["status"] = "running"
    _export_state["message"] = "正在导出..."

    # Persist selection
    if req.conversations is not None:
        cfg = _load_config()
        cfg["export_selected"] = list(req.conversations)
        _save_config(cfg)

    convs = list(req.conversations) if req.conversations else None
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _do_export, req.format, req.filter, convs)
    return {
        "status": _export_state["status"],
        "message": _export_state["message"],
        "file_path": _export_state["file_path"],
    }


def _do_export(fmt: str, filter_name: str, conversations: list | None):
    try:
        from extractor.exporter import ChatLabExporter
        import re
        import zipfile

        ext = ".json" if fmt == "json" else ".jsonl"
        data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

        # Decide targets
        if conversations:
            targets = conversations
        elif filter_name:
            targets = [filter_name]
        else:
            targets = [None]  # None = exporter picks latest

        if len(targets) <= 1:
            # Single file
            output_path = os.path.join(data_dir, f"export{ext}")
            # Remove stale file so a "conv not found" early-return doesn't look like success
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception:
                    pass
            exporter = ChatLabExporter(conv_name=targets[0] or None, output_format=fmt)
            exporter.export(output_path)
            if not os.path.exists(output_path):
                raise RuntimeError(f"未找到会话: {targets[0] or '(any)'}")
            _export_state["file_path"] = f"export{ext}"
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            _export_state["message"] = f"导出完成 ({size_mb:.1f} MB)"
        else:
            # Multiple → bundle into a zip
            tmp_dir = os.path.join(data_dir, "export_tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            # Clear old tmp files
            for fn in os.listdir(tmp_dir):
                try:
                    os.remove(os.path.join(tmp_dir, fn))
                except Exception:
                    pass

            produced = []
            for name in targets:
                safe = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", name)[:80] or "conv"
                path = os.path.join(tmp_dir, f"{safe}{ext}")
                try:
                    ChatLabExporter(conv_name=name, output_format=fmt).export(path)
                    if os.path.exists(path):
                        produced.append((name, path))
                except Exception as e:
                    print(f"[-] 导出 {name} 失败: {e}")

            if not produced:
                raise RuntimeError("没有成功导出的会话")

            zip_path = os.path.join(data_dir, "export.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for _, path in produced:
                    zf.write(path, arcname=os.path.basename(path))

            _export_state["file_path"] = "export.zip"
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            _export_state["message"] = f"导出完成 ({len(produced)} 个会话, {size_mb:.1f} MB)"

        _export_state["status"] = "completed"
    except Exception as e:
        _export_state["status"] = "failed"
        _export_state["message"] = f"导出失败: {e}"


@control_router.get("/api/export/download")
async def download_export():
    if not _export_state["file_path"]:
        return JSONResponse({"error": "No export file"}, status_code=404)
    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", _export_state["file_path"]
    )
    if not os.path.exists(path):
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(path, filename=_export_state["file_path"])


# ── Login (in-container headless with screenshot) ──

import base64

_USER_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "browser_profile")

_login_state = {
    "status": "idle",  # idle | starting | waiting_scan | logged_in | failed
    "screenshot": None,  # base64 png
    "message": "",
    "countdown": 0,
    "_context": None,
    "_pw": None,
}


@control_router.get("/api/login/check")
async def login_check():
    """Check login by actually opening browser and reading cookies."""
    return await _probe_login_state()


@control_router.post("/api/login/start")
async def login_start():
    if _login_state["status"] in ("starting", "waiting_scan"):
        return JSONResponse({"error": "已在登录流程中"}, status_code=409)
    # If scraper is running, reject
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "请先停止采集再登录"}, status_code=409)

    _login_state["status"] = "starting"
    _login_state["screenshot"] = None
    _login_state["message"] = "正在启动浏览器..."
    asyncio.create_task(_login_flow())
    return {"status": "started"}


@control_router.get("/api/login/status")
async def login_status():
    return {
        "status": _login_state["status"],
        "screenshot": _login_state["screenshot"],
        "message": _login_state["message"],
        "countdown": _login_state["countdown"],
    }


class MouseAction(BaseModel):
    action: str  # click, mousedown, mousemove, mouseup
    x: float
    y: float


class KeyAction(BaseModel):
    action: str  # press, type
    key: str = ""
    text: str = ""


@control_router.post("/api/login/mouse")
async def login_mouse(req: MouseAction):
    """Forward mouse events to the headless browser page."""
    ctx = _login_state.get("_context")
    if not ctx or _login_state["status"] not in ("waiting_scan",):
        return JSONResponse({"error": "No active login session"}, status_code=400)

    try:
        page = ctx.pages[0] if ctx.pages else None
        if not page:
            return JSONResponse({"error": "No page"}, status_code=400)

        mouse = page.mouse
        if req.action == "click":
            await mouse.click(req.x, req.y)
        elif req.action == "mousedown":
            await mouse.move(req.x, req.y)
            await mouse.down()
        elif req.action == "mousemove":
            await mouse.move(req.x, req.y)
        elif req.action == "mouseup":
            await mouse.up()
        else:
            return JSONResponse({"error": f"Unknown action: {req.action}"}, status_code=400)

        # Take a fresh screenshot after interaction
        await asyncio.sleep(0.15)
        png = await page.screenshot(type="png")
        _login_state["screenshot"] = base64.b64encode(png).decode()

        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@control_router.post("/api/login/keyboard")
async def login_keyboard(req: KeyAction):
    """Forward keyboard events to the headless browser page."""
    ctx = _login_state.get("_context")
    if not ctx or _login_state["status"] not in ("waiting_scan",):
        return JSONResponse({"error": "No active login session"}, status_code=400)

    try:
        page = ctx.pages[0] if ctx.pages else None
        if not page:
            return JSONResponse({"error": "No page"}, status_code=400)

        kb = page.keyboard
        if req.action == "type" and req.text:
            await kb.type(req.text)
        elif req.action == "press" and req.key:
            await kb.press(req.key)
        else:
            return JSONResponse({"error": "Invalid keyboard action"}, status_code=400)

        await asyncio.sleep(0.15)
        png = await page.screenshot(type="png")
        _login_state["screenshot"] = base64.b64encode(png).decode()
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@control_router.post("/api/login/cancel")
async def login_cancel():
    await _login_cleanup()
    _login_state["status"] = "idle"
    _login_state["message"] = "已取消"
    _login_state["screenshot"] = None
    return {"status": "cancelled"}


@control_router.post("/api/login/clear")
async def login_clear():
    """Clear browser profile to force re-login."""
    import shutil
    if os.path.isdir(_USER_DATA_DIR):
        shutil.rmtree(_USER_DATA_DIR, ignore_errors=True)
    return {"status": "cleared"}


def _validate_cookie_entries(parsed: list[dict]) -> tuple[list[str], list[str]]:
    """Pre-flight check on parsed cookies. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    sids = [c for c in parsed if c["name"] == "sessionid"]
    if not sids:
        errors.append("Cookie 中未包含 sessionid，请确保已登录后再导出（cookie-editor 需全选导出）")
        return errors, warnings

    sid = sids[0]
    value = (sid.get("value") or "").strip()
    if not value:
        errors.append("sessionid 的值为空")
    elif len(value) < 16:
        warnings.append(f"sessionid 长度异常 ({len(value)} 字节)，可能被截断")

    domain = (sid.get("domain") or "").lstrip(".")
    if domain and domain != "douyin.com" and not domain.endswith(".douyin.com"):
        errors.append(
            f"sessionid 的 domain 是 .{domain}（应为 .douyin.com）"
            "—— 可能在子站点（iesdouyin.com 等）导出了，请回到 www.douyin.com 重导"
        )

    exp = sid.get("expires")
    if exp and exp > 0 and exp < time.time():
        errors.append("sessionid 已过期（expirationDate 在过去），请重新登录后再导出")

    if len(parsed) < 3:
        warnings.append(
            f"只解析出 {len(parsed)} 个 cookie，抖音通常需要 10+ 个才能完整工作，"
            "建议在 cookie-editor 里全选后再导出"
        )
    return errors, warnings


@control_router.post("/api/login/cookie-import")
async def login_cookie_import(req: CookieImportRequest):
    """Import cookies from browser DevTools or document.cookie string."""
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "采集进行中，请先停止"}, status_code=409)
    if _login_state["status"] in ("starting", "waiting_scan"):
        return JSONResponse({"error": "登录流程进行中，请先取消"}, status_code=409)

    raw = req.cookies.strip()
    if not raw:
        return JSONResponse({"error": "Cookie 数据为空"}, status_code=400)

    parsed: list[dict] = []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            for c in data:
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                entry: dict = {
                    "name": c["name"],
                    "value": str(c.get("value", "")),
                    "domain": c.get("domain", ".douyin.com"),
                    "path": c.get("path", "/"),
                }
                exp = c.get("expirationDate") or c.get("expires")
                if exp:
                    entry["expires"] = float(exp)
                if c.get("httpOnly") is not None:
                    entry["httpOnly"] = bool(c["httpOnly"])
                if c.get("secure") is not None:
                    entry["secure"] = bool(c["secure"])
                # cookie-editor exports sameSite as lowercase enum.
                # Map "no_restriction" → "None" (cross-site allowed) — must NOT downgrade to Lax,
                # since some Douyin auth cookies require cross-site delivery for IM API calls.
                ss = (c.get("sameSite") or "").strip().lower()
                ss_map = {"no_restriction": "None", "none": "None",
                          "lax": "Lax", "strict": "Strict"}
                if ss in ss_map:
                    entry["sameSite"] = ss_map[ss]
                    # Playwright requires Secure=true when SameSite=None
                    if entry["sameSite"] == "None":
                        entry["secure"] = True
                parsed.append(entry)
        else:
            return JSONResponse({"error": "JSON 格式需为数组"}, status_code=400)
    except (json.JSONDecodeError, ValueError):
        for pair in raw.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            parsed.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".douyin.com",
                "path": "/",
            })

    if not parsed:
        return JSONResponse({"error": "未能解析出任何 Cookie"}, status_code=400)

    errors, warnings = _validate_cookie_entries(parsed)
    if errors:
        return JSONResponse({"error": "；".join(errors)}, status_code=400)

    # Session cookies (no expirationDate) get dropped on browser restart,
    # so the next login probe wouldn't see them. Pin a 30-day default.
    default_exp = time.time() + 30 * 86400
    for c in parsed:
        if "expires" not in c:
            c["expires"] = default_exp

    try:
        from playwright.async_api import async_playwright
        os.makedirs(_USER_DATA_DIR, exist_ok=True)
        pw = await async_playwright().start()
        ctx = await pw.chromium.launch_persistent_context(
            _USER_DATA_DIR,
            headless=True,
            viewport={"width": 1400, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        await asyncio.sleep(1)
        await ctx.add_cookies(parsed)
        cookies = await ctx.cookies("https://www.douyin.com")
        ok = "sessionid" in {c["name"] for c in cookies}
        all_cookies = await ctx.cookies()  # everything regardless of url, for diagnostics
        await ctx.close()
        await pw.stop()
        if ok:
            msg = f"成功导入 {len(parsed)} 个 Cookie"
            if warnings:
                msg += "（注意：" + "；".join(warnings) + "）"
            return {"status": "ok", "message": msg, "count": len(parsed),
                    "warnings": warnings}
        # Verification failed — diagnose why so the user knows what to fix.
        sid_other = [c for c in all_cookies if c["name"] == "sessionid"]
        if sid_other:
            wrong_domain = sid_other[0].get("domain", "?")
            return JSONResponse(
                {"error": f"sessionid 被加载到 domain={wrong_domain}，"
                          f"对 www.douyin.com 不生效。请确认 cookie 的 domain 是 .douyin.com"},
                status_code=400,
            )
        return JSONResponse(
            {"error": "sessionid 导入后无法在 douyin.com 读取到，"
                      "可能已被服务端注销，请重新登录后再导出"},
            status_code=400,
        )
    except Exception as e:
        return JSONResponse({"error": f"导入失败: {e}"}, status_code=500)


async def _login_cleanup():
    try:
        if _login_state["_context"]:
            await _login_state["_context"].close()
    except Exception:
        pass
    try:
        if _login_state["_pw"]:
            await _login_state["_pw"].stop()
    except Exception:
        pass
    _login_state["_context"] = None
    _login_state["_pw"] = None


async def _login_flow():
    """In-container: open headless browser, screenshot the page for QR scanning."""
    try:
        from playwright.async_api import async_playwright

        os.makedirs(_USER_DATA_DIR, exist_ok=True)
        pw = await async_playwright().start()
        _login_state["_pw"] = pw

        ctx = await pw.chromium.launch_persistent_context(
            _USER_DATA_DIR,
            headless=True,
            viewport={"width": 1400, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        _login_state["_context"] = ctx
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Navigate to Douyin
        _login_state["message"] = "正在打开抖音..."
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        # Check if already logged in
        cookies = await ctx.cookies("https://www.douyin.com")
        cookie_names = {c["name"] for c in cookies}
        if "sessionid" in cookie_names:
            _login_state["status"] = "logged_in"
            _login_state["message"] = "已登录，无需扫码"
            await _login_cleanup()
            return

        # Try to click login button
        _login_state["status"] = "waiting_scan"
        _login_state["message"] = "正在获取二维码..."
        try:
            login_btn = await page.wait_for_selector(
                'button:has-text("登录")', timeout=5000
            )
            if login_btn:
                await login_btn.click()
                await asyncio.sleep(2)
        except Exception:
            pass

        # Poll: take screenshots and check cookies
        timeout_secs = 180
        for i in range(timeout_secs):
            if _login_state["status"] != "waiting_scan":
                break  # cancelled

            _login_state["countdown"] = timeout_secs - i

            # Screenshot
            png = await page.screenshot(type="png")
            _login_state["screenshot"] = base64.b64encode(png).decode()
            _login_state["message"] = f"请用抖音 APP 扫码 ({timeout_secs - i}s)"

            # Check login
            cookies = await ctx.cookies("https://www.douyin.com")
            cookie_names = {c["name"] for c in cookies}
            if "sessionid" in cookie_names:
                _login_state["status"] = "logged_in"
                _login_state["message"] = "登录成功！"
                _login_state["screenshot"] = None
                await _login_cleanup()
                return

            await asyncio.sleep(1)

        if _login_state["status"] == "waiting_scan":
            _login_state["status"] = "failed"
            _login_state["message"] = "扫码超时（3 分钟）"

    except Exception as e:
        _login_state["status"] = "failed"
        _login_state["message"] = f"登录错误: {e}"
    finally:
        await _login_cleanup()
