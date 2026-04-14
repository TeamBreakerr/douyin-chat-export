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

# ── Persistent config (saved to data/panel_config.json) ──
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "panel_config.json")


def _load_config():
    if os.path.exists(_CONFIG_PATH):
        with open(_CONFIG_PATH) as f:
            return json.load(f)
    return {"custom_filters": [], "schedule": ""}


def _save_config(cfg):
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, ensure_ascii=False)


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

    # Selected conversations (checkbox list) take precedence over free-text filter
    effective_filter = ",".join(req.conversations) if req.conversations else req.filter

    cmd = [sys.executable, "-u", "extract.py"]
    if req.incremental:
        cmd.append("--incremental")
    if effective_filter:
        cmd.extend(["--filter", effective_filter])

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


@control_router.get("/api/scrape/log")
async def scrape_log(lines: int = 50):
    if not os.path.exists(LOG_PATH):
        return {"log": ""}
    try:
        with open(LOG_PATH, "r", errors="replace") as f:
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


@control_router.post("/api/conversations/refresh")
async def refresh_conversations():
    """Run a lightweight scrape that only enumerates the conversation list."""
    if _discover_state["status"] == "running":
        return JSONResponse({"error": "Refresh already running"}, status_code=409)
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "Scraper is running — stop it first"}, status_code=409)

    _discover_state["status"] = "running"
    _discover_state["message"] = "正在加载会话列表..."
    _discover_state["started_at"] = time.time()
    _discover_state["finished_at"] = None

    cmd = [sys.executable, "-u", "extract.py", "--list-conversations"]
    asyncio.create_task(_run_discover(cmd))
    return {"status": "started"}


async def _run_discover(cmd):
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
        else:
            _discover_state["status"] = "failed"
            _discover_state["message"] = f"刷新失败 (exit {proc.returncode})"
    except Exception as e:
        _discover_state["status"] = "failed"
        _discover_state["message"] = f"刷新错误: {e}"
    finally:
        _discover_state["finished_at"] = time.time()
        _discover_state["process"] = None


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
    has_profile = os.path.isdir(_USER_DATA_DIR) and os.listdir(_USER_DATA_DIR)
    if not has_profile:
        return {"status": "no_profile", "has_cookies": False}

    # Quick cookie check via Playwright
    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        ctx = await pw.chromium.launch_persistent_context(
            _USER_DATA_DIR, headless=True,
            viewport={"width": 1400, "height": 900}, locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        cookies = await ctx.cookies("https://www.douyin.com")
        cookie_names = {c["name"] for c in cookies}
        has_login = "sessionid" in cookie_names
        await ctx.close()
        await pw.stop()
        return {"status": "logged_in" if has_login else "expired", "has_cookies": has_login}
    except Exception as e:
        return {"status": "error", "has_cookies": False, "message": str(e)}


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


# ── Inline HTML ──

PANEL_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<title>Control Panel - 抖音聊天记录</title>
<style>
/* ── Theme variables ── */
[data-theme="dark"] {
  --bg:       #0d1117;
  --bg2:      #161b22;
  --bg3:      #21262d;
  --surface:  #1c2128;
  --accent:   #58a6ff;
  --accent2:  #79c0ff;
  --accent-bg:rgba(56,139,253,0.1);
  --text:     #c9d1d9;
  --text2:    #8b949e;
  --text3:    #6e7681;
  --border:   #30363d;
  --green:    #3fb950;
  --green-bg: rgba(63,185,80,0.12);
  --red:      #f85149;
  --red-bg:   rgba(248,81,73,0.12);
  --yellow:   #d29922;
  --yellow-bg:rgba(210,153,34,0.12);
  --shadow:   0 1px 3px rgba(0,0,0,0.3);
}
[data-theme="light"] {
  --bg:       #f6f8fa;
  --bg2:      #ffffff;
  --bg3:      #f0f2f5;
  --surface:  #ffffff;
  --accent:   #0969da;
  --accent2:  #0550ae;
  --accent-bg:rgba(9,105,218,0.08);
  --text:     #24292f;
  --text2:    #57606a;
  --text3:    #8c959f;
  --border:   #d0d7de;
  --green:    #1a7f37;
  --green-bg: rgba(26,127,55,0.08);
  --red:      #cf222e;
  --red-bg:   rgba(207,34,46,0.08);
  --yellow:   #9a6700;
  --yellow-bg:rgba(154,103,0,0.08);
  --shadow:   0 1px 3px rgba(31,35,40,0.08);
}
[data-theme="ocean"] {
  --bg:       #0b1929;
  --bg2:      #0f2744;
  --bg3:      #163561;
  --surface:  #122a4b;
  --accent:   #5eb1ef;
  --accent2:  #90caf9;
  --accent-bg:rgba(94,177,239,0.1);
  --text:     #d4e4f7;
  --text2:    #8eacc5;
  --text3:    #5d7d96;
  --border:   #1e3a5f;
  --green:    #4caf50;
  --green-bg: rgba(76,175,80,0.12);
  --red:      #ef5350;
  --red-bg:   rgba(239,83,80,0.12);
  --yellow:   #ffa726;
  --yellow-bg:rgba(255,167,38,0.12);
  --shadow:   0 1px 3px rgba(0,0,0,0.4);
}
[data-theme="purple"] {
  --bg:       #13111c;
  --bg2:      #1c1828;
  --bg3:      #2a2438;
  --surface:  #211c30;
  --accent:   #bb86fc;
  --accent2:  #d4b0ff;
  --accent-bg:rgba(187,134,252,0.1);
  --text:     #e2daf0;
  --text2:    #9e8fba;
  --text3:    #6e5f8a;
  --border:   #332d44;
  --green:    #66bb6a;
  --green-bg: rgba(102,187,106,0.12);
  --red:      #ef5350;
  --red-bg:   rgba(239,83,80,0.12);
  --yellow:   #ffc107;
  --yellow-bg:rgba(255,193,7,0.12);
  --shadow:   0 1px 3px rgba(0,0,0,0.4);
}

* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans SC', sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  transition: background 0.3s, color 0.3s;
}
.container { max-width: 860px; margin: 0 auto; padding: 32px 20px; }

/* ── Header ── */
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 28px;
}
.header h1 {
  font-size: 20px;
  font-weight: 600;
  color: var(--text);
  letter-spacing: -0.3px;
}
.header-right { display: flex; align-items: center; gap: 12px; }

/* Theme switcher */
.theme-switcher {
  display: flex;
  gap: 6px;
  padding: 4px;
  background: var(--bg3);
  border-radius: 8px;
  border: 1px solid var(--border);
}
.theme-dot {
  width: 22px; height: 22px;
  border-radius: 6px;
  border: 2px solid transparent;
  cursor: pointer;
  transition: all 0.15s;
}
.theme-dot:hover { transform: scale(1.15); }
.theme-dot.active { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-bg); }
.theme-dot[data-t="dark"]   { background: #0d1117; }
.theme-dot[data-t="light"]  { background: #f6f8fa; border-color: #d0d7de; }
.theme-dot[data-t="light"].active { border-color: #0969da; }
.theme-dot[data-t="ocean"]  { background: linear-gradient(135deg, #0b1929, #163561); }
.theme-dot[data-t="purple"] { background: linear-gradient(135deg, #13111c, #2a2438); }

.viewer-btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 7px 16px; border-radius: 8px; font-size: 13px; font-weight: 500;
  color: var(--accent); background: var(--accent-bg); border: 1px solid var(--border);
  text-decoration: none; transition: all 0.15s;
}
.viewer-btn:hover { background: var(--accent); color: #fff; }

/* ── Stats cards ── */
.cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 24px; }
.card {
  background: var(--bg2); border-radius: 12px; padding: 20px;
  text-align: center; border: 1px solid var(--border);
  box-shadow: var(--shadow); transition: transform 0.15s, box-shadow 0.15s;
}
.card:hover { transform: translateY(-2px); box-shadow: var(--shadow), 0 4px 12px rgba(0,0,0,0.1); }
.card .num {
  font-size: 30px; font-weight: 700; color: var(--accent);
  font-variant-numeric: tabular-nums;
}
.card .label { font-size: 12px; color: var(--text2); margin-top: 6px; text-transform: uppercase; letter-spacing: 0.5px; }

/* ── Sections ── */
.section {
  background: var(--bg2); border-radius: 12px; padding: 22px;
  margin-bottom: 16px; border: 1px solid var(--border); box-shadow: var(--shadow);
}
.section h2 {
  font-size: 14px; font-weight: 600; margin-bottom: 14px;
  display: flex; align-items: center; gap: 10px;
  text-transform: uppercase; letter-spacing: 0.3px; color: var(--text2);
}
.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.row + .row { margin-top: 10px; }

/* ── Form controls ── */
select, input[type=text] {
  background: var(--bg3); border: 1px solid var(--border); color: var(--text);
  padding: 8px 12px; border-radius: 8px; font-size: 13px; outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}
select:focus, input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-bg); }

.btn {
  padding: 8px 18px; border: none; border-radius: 8px; font-size: 13px;
  cursor: pointer; font-weight: 500; transition: all 0.15s;
  display: inline-flex; align-items: center; gap: 6px;
}
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { filter: brightness(1.1); }
.btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-danger { background: var(--red); color: #fff; }
.btn-danger:hover { filter: brightness(1.1); }
.btn-success { background: var(--green); color: #fff; text-decoration: none; }
.btn-success:hover { filter: brightness(1.1); }
.btn-outline {
  background: transparent; color: var(--text2); border: 1px solid var(--border);
}
.btn-outline:hover { border-color: var(--accent); color: var(--accent); }
.btn-sm { padding: 4px 10px; font-size: 12px; }

/* ── Status badge ── */
.status {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 10px; border-radius: 20px; font-size: 11px;
  font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px;
}
.status::before {
  content: ''; width: 6px; height: 6px; border-radius: 50%;
}
.status-idle { background: var(--bg3); color: var(--text3); }
.status-idle::before { background: var(--text3); }
.status-running { background: var(--yellow-bg); color: var(--yellow); }
.status-running::before { background: var(--yellow); animation: pulse 1.5s infinite; }
.status-completed { background: var(--green-bg); color: var(--green); }
.status-completed::before { background: var(--green); }
.status-failed { background: var(--red-bg); color: var(--red); }
.status-failed::before { background: var(--red); }

@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

/* ── Log box ── */
.log-box {
  background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px; margin-top: 14px; max-height: 220px; overflow-y: auto;
  font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
  font-size: 12px; line-height: 1.6; color: var(--text2);
  white-space: pre-wrap; word-break: break-all; display: none;
}
.log-box.show { display: block; }
.log-box::-webkit-scrollbar { width: 6px; }
.log-box::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

/* ── Meta info ── */
.meta { font-size: 12px; color: var(--text3); margin-top: 8px; }

/* ── Custom filter chips ── */
.chip-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 10px; border-radius: 20px; font-size: 12px;
  background: var(--accent-bg); color: var(--accent); border: 1px solid var(--border);
}
.chip .remove {
  cursor: pointer; opacity: 0.6; font-size: 14px; line-height: 1;
  margin-left: 2px;
}
.chip .remove:hover { opacity: 1; color: var(--red); }

/* ── Schedule section ── */
.schedule-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.schedule-row input[type=text] { width: 80px; text-align: center; }
.cron-preset {
  font-size: 11px; color: var(--accent); cursor: pointer;
  padding: 2px 8px; border-radius: 4px; background: var(--accent-bg);
  border: 1px solid var(--border); transition: all 0.15s;
}
.cron-preset:hover { background: var(--accent); color: #fff; }

/* ── Conversation checkbox list ── */
.conv-list-box {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 6px 10px;
  max-height: 220px; overflow-y: auto;
  padding: 10px;
  background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
  margin-top: 8px;
}
.conv-list-box::-webkit-scrollbar { width: 6px; }
.conv-list-box::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.conv-check {
  display: flex; align-items: center; gap: 6px;
  font-size: 13px; color: var(--text); cursor: pointer;
  padding: 3px 4px; border-radius: 4px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.conv-check:hover { background: var(--bg3); }
.conv-check input[type=checkbox] {
  accent-color: var(--accent); cursor: pointer; flex-shrink: 0;
}
.conv-list-empty {
  padding: 14px; text-align: center; font-size: 12px;
  color: var(--text3);
  grid-column: 1 / -1;
}
.conv-list-toolbar {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: 12px; color: var(--text2);
}
.conv-list-toolbar .sep { color: var(--text3); }

/* ── Language switcher ── */
.lang-switcher {
  display: inline-flex; gap: 2px;
  padding: 3px; background: var(--bg3);
  border-radius: 8px; border: 1px solid var(--border);
  font-size: 12px;
}
.lang-switcher button {
  background: transparent; border: none; color: var(--text2);
  padding: 3px 10px; border-radius: 5px; cursor: pointer;
  font-size: 12px; font-weight: 500;
}
.lang-switcher button.active {
  background: var(--accent); color: #fff;
}

.switch {
  position: relative; width: 38px; height: 22px; cursor: pointer;
}
.switch input { display: none; }
.switch .slider {
  position: absolute; inset: 0; background: var(--bg3); border-radius: 22px;
  border: 1px solid var(--border); transition: all 0.2s;
}
.switch .slider::before {
  content: ''; position: absolute; left: 2px; top: 2px;
  width: 16px; height: 16px; border-radius: 50%;
  background: var(--text3); transition: all 0.2s;
}
.switch input:checked + .slider { background: var(--accent-bg); border-color: var(--accent); }
.switch input:checked + .slider::before { transform: translateX(16px); background: var(--accent); }

/* ── Toggle (incr/full) ── */
.toggle {
  display: flex; border-radius: 8px; overflow: hidden;
  border: 1px solid var(--border); background: var(--bg3);
}
.toggle label {
  padding: 7px 16px; font-size: 12px; cursor: pointer;
  transition: all 0.15s; color: var(--text2); font-weight: 500;
}
.toggle input { display: none; }
.toggle input:checked + label { background: var(--accent); color: #fff; }

/* ── Responsive ── */
@media (max-width: 600px) {
  .container { padding: 16px 12px; }
  .cards { grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .card { padding: 14px 8px; }
  .card .num { font-size: 22px; }
  .header { flex-wrap: wrap; gap: 10px; }
}
</style>
</head>
<body data-theme="dark">
<div id="loginOverlay" style="display:none;position:fixed;inset:0;z-index:9999;background:var(--bg);display:flex;align-items:center;justify-content:center;">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:32px;width:320px;box-shadow:var(--shadow);text-align:center;">
    <h2 style="margin:0 0 8px;color:var(--text);" data-i18n="panelTitle">控制面板</h2>
    <p style="margin:0 0 20px;color:var(--text2);font-size:14px;" data-i18n="enterPasswordPrompt">请输入密码</p>
    <input id="panelPwInput" type="password" data-i18n-placeholder="password" placeholder="密码" onkeydown="if(event.key==='Enter')panelLogin()"
      style="width:100%;box-sizing:border-box;padding:10px 14px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);font-size:15px;margin-bottom:12px;outline:none;">
    <button onclick="panelLogin()" style="width:100%;padding:10px;border:none;border-radius:8px;background:var(--accent);color:#fff;font-size:15px;cursor:pointer;" data-i18n="loginBtn">登录</button>
    <div id="panelLoginErr" style="margin-top:10px;color:var(--red);font-size:13px;"></div>
  </div>
</div>
<div class="container" id="mainContainer" style="display:none;">

  <!-- Header -->
  <div class="header">
    <h1 data-i18n="appTitle">抖音聊天记录导出</h1>
    <div class="header-right">
      <div class="lang-switcher">
        <button data-lang="zh" onclick="setLang('zh')">中</button>
        <button data-lang="en" onclick="setLang('en')">EN</button>
      </div>
      <div class="theme-switcher">
        <div class="theme-dot active" data-t="dark" title="Dark" onclick="setTheme('dark')"></div>
        <div class="theme-dot" data-t="light" title="Light" onclick="setTheme('light')"></div>
        <div class="theme-dot" data-t="ocean" title="Ocean" onclick="setTheme('ocean')"></div>
        <div class="theme-dot" data-t="purple" title="Purple" onclick="setTheme('purple')"></div>
      </div>
      <a href="/" class="viewer-btn" data-i18n="chatViewer">聊天查看器 &rarr;</a>
    </div>
  </div>

  <!-- Stats -->
  <div class="cards">
    <div class="card"><div class="num" id="convCount">-</div><div class="label" data-i18n="statConversations">会话</div></div>
    <div class="card"><div class="num" id="msgCount">-</div><div class="label" data-i18n="statMessages">消息</div></div>
    <div class="card"><div class="num" id="userCount">-</div><div class="label" data-i18n="statUsers">用户</div></div>
  </div>

  <!-- Login -->
  <div class="section" id="loginSection">
    <h2><span data-i18n="loginTitle">登录</span> <span class="status status-idle" id="loginStatus" data-i18n="checking">检测中...</span></h2>
    <div id="loginInfo" class="meta" style="margin-bottom:10px"></div>
    <div class="row">
      <button class="btn btn-primary" id="loginBtn" onclick="startLogin()" data-i18n="scanQr">扫码登录</button>
      <button class="btn btn-danger" id="loginCancelBtn" onclick="cancelLogin()" style="display:none" data-i18n="cancel">取消</button>
      <button class="btn btn-outline btn-sm" onclick="clearLogin()" data-i18n="clearSession">清除会话</button>
    </div>
    <div id="loginScreenshot" style="display:none;margin-top:14px;position:relative;user-select:none;">
      <div style="font-size:11px;color:var(--text3);margin-bottom:6px;" data-i18n="clickScreenshotHint">点击截图进行交互。下方输入文本后回车发送。</div>
      <div style="text-align:center;">
        <img id="loginImg" style="max-width:100%;border-radius:8px;border:1px solid var(--border);cursor:crosshair;"
             onmousedown="imgMouseDown(event)" onmousemove="imgMouseMove(event)" onmouseup="imgMouseUp(event)"
             ondragstart="return false" />
      </div>
      <div style="display:flex;gap:8px;margin-top:10px;align-items:center;">
        <input type="text" id="loginKeyInput" data-i18n-placeholder="typeAndEnter" placeholder="输入文字后回车发送..."
               style="flex:1;" onkeydown="loginKeyDown(event)" autocomplete="off" />
        <button class="btn btn-outline btn-sm" onclick="sendLoginKey('Backspace')">⌫</button>
        <button class="btn btn-outline btn-sm" onclick="sendLoginKey('Tab')">Tab</button>
        <button class="btn btn-outline btn-sm" onclick="sendLoginKey('Enter')">Enter</button>
      </div>
    </div>
  </div>

  <!-- Conversations (shared source of truth for the checkbox lists below) -->
  <div class="section">
    <h2><span data-i18n="convListTitle">会话列表</span> <span class="status status-idle" id="discoverStatus" data-i18n="idle">闲置</span></h2>
    <div class="row">
      <button class="btn btn-primary" id="refreshConvsBtn" onclick="refreshConvs()" data-i18n="refreshConvList">刷新会话列表</button>
      <button class="btn btn-danger" id="stopRefreshBtn" onclick="stopRefreshConvs()" style="display:none" data-i18n="cancel">取消</button>
      <span class="meta" id="discoverMeta"></span>
    </div>
    <div class="meta" id="discoverHint" style="margin-top:6px;" data-i18n="convListHint">点击"刷新会话列表"从抖音加载全部会话，然后在下方采集/导出/定时各节勾选需要处理的会话。</div>
  </div>

  <!-- Scraper -->
  <div class="section">
    <h2><span data-i18n="scraperTitle">采集</span> <span class="status status-idle" id="scrapeStatus" data-i18n="idle">闲置</span></h2>
    <div class="row">
      <div class="toggle">
        <input type="radio" name="mode" id="modeIncr" value="incremental" checked>
        <label for="modeIncr" data-i18n="incremental">增量</label>
        <input type="radio" name="mode" id="modeFull" value="full">
        <label for="modeFull" data-i18n="full">全量</label>
      </div>
      <button class="btn btn-primary" id="scrapeBtn" onclick="startScrape()" data-i18n="start">开始</button>
      <button class="btn btn-danger" id="stopBtn" onclick="stopScrape()" style="display:none" data-i18n="stop">停止</button>
    </div>
    <div class="conv-list-toolbar" style="margin-top:10px;">
      <span data-i18n="selectConvsLabel">选择会话:</span>
      <a href="#" onclick="selectAllConvs('scrape',true);return false;" data-i18n="selectAll">全选</a>
      <span class="sep">·</span>
      <a href="#" onclick="selectAllConvs('scrape',false);return false;" data-i18n="selectNone">清空</a>
      <span class="sep">·</span>
      <span id="scrapeConvCount">0 / 0</span>
    </div>
    <div class="conv-list-box" id="scrapeConvList"></div>
    <div class="meta" id="scrapeTime"></div>
    <div class="log-box" id="scrapeLog"></div>
  </div>

  <!-- Schedule -->
  <div class="section">
    <h2 data-i18n="scheduleTitle">定时任务</h2>
    <div class="schedule-row">
      <label class="switch">
        <input type="checkbox" id="scheduleEnabled">
        <span class="slider"></span>
      </label>
      <div style="display:flex;gap:4px;align-items:center;flex:1;">
        <input type="text" id="cronMin" value="0" style="width:42px;text-align:center" placeholder="min">
        <input type="text" id="cronHour" value="0" style="width:42px;text-align:center" placeholder="hour">
        <input type="text" id="cronDay" value="*" style="width:42px;text-align:center" placeholder="day">
        <input type="text" id="cronMonth" value="*" style="width:42px;text-align:center" placeholder="mon">
        <input type="text" id="cronDow" value="*" style="width:42px;text-align:center" placeholder="dow">
      </div>
      <button class="btn btn-outline btn-sm" onclick="updateSchedule()" data-i18n="apply">应用</button>
    </div>
    <div style="margin-top:6px;display:flex;gap:12px;flex-wrap:wrap;">
      <span style="font-size:11px;color:var(--text3);" data-i18n="cronHint">分 时 日 月 周</span>
      <span class="cron-preset" onclick="setCron('0 0 * * *')" data-i18n="cronMidnight">每天 00:00</span>
      <span class="cron-preset" onclick="setCron('0 */6 * * *')" data-i18n="cron6h">每 6 小时</span>
      <span class="cron-preset" onclick="setCron('0 8,20 * * *')" data-i18n="cron8am8pm">每天 8 点 & 20 点</span>
      <span class="cron-preset" onclick="setCron('30 2 * * 1')" data-i18n="cronMon">周一 02:30</span>
    </div>
    <div class="conv-list-toolbar" style="margin-top:10px;">
      <span data-i18n="selectConvsLabel">选择会话:</span>
      <a href="#" onclick="selectAllConvs('schedule',true);return false;" data-i18n="selectAll">全选</a>
      <span class="sep">·</span>
      <a href="#" onclick="selectAllConvs('schedule',false);return false;" data-i18n="selectNone">清空</a>
      <span class="sep">·</span>
      <span id="scheduleConvCount">0 / 0</span>
    </div>
    <div class="conv-list-box" id="scheduleConvList"></div>
    <div class="meta" id="scheduleMeta" style="margin-top:6px;"></div>
  </div>

  <!-- Export -->
  <div class="section">
    <h2><span data-i18n="exportTitle">导出</span> <span class="status status-idle" id="exportStatus" data-i18n="idle">闲置</span></h2>
    <div class="row">
      <select id="exportFormat">
        <option value="jsonl">JSONL</option>
        <option value="json">JSON</option>
      </select>
      <button class="btn btn-primary" id="exportBtn" onclick="startExport()" data-i18n="exportBtn">导出</button>
      <a class="btn btn-success" id="downloadBtn" style="display:none;text-decoration:none" href="/panel/api/export/download" data-i18n="download">下载</a>
    </div>
    <div class="conv-list-toolbar" style="margin-top:10px;">
      <span data-i18n="selectConvsLabel">选择会话:</span>
      <a href="#" onclick="selectAllConvs('export',true);return false;" data-i18n="selectAll">全选</a>
      <span class="sep">·</span>
      <a href="#" onclick="selectAllConvs('export',false);return false;" data-i18n="selectNone">清空</a>
      <span class="sep">·</span>
      <span id="exportConvCount">0 / 0</span>
    </div>
    <div class="conv-list-box" id="exportConvList"></div>
    <div class="meta" id="exportMsg"></div>
  </div>

  <div class="section">
    <h2 data-i18n="passwordTitle">密码</h2>
    <div class="meta" style="margin-bottom:8px" data-i18n="passwordDesc">设置密码以保护聊天查看器和控制面板。</div>
    <div class="row">
      <input type="password" id="pwInput" data-i18n-placeholder="enterPassword" placeholder="输入密码" style="flex:1;padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg)">
      <button class="btn btn-primary" onclick="setPassword()" data-i18n="setBtn">设置</button>
      <button class="btn" onclick="clearPassword()" data-i18n="clearBtn">清除</button>
    </div>
    <div class="meta" id="pwStatus" style="margin-top:6px"></div>
  </div>

</div>

<script>
/* ── i18n ── */
const T = {
  zh: {
    appTitle: '抖音聊天记录导出',
    chatViewer: '聊天查看器 →',
    panelTitle: '控制面板',
    enterPasswordPrompt: '请输入密码',
    password: '密码',
    loginBtn: '登录',
    statConversations: '会话',
    statMessages: '消息',
    statUsers: '用户',
    loginTitle: '登录',
    scanQr: '扫码登录',
    cancel: '取消',
    clearSession: '清除会话',
    clickScreenshotHint: '点击截图进行交互。下方输入文本后回车发送。',
    typeAndEnter: '输入文字后回车发送...',
    convListTitle: '会话列表',
    refreshConvList: '刷新会话列表',
    convListHint: '点击"刷新会话列表"从抖音加载全部会话，然后在下方采集/导出/定时各节勾选需要处理的会话。',
    scraperTitle: '采集',
    incremental: '增量',
    full: '全量',
    start: '开始',
    stop: '停止',
    selectConvsLabel: '选择会话:',
    selectAll: '全选',
    selectNone: '清空',
    scheduleTitle: '定时任务',
    apply: '应用',
    cronHint: '分 时 日 月 周',
    cronMidnight: '每天 00:00',
    cron6h: '每 6 小时',
    cron8am8pm: '每天 8 点 & 20 点',
    cronMon: '周一 02:30',
    exportTitle: '导出',
    exportBtn: '导出',
    download: '下载',
    passwordTitle: '密码',
    passwordDesc: '设置密码以保护聊天查看器和控制面板。',
    enterPassword: '输入密码',
    setBtn: '设置',
    clearBtn: '清除',
    idle: '闲置',
    running: '进行中',
    completed: '已完成',
    failed: '失败',
    checking: '检测中...',
    loginActive: '登录有效',
    loginExpired: '登录已过期 — 请重新扫码',
    loginNoProfile: '未登录 — 请扫码登录',
    sessionCleared: '会话已清除',
    clearLoginConfirm: '确定清除登录会话？需要重新扫码。',
    passwordSet: '密码已设置',
    passwordEmpty: '请输入密码',
    noPasswordSet: '未设置密码（查看器公开）',
    passwordIsSet: '密码已设置',
    startedAt: '开始: ',
    finishedAt: '完成: ',
    noOutput: '(无输出)',
    nextRun: '下次运行: ',
    noConvsDiscovered: '尚未发现会话。点击上方"刷新会话列表"。',
    waitingScan: '等待扫码',
    startingLogin: '正在启动',
    error: '错误',
    convsCount: (n, tot) => n + ' / ' + tot,
    discoveredAt: (t) => '最近刷新: ' + t,
    discovered: (n) => '发现 ' + n + ' 个会话',
    confirmMultiExport: (n) => '将导出 ' + n + ' 个会话并打包为 zip，是否继续？',
    loginFailed: '登录失败',
    wrongPassword: '密码错误',
  },
  en: {
    appTitle: 'Douyin Chat Export',
    chatViewer: 'Chat Viewer →',
    panelTitle: 'Control Panel',
    enterPasswordPrompt: 'Please enter password',
    password: 'Password',
    loginBtn: 'Login',
    statConversations: 'Conversations',
    statMessages: 'Messages',
    statUsers: 'Users',
    loginTitle: 'Login',
    scanQr: 'Scan QR Login',
    cancel: 'Cancel',
    clearSession: 'Clear Session',
    clickScreenshotHint: 'Click the screenshot to interact. Type below to input text.',
    typeAndEnter: 'Type here and press Enter to send...',
    convListTitle: 'Conversations',
    refreshConvList: 'Refresh conversation list',
    convListHint: 'Click "Refresh conversation list" to load all conversations from Douyin, then check the ones you want in each section below.',
    scraperTitle: 'Scraper',
    incremental: 'Incremental',
    full: 'Full',
    start: 'Start',
    stop: 'Stop',
    selectConvsLabel: 'Select conversations:',
    selectAll: 'Select all',
    selectNone: 'Clear',
    scheduleTitle: 'Schedule',
    apply: 'Apply',
    cronHint: 'min hour day month weekday',
    cronMidnight: 'Every midnight',
    cron6h: 'Every 6 hours',
    cron8am8pm: '8AM & 8PM',
    cronMon: 'Mon 2:30AM',
    exportTitle: 'Export',
    exportBtn: 'Export',
    download: 'Download',
    passwordTitle: 'Password',
    passwordDesc: 'Set a password to protect the chat viewer and panel.',
    enterPassword: 'Enter password',
    setBtn: 'Set',
    clearBtn: 'Clear',
    idle: 'idle',
    running: 'running',
    completed: 'completed',
    failed: 'failed',
    checking: 'checking...',
    loginActive: 'Login session valid',
    loginExpired: 'Session expired — click Scan QR Login',
    loginNoProfile: 'No login session — click Scan QR Login',
    sessionCleared: 'Session cleared',
    clearLoginConfirm: 'Clear login session? You will need to re-scan QR code.',
    passwordSet: 'Password saved',
    passwordEmpty: 'Please enter a password',
    noPasswordSet: 'No password set (viewer is public)',
    passwordIsSet: 'Password is set',
    startedAt: 'Started: ',
    finishedAt: 'Finished: ',
    noOutput: '(no output)',
    nextRun: 'Next run: ',
    noConvsDiscovered: 'No conversations discovered yet. Click "Refresh conversation list" above.',
    waitingScan: 'waiting scan',
    startingLogin: 'starting',
    error: 'error',
    convsCount: (n, tot) => n + ' / ' + tot,
    discoveredAt: (t) => 'Last refresh: ' + t,
    discovered: (n) => 'Found ' + n + ' conversations',
    confirmMultiExport: (n) => 'Export ' + n + ' conversations as a zip. Continue?',
    loginFailed: 'Login failed',
    wrongPassword: 'Wrong password',
  },
};

let lang = localStorage.getItem('panel-lang') || 'zh';

function t(key, ...args) {
  const v = (T[lang] && T[lang][key]) ?? (T.zh && T.zh[key]) ?? key;
  return typeof v === 'function' ? v(...args) : v;
}

function applyI18n() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    const v = T[lang]?.[key];
    if (typeof v === 'string') el.textContent = v;
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    const key = el.getAttribute('data-i18n-placeholder');
    const v = T[lang]?.[key];
    if (typeof v === 'string') el.placeholder = v;
  });
  document.querySelectorAll('.lang-switcher button').forEach(b => {
    b.classList.toggle('active', b.dataset.lang === lang);
  });
  document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
}

function setLang(l) {
  lang = l;
  localStorage.setItem('panel-lang', l);
  applyI18n();
  // Re-render dynamic texts (counts, meta, etc.)
  renderDiscoverMeta();
  renderConvCounts();
  refreshStatusLabels();
}

/* ── Theme ── */
function setTheme(t) {
  document.body.setAttribute('data-theme', t);
  document.querySelectorAll('.theme-dot').forEach(d => d.classList.toggle('active', d.dataset.t === t));
  localStorage.setItem('panel-theme', t);
}
(function() {
  const saved = localStorage.getItem('panel-theme');
  if (saved) setTheme(saved);
})();

/* ── Conversation selection state ── */
let discoveredConvs = [];     // [{nickname, name, time, preview}, ...]
let discoveredAt = 0;
let selectedMap = {           // section -> Set of nicknames
  scraper: new Set(),
  export: new Set(),
  schedule: new Set(),
};
let lastDiscoverStatus = 'idle';
let lastDiscoverMsg = '';

async function loadStatus() {
  try {
    const r = await fetch('/panel/api/status');
    const d = await r.json();
    document.getElementById('convCount').textContent = d.conversations;
    document.getElementById('msgCount').textContent = d.messages.toLocaleString();
    document.getElementById('userCount').textContent = d.users;

    // Scrape status
    const ss = d.scrape;
    const se = document.getElementById('scrapeStatus');
    setStatusEl(se, ss.status);
    document.getElementById('scrapeBtn').disabled = ss.status === 'running';
    document.getElementById('stopBtn').style.display = ss.status === 'running' ? '' : 'none';
    let timeStr = '';
    if (ss.started_at) timeStr += t('startedAt') + new Date(ss.started_at * 1000).toLocaleTimeString();
    if (ss.finished_at) timeStr += '  ' + t('finishedAt') + new Date(ss.finished_at * 1000).toLocaleTimeString();
    if (ss.message) timeStr += '  ' + ss.message;
    document.getElementById('scrapeTime').textContent = timeStr;

    if (ss.status === 'running' || ss.status === 'completed' || ss.status === 'failed') {
      loadLog();
    }

    // Export status
    const es = d.export;
    const ee = document.getElementById('exportStatus');
    setStatusEl(ee, es.status);
    document.getElementById('exportBtn').disabled = es.status === 'running';
    document.getElementById('exportMsg').textContent = es.message || '';
    document.getElementById('downloadBtn').style.display = (es.status === 'completed' && es.file_path) ? '' : 'none';

    // Schedule status
    const sch = d.scheduler;
    document.getElementById('scheduleEnabled').checked = sch.enabled;
    if (sch.schedule) {
      const parts = sch.schedule.split(/\s+/);
      if (parts.length === 5) {
        ['cronMin','cronHour','cronDay','cronMonth','cronDow'].forEach((id, i) => {
          document.getElementById(id).value = parts[i];
        });
      }
    }
    let schMeta = '';
    if (sch.enabled && sch.next_run) {
      schMeta = t('nextRun') + new Date(sch.next_run * 1000).toLocaleString();
    }
    document.getElementById('scheduleMeta').textContent = schMeta;

  } catch (e) { console.error('Status fetch failed:', e); }
}

function setStatusEl(el, status) {
  if (!el) return;
  el.textContent = t(status) || status;
  el.className = 'status status-' + status;
}

function refreshStatusLabels() {
  // Re-apply current status text (used after language switch)
  const scrapeStatus = document.getElementById('scrapeStatus');
  if (scrapeStatus) {
    const cur = [...scrapeStatus.classList].find(c => c.startsWith('status-') && c !== 'status');
    if (cur) setStatusEl(scrapeStatus, cur.replace('status-', ''));
  }
  const exportStatus = document.getElementById('exportStatus');
  if (exportStatus) {
    const cur = [...exportStatus.classList].find(c => c.startsWith('status-') && c !== 'status');
    if (cur) setStatusEl(exportStatus, cur.replace('status-', ''));
  }
  const discoverEl = document.getElementById('discoverStatus');
  if (discoverEl) setStatusEl(discoverEl, lastDiscoverStatus);
}

/* ── Conversation discovery / checkbox lists ── */

async function loadSelections() {
  try {
    const r = await fetch('/panel/api/conversations/selected');
    const d = await r.json();
    selectedMap.scraper = new Set(d.scraper || []);
    selectedMap.export = new Set(d.export || []);
    selectedMap.schedule = new Set(d.schedule || []);
  } catch {}
}

async function loadDiscoveredConvs() {
  try {
    const r = await fetch('/panel/api/conversations/refresh/status');
    const d = await r.json();
    discoveredConvs = d.items || [];
    discoveredAt = d.discovered_at || 0;
    lastDiscoverStatus = d.status || 'idle';
    lastDiscoverMsg = d.message || '';
    renderDiscoverMeta();
    setStatusEl(document.getElementById('discoverStatus'), lastDiscoverStatus);
    document.getElementById('refreshConvsBtn').disabled = lastDiscoverStatus === 'running';
    document.getElementById('stopRefreshBtn').style.display = lastDiscoverStatus === 'running' ? '' : 'none';
    renderAllConvLists();
  } catch {}
}

function renderDiscoverMeta() {
  const el = document.getElementById('discoverMeta');
  if (!el) return;
  const parts = [];
  if (lastDiscoverMsg) parts.push(lastDiscoverMsg);
  if (discoveredAt) parts.push(t('discoveredAt', new Date(discoveredAt * 1000).toLocaleString()));
  el.textContent = parts.join(' · ');
}

function renderAllConvLists() {
  ['scrape', 'schedule', 'export'].forEach(prefix => renderConvList(prefix));
  renderConvCounts();
}

function sectionKey(prefix) {
  // UI prefix -> backend section name
  return prefix === 'scrape' ? 'scraper' : prefix;
}

function renderConvList(prefix) {
  const box = document.getElementById(prefix + 'ConvList');
  if (!box) return;
  box.innerHTML = '';
  if (discoveredConvs.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'conv-list-empty';
    empty.textContent = t('noConvsDiscovered');
    box.appendChild(empty);
    return;
  }
  const selected = selectedMap[sectionKey(prefix)];
  for (const conv of discoveredConvs) {
    const key = conv.nickname || conv.name;
    if (!key) continue;
    const label = document.createElement('label');
    label.className = 'conv-check';
    label.title = key;
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = key;
    cb.checked = selected.has(key);
    cb.onchange = () => onConvToggle(prefix, key, cb.checked);
    const span = document.createElement('span');
    span.textContent = key;
    label.appendChild(cb);
    label.appendChild(span);
    box.appendChild(label);
  }
}

function renderConvCounts() {
  ['scrape', 'schedule', 'export'].forEach(prefix => {
    const el = document.getElementById(prefix + 'ConvCount');
    if (!el) return;
    el.textContent = t('convsCount', selectedMap[sectionKey(prefix)].size, discoveredConvs.length);
  });
}

let saveTimers = {};
function onConvToggle(prefix, key, checked) {
  const section = sectionKey(prefix);
  const set = selectedMap[section];
  if (checked) set.add(key); else set.delete(key);
  renderConvCounts();
  // Debounced persist
  clearTimeout(saveTimers[section]);
  saveTimers[section] = setTimeout(() => persistSelection(section), 400);
}

async function persistSelection(section) {
  try {
    await fetch('/panel/api/conversations/selected', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ section, conversations: [...selectedMap[section]] }),
    });
  } catch {}
}

function selectAllConvs(prefix, checked) {
  const section = sectionKey(prefix);
  const set = selectedMap[section];
  if (checked) {
    for (const conv of discoveredConvs) {
      const key = conv.nickname || conv.name;
      if (key) set.add(key);
    }
  } else {
    set.clear();
  }
  renderConvList(prefix);
  renderConvCounts();
  persistSelection(section);
}

async function refreshConvs() {
  document.getElementById('refreshConvsBtn').disabled = true;
  try {
    const r = await fetch('/panel/api/conversations/refresh', { method: 'POST' });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      alert(d.error || 'Failed');
      document.getElementById('refreshConvsBtn').disabled = false;
      return;
    }
  } catch {
    document.getElementById('refreshConvsBtn').disabled = false;
    return;
  }
  // Poll status
  const poll = async () => {
    await loadDiscoveredConvs();
    if (lastDiscoverStatus === 'running') {
      setTimeout(poll, 1500);
    }
  };
  poll();
}

async function stopRefreshConvs() {
  await fetch('/panel/api/conversations/refresh/stop', { method: 'POST' });
  loadDiscoveredConvs();
}

async function loadLog() {
  try {
    const r = await fetch('/panel/api/scrape/log?lines=80');
    const d = await r.json();
    const box = document.getElementById('scrapeLog');
    box.textContent = d.log || t('noOutput');
    box.classList.add('show');
    box.scrollTop = box.scrollHeight;
  } catch {}
}

async function startScrape() {
  const incremental = document.getElementById('modeIncr').checked;
  const conversations = [...selectedMap.scraper];
  document.getElementById('scrapeBtn').disabled = true;
  await fetch('/panel/api/scrape', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ incremental, conversations }),
  });
  loadStatus();
}

async function stopScrape() {
  await fetch('/panel/api/scrape/stop', { method: 'POST' });
  loadStatus();
}

function setCron(expr) {
  const parts = expr.split(/\s+/);
  ['cronMin','cronHour','cronDay','cronMonth','cronDow'].forEach((id, i) => {
    document.getElementById(id).value = parts[i] || '*';
  });
}

async function updateSchedule() {
  const enabled = document.getElementById('scheduleEnabled').checked;
  const cron = ['cronMin','cronHour','cronDay','cronMonth','cronDow']
    .map(id => document.getElementById(id).value.trim() || '*').join(' ');
  const incremental = document.getElementById('modeIncr').checked;
  const conversations = [...selectedMap.schedule];
  const r = await fetch('/panel/api/schedule', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ enabled, cron, incremental, conversations }),
  });
  const d = await r.json();
  if (d.error) {
    document.getElementById('scheduleMeta').textContent = d.error;
  }
  loadStatus();
}

async function startExport() {
  const format = document.getElementById('exportFormat').value;
  const conversations = [...selectedMap.export];
  if (conversations.length > 1 && !confirm(t('confirmMultiExport', conversations.length))) {
    return;
  }
  document.getElementById('exportBtn').disabled = true;
  setStatusEl(document.getElementById('exportStatus'), 'running');
  await fetch('/panel/api/export', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ format, conversations }),
  });
  loadStatus();
}

/* ── Password ── */
async function loadPasswordStatus() {
  const r = await fetch('/panel/api/password/status');
  const d = await r.json();
  document.getElementById('pwStatus').textContent = d.has_password ? t('passwordIsSet') : t('noPasswordSet');
}
async function setPassword() {
  const pw = document.getElementById('pwInput').value;
  if (!pw) { document.getElementById('pwStatus').textContent = t('passwordEmpty'); return; }
  const r = await fetch('/panel/api/password', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ password: pw }),
  });
  const d = await r.json();
  document.getElementById('pwStatus').textContent = d.message;
  document.getElementById('pwInput').value = '';
}
async function clearPassword() {
  const r = await fetch('/panel/api/password', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ password: '' }),
  });
  const d = await r.json();
  document.getElementById('pwStatus').textContent = d.message;
}

/* ── Login ── */
let loginPollTimer = null;

async function checkLogin() {
  const ls = document.getElementById('loginStatus');
  const info = document.getElementById('loginInfo');
  ls.textContent = t('checking'); ls.className = 'status status-running';
  try {
    const r = await fetch('/panel/api/login/check');
    const d = await r.json();
    if (d.status === 'logged_in') {
      ls.textContent = t('completed'); ls.className = 'status status-completed';
      info.textContent = t('loginActive');
    } else if (d.status === 'expired') {
      ls.textContent = t('failed'); ls.className = 'status status-failed';
      info.textContent = t('loginExpired');
    } else if (d.status === 'no_profile') {
      ls.textContent = t('failed'); ls.className = 'status status-failed';
      info.textContent = t('loginNoProfile');
    } else {
      ls.textContent = d.status; ls.className = 'status status-idle';
      info.textContent = d.message || '';
    }
  } catch { ls.textContent = t('error'); ls.className = 'status status-failed'; }
}

async function startLogin() {
  document.getElementById('loginBtn').disabled = true;
  document.getElementById('loginCancelBtn').style.display = '';
  await fetch('/panel/api/login/start', { method: 'POST' });
  if (loginPollTimer) clearInterval(loginPollTimer);
  loginPollTimer = setInterval(pollLoginStatus, 1000);
}

async function cancelLogin() {
  await fetch('/panel/api/login/cancel', { method: 'POST' });
  finishLogin();
}

function finishLogin() {
  if (loginPollTimer) { clearInterval(loginPollTimer); loginPollTimer = null; }
  document.getElementById('loginBtn').disabled = false;
  document.getElementById('loginCancelBtn').style.display = 'none';
  document.getElementById('loginScreenshot').style.display = 'none';
}

async function pollLoginStatus() {
  try {
    const r = await fetch('/panel/api/login/status');
    const d = await r.json();
    const ls = document.getElementById('loginStatus');
    const info = document.getElementById('loginInfo');

    if (d.status === 'waiting_scan') {
      ls.textContent = t('waitingScan'); ls.className = 'status status-running';
      info.textContent = d.message || '';
      if (d.screenshot) {
        document.getElementById('loginImg').src = 'data:image/png;base64,' + d.screenshot;
        document.getElementById('loginScreenshot').style.display = '';
      }
    } else if (d.status === 'logged_in') {
      ls.textContent = t('completed'); ls.className = 'status status-completed';
      info.textContent = d.message;
      finishLogin();
    } else if (d.status === 'failed') {
      ls.textContent = t('failed'); ls.className = 'status status-failed';
      info.textContent = d.message;
      finishLogin();
    } else if (d.status === 'starting') {
      ls.textContent = t('startingLogin'); ls.className = 'status status-running';
      info.textContent = d.message;
    }
  } catch {}
}

/* ── Mouse interaction on screenshot ── */
let isDragging = false;
const VIEWPORT_W = 1400, VIEWPORT_H = 900;

function imgCoords(e) {
  const img = document.getElementById('loginImg');
  const rect = img.getBoundingClientRect();
  const scaleX = VIEWPORT_W / rect.width;
  const scaleY = VIEWPORT_H / rect.height;
  return {
    x: Math.round((e.clientX - rect.left) * scaleX),
    y: Math.round((e.clientY - rect.top) * scaleY),
  };
}

function sendMouse(action, x, y) {
  fetch('/panel/api/login/mouse', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ action, x, y }),
  });
}

function imgMouseDown(e) {
  e.preventDefault();
  isDragging = true;
  const {x, y} = imgCoords(e);
  sendMouse('mousedown', x, y);
}

function imgMouseMove(e) {
  if (!isDragging) return;
  e.preventDefault();
  const {x, y} = imgCoords(e);
  sendMouse('mousemove', x, y);
}

function imgMouseUp(e) {
  if (!isDragging) {
    const {x, y} = imgCoords(e);
    sendMouse('click', x, y);
  } else {
    const {x, y} = imgCoords(e);
    sendMouse('mouseup', x, y);
  }
  isDragging = false;
}

/* ── Keyboard interaction ── */
function loginKeyDown(e) {
  if (e.key === 'Enter') {
    e.preventDefault();
    const input = document.getElementById('loginKeyInput');
    const text = input.value;
    if (text) {
      fetch('/panel/api/login/keyboard', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ action: 'type', text }),
      });
      input.value = '';
    } else {
      sendLoginKey('Enter');
    }
  }
}

function sendLoginKey(key) {
  fetch('/panel/api/login/keyboard', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ action: 'press', key }),
  });
}

async function clearLogin() {
  if (!confirm(t('clearLoginConfirm'))) return;
  await fetch('/panel/api/login/clear', { method: 'POST' });
  const ls = document.getElementById('loginStatus');
  ls.textContent = t('idle'); ls.className = 'status status-idle';
  document.getElementById('loginInfo').textContent = t('sessionCleared');
}

/* ── Panel Auth ── */
let panelToken = localStorage.getItem('panel_token') || '';

function setCookie(name, val, days) {
  const d = new Date(); d.setTime(d.getTime() + days*86400000);
  document.cookie = name + '=' + val + ';expires=' + d.toUTCString() + ';path=/';
}

async function panelAuthCheck() {
  try {
    const r = await fetch('/api/auth/check', {
      headers: panelToken ? {'Authorization': 'Bearer ' + panelToken} : {}
    });
    const d = await r.json();
    if (!d.need_password || d.authenticated) {
      // Authenticated or no password set
      setCookie('auth_token', panelToken, 7);
      document.getElementById('loginOverlay').style.display = 'none';
      document.getElementById('mainContainer').style.display = '';
      checkLogin();
      // Load selections + discovered conv list before loadStatus so checkboxes
      // can render with correct prior state.
      await loadSelections();
      await loadDiscoveredConvs();
      loadStatus();
      loadPasswordStatus();
      setInterval(loadStatus, 5000);
    } else {
      document.getElementById('loginOverlay').style.display = 'flex';
      document.getElementById('mainContainer').style.display = 'none';
    }
  } catch(e) {
    document.getElementById('loginOverlay').style.display = 'flex';
  }
}

async function panelLogin() {
  const pw = document.getElementById('panelPwInput').value;
  if (!pw) return;
  try {
    const r = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: pw})
    });
    const d = await r.json();
    if (d.token) {
      panelToken = d.token;
      localStorage.setItem('panel_token', panelToken);
      setCookie('auth_token', panelToken, 7);
      document.getElementById('panelLoginErr').textContent = '';
      panelAuthCheck();
    } else {
      document.getElementById('panelLoginErr').textContent = t('wrongPassword');
    }
  } catch(e) {
    document.getElementById('panelLoginErr').textContent = t('loginFailed');
  }
}

// Inject token cookie on every fetch for panel API calls
const _origFetch = window.fetch;
window.fetch = function(url, opts) {
  if (panelToken && typeof url === 'string' && url.startsWith('/panel/')) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    if (opts.headers instanceof Headers) {
      if (!opts.headers.has('Authorization')) opts.headers.set('Authorization', 'Bearer ' + panelToken);
    } else {
      if (!opts.headers['Authorization']) opts.headers['Authorization'] = 'Bearer ' + panelToken;
    }
  }
  return _origFetch.call(this, url, opts);
};

applyI18n();
panelAuthCheck();
</script>
</body>
</html>
"""
