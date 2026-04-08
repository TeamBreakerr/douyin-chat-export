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

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "scrape.log")


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


class ExportRequest(BaseModel):
    format: str = "jsonl"
    filter: str = ""


class ScheduleRequest(BaseModel):
    enabled: bool
    cron: str = ""  # cron expression: "0 0 * * *" or shorthand
    incremental: bool = True


class CustomFilterAction(BaseModel):
    action: str  # "add" | "remove"
    value: str


class PasswordRequest(BaseModel):
    password: str = ""  # empty = remove password


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

    cmd = [sys.executable, "-u", "extract.py"]
    if req.incremental:
        cmd.append("--incremental")
    if req.filter:
        cmd.extend(["--filter", req.filter])

    _scrape_state["status"] = "running"
    _scrape_state["started_at"] = time.time()
    _scrape_state["finished_at"] = None
    _scrape_state["message"] = f"{'增量' if req.incremental else '全量'}采集"
    if req.filter:
        _scrape_state["message"] += f" (过滤: {req.filter})"

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


@control_router.post("/api/schedule")
async def set_schedule(req: ScheduleRequest):
    # Cancel existing scheduled task
    if _scheduler_state["task"] and not _scheduler_state["task"].done():
        _scheduler_state["task"].cancel()
        _scheduler_state["task"] = None

    _scheduler_state["enabled"] = req.enabled
    _scheduler_state["schedule"] = req.cron if req.enabled else ""
    _scheduler_state["next_run"] = None

    if req.enabled and req.cron:
        parsed = _parse_cron(req.cron)
        if not parsed:
            return JSONResponse({"error": "无效的 cron 表达式（分 时 日 月 周）"}, status_code=400)

        next_run = _next_cron_run(parsed)
        _scheduler_state["next_run"] = next_run
        _scheduler_state["task"] = asyncio.create_task(
            _cron_loop(parsed, req.incremental)
        )
        cfg = _load_config()
        cfg["schedule"] = req.cron
        _save_config(cfg)
        return {"status": "enabled", "cron": req.cron, "next_run": next_run}

    cfg = _load_config()
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
                # 从 config 读取已配置的会话过滤器，若无则使用数据库中已有的会话名
                cfg = _load_config()
                filters = cfg.get("custom_filters", [])
                if not filters:
                    # 回退到数据库中已保存的会话名称
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

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _do_export, req.format, req.filter)
    return {
        "status": _export_state["status"],
        "message": _export_state["message"],
        "file_path": _export_state["file_path"],
    }


def _do_export(fmt: str, filter_name: str):
    try:
        from extractor.exporter import ChatLabExporter

        ext = ".json" if fmt == "json" else ".jsonl"
        output_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data", f"export{ext}"
        )
        exporter = ChatLabExporter(conv_name=filter_name or None, output_format=fmt)
        exporter.export(output_path)
        _export_state["status"] = "completed"
        _export_state["file_path"] = f"export{ext}"
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        _export_state["message"] = f"导出完成 ({size_mb:.1f} MB)"
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
    <h2 style="margin:0 0 8px;color:var(--text);">Control Panel</h2>
    <p style="margin:0 0 20px;color:var(--text2);font-size:14px;">请输入密码</p>
    <input id="panelPwInput" type="password" placeholder="密码" onkeydown="if(event.key==='Enter')panelLogin()"
      style="width:100%;box-sizing:border-box;padding:10px 14px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);font-size:15px;margin-bottom:12px;outline:none;">
    <button onclick="panelLogin()" style="width:100%;padding:10px;border:none;border-radius:8px;background:var(--accent);color:#fff;font-size:15px;cursor:pointer;">登录</button>
    <div id="panelLoginErr" style="margin-top:10px;color:var(--red);font-size:13px;"></div>
  </div>
</div>
<div class="container" id="mainContainer" style="display:none;">

  <!-- Header -->
  <div class="header">
    <h1>Douyin Chat Export</h1>
    <div class="header-right">
      <div class="theme-switcher">
        <div class="theme-dot active" data-t="dark" title="Dark" onclick="setTheme('dark')"></div>
        <div class="theme-dot" data-t="light" title="Light" onclick="setTheme('light')"></div>
        <div class="theme-dot" data-t="ocean" title="Ocean" onclick="setTheme('ocean')"></div>
        <div class="theme-dot" data-t="purple" title="Purple" onclick="setTheme('purple')"></div>
      </div>
      <a href="/" class="viewer-btn">Chat Viewer &rarr;</a>
    </div>
  </div>

  <!-- Stats -->
  <div class="cards">
    <div class="card"><div class="num" id="convCount">-</div><div class="label">Conversations</div></div>
    <div class="card"><div class="num" id="msgCount">-</div><div class="label">Messages</div></div>
    <div class="card"><div class="num" id="userCount">-</div><div class="label">Users</div></div>
  </div>

  <!-- Login -->
  <div class="section" id="loginSection">
    <h2>Login <span class="status status-idle" id="loginStatus">checking...</span></h2>
    <div id="loginInfo" class="meta" style="margin-bottom:10px"></div>
    <div class="row">
      <button class="btn btn-primary" id="loginBtn" onclick="startLogin()">Scan QR Login</button>
      <button class="btn btn-danger" id="loginCancelBtn" onclick="cancelLogin()" style="display:none">Cancel</button>
      <button class="btn btn-outline btn-sm" onclick="clearLogin()">Clear Session</button>
    </div>
    <div id="loginScreenshot" style="display:none;margin-top:14px;position:relative;user-select:none;">
      <div style="font-size:11px;color:var(--text3);margin-bottom:6px;">Click the screenshot to interact. Type below to input text.</div>
      <div style="text-align:center;">
        <img id="loginImg" style="max-width:100%;border-radius:8px;border:1px solid var(--border);cursor:crosshair;"
             onmousedown="imgMouseDown(event)" onmousemove="imgMouseMove(event)" onmouseup="imgMouseUp(event)"
             ondragstart="return false" />
      </div>
      <div style="display:flex;gap:8px;margin-top:10px;align-items:center;">
        <input type="text" id="loginKeyInput" placeholder="Type here and press Enter to send..."
               style="flex:1;" onkeydown="loginKeyDown(event)" autocomplete="off" />
        <button class="btn btn-outline btn-sm" onclick="sendLoginKey('Backspace')">⌫</button>
        <button class="btn btn-outline btn-sm" onclick="sendLoginKey('Tab')">Tab</button>
        <button class="btn btn-outline btn-sm" onclick="sendLoginKey('Enter')">Enter</button>
      </div>
    </div>
  </div>

  <!-- Scraper -->
  <div class="section">
    <h2>Scraper <span class="status status-idle" id="scrapeStatus">idle</span></h2>
    <div class="row">
      <div class="toggle">
        <input type="radio" name="mode" id="modeIncr" value="incremental" checked>
        <label for="modeIncr">Incremental</label>
        <input type="radio" name="mode" id="modeFull" value="full">
        <label for="modeFull">Full</label>
      </div>
      <select id="scrapeFilter"><option value="">All conversations</option></select>
      <input type="text" id="scrapeCustomInput" placeholder="Custom filter..." style="width:140px"
             onkeydown="if(event.key==='Enter')addCustomFilter()">
      <button class="btn btn-outline btn-sm" onclick="addCustomFilter()" title="Add to list">+</button>
      <button class="btn btn-primary" id="scrapeBtn" onclick="startScrape()">Start</button>
      <button class="btn btn-danger" id="stopBtn" onclick="stopScrape()" style="display:none">Stop</button>
    </div>
    <div class="chip-list" id="customChips"></div>
    <div class="meta" id="scrapeTime"></div>
    <div class="log-box" id="scrapeLog"></div>
  </div>

  <!-- Schedule -->
  <div class="section">
    <h2>Schedule</h2>
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
      <button class="btn btn-outline btn-sm" onclick="updateSchedule()">Apply</button>
    </div>
    <div style="margin-top:6px;display:flex;gap:12px;flex-wrap:wrap;">
      <span style="font-size:11px;color:var(--text3);">min hour day month weekday</span>
      <span class="cron-preset" onclick="setCron('0 0 * * *')">Every midnight</span>
      <span class="cron-preset" onclick="setCron('0 */6 * * *')">Every 6 hours</span>
      <span class="cron-preset" onclick="setCron('0 8,20 * * *')">8AM & 8PM</span>
      <span class="cron-preset" onclick="setCron('30 2 * * 1')">Mon 2:30AM</span>
    </div>
    <div class="meta" id="scheduleMeta" style="margin-top:6px;"></div>
  </div>

  <!-- Export -->
  <div class="section">
    <h2>Export <span class="status status-idle" id="exportStatus">idle</span></h2>
    <div class="row">
      <select id="exportFormat">
        <option value="jsonl">JSONL</option>
        <option value="json">JSON</option>
      </select>
      <select id="exportFilter"><option value="">All conversations</option></select>
      <button class="btn btn-primary" id="exportBtn" onclick="startExport()">Export</button>
      <a class="btn btn-success" id="downloadBtn" style="display:none;text-decoration:none" href="/panel/api/export/download">Download</a>
    </div>
    <div class="meta" id="exportMsg"></div>
  </div>

  <div class="section">
    <h2>Password</h2>
    <div class="meta" style="margin-bottom:8px">Set a password to protect the chat viewer and panel.</div>
    <div class="row">
      <input type="password" id="pwInput" placeholder="Enter password" style="flex:1;padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg)">
      <button class="btn btn-primary" onclick="setPassword()">Set</button>
      <button class="btn" onclick="clearPassword()">Clear</button>
    </div>
    <div class="meta" id="pwStatus" style="margin-top:6px"></div>
  </div>

</div>

<script>
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

/* ── State ── */
let customFilters = [];

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
    se.textContent = ss.status;
    se.className = 'status status-' + ss.status;
    document.getElementById('scrapeBtn').disabled = ss.status === 'running';
    document.getElementById('stopBtn').style.display = ss.status === 'running' ? '' : 'none';
    let timeStr = '';
    if (ss.started_at) timeStr += 'Started: ' + new Date(ss.started_at * 1000).toLocaleTimeString();
    if (ss.finished_at) timeStr += '  Finished: ' + new Date(ss.finished_at * 1000).toLocaleTimeString();
    if (ss.message) timeStr += '  ' + ss.message;
    document.getElementById('scrapeTime').textContent = timeStr;

    if (ss.status === 'running' || ss.status === 'completed' || ss.status === 'failed') {
      loadLog();
    }

    // Export status
    const es = d.export;
    const ee = document.getElementById('exportStatus');
    ee.textContent = es.status;
    ee.className = 'status status-' + es.status;
    document.getElementById('exportBtn').disabled = es.status === 'running';
    document.getElementById('exportMsg').textContent = es.message || '';
    document.getElementById('downloadBtn').style.display = (es.status === 'completed' && es.file_path) ? '' : 'none';

    // Scraper filter: custom filters + DB conversation names
    customFilters = d.custom_filters || [];
    const dbNames = d.conversation_names || [];
    buildScrapeFilter(dbNames);
    renderChips();

    // Export filter: only from DB
    const expSel = document.getElementById('exportFilter');
    const expCur = expSel.value;
    expSel.innerHTML = '<option value="">All conversations</option>';
    for (const name of dbNames) {
      const opt = document.createElement('option');
      opt.value = name; opt.textContent = name;
      expSel.appendChild(opt);
    }
    expSel.value = expCur;

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
      schMeta = 'Next run: ' + new Date(sch.next_run * 1000).toLocaleString();
    }
    document.getElementById('scheduleMeta').textContent = schMeta;

  } catch (e) { console.error('Status fetch failed:', e); }
}

function buildScrapeFilter(dbNames) {
  const sel = document.getElementById('scrapeFilter');
  const cur = sel.value;
  sel.innerHTML = '<option value="">All conversations</option>';
  // custom filters first
  if (customFilters.length > 0) {
    const grp1 = document.createElement('optgroup');
    grp1.label = 'Custom';
    for (const f of customFilters) {
      const opt = document.createElement('option');
      opt.value = f; opt.textContent = f;
      grp1.appendChild(opt);
    }
    sel.appendChild(grp1);
  }
  // DB names
  if (dbNames.length > 0) {
    const grp2 = document.createElement('optgroup');
    grp2.label = 'Database';
    for (const name of dbNames) {
      const opt = document.createElement('option');
      opt.value = name; opt.textContent = name;
      grp2.appendChild(opt);
    }
    sel.appendChild(grp2);
  }
  sel.value = cur;
}

function renderChips() {
  const box = document.getElementById('customChips');
  box.innerHTML = '';
  for (const f of customFilters) {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.innerHTML = f + ' <span class="remove" onclick="removeCustomFilter(\'' +
      f.replace(/'/g, "\\'") + '\')">&times;</span>';
    box.appendChild(chip);
  }
}

async function addCustomFilter() {
  const input = document.getElementById('scrapeCustomInput');
  const val = input.value.trim();
  if (!val) return;
  await fetch('/panel/api/custom-filter', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ action: 'add', value: val }),
  });
  input.value = '';
  loadStatus();
}

async function removeCustomFilter(val) {
  await fetch('/panel/api/custom-filter', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ action: 'remove', value: val }),
  });
  loadStatus();
}

async function loadLog() {
  try {
    const r = await fetch('/panel/api/scrape/log?lines=80');
    const d = await r.json();
    const box = document.getElementById('scrapeLog');
    box.textContent = d.log || '(no output)';
    box.classList.add('show');
    box.scrollTop = box.scrollHeight;
  } catch {}
}

async function startScrape() {
  const incremental = document.getElementById('modeIncr').checked;
  const filter = document.getElementById('scrapeFilter').value;
  document.getElementById('scrapeBtn').disabled = true;
  await fetch('/panel/api/scrape', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ incremental, filter }),
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
  const r = await fetch('/panel/api/schedule', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ enabled, cron, incremental }),
  });
  const d = await r.json();
  if (d.error) {
    document.getElementById('scheduleMeta').textContent = d.error;
  }
  loadStatus();
}

async function startExport() {
  const format = document.getElementById('exportFormat').value;
  const filter = document.getElementById('exportFilter').value;
  document.getElementById('exportBtn').disabled = true;
  document.getElementById('exportStatus').textContent = 'running';
  document.getElementById('exportStatus').className = 'status status-running';
  await fetch('/panel/api/export', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ format, filter }),
  });
  loadStatus();
}

/* ── Password ── */
async function loadPasswordStatus() {
  const r = await fetch('/panel/api/password/status');
  const d = await r.json();
  document.getElementById('pwStatus').textContent = d.has_password ? 'Password is set' : 'No password set (viewer is public)';
}
async function setPassword() {
  const pw = document.getElementById('pwInput').value;
  if (!pw) { document.getElementById('pwStatus').textContent = 'Please enter a password'; return; }
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
  ls.textContent = 'checking...'; ls.className = 'status status-running';
  try {
    const r = await fetch('/panel/api/login/check');
    const d = await r.json();
    if (d.status === 'logged_in') {
      ls.textContent = 'active'; ls.className = 'status status-completed';
      info.textContent = 'Login session valid';
    } else if (d.status === 'expired') {
      ls.textContent = 'expired'; ls.className = 'status status-failed';
      info.textContent = 'Session expired — click Scan QR Login';
    } else if (d.status === 'no_profile') {
      ls.textContent = 'not logged in'; ls.className = 'status status-failed';
      info.textContent = 'No login session — click Scan QR Login';
    } else {
      ls.textContent = d.status; ls.className = 'status status-idle';
      info.textContent = d.message || '';
    }
  } catch { ls.textContent = 'error'; ls.className = 'status status-failed'; }
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
      ls.textContent = 'waiting scan'; ls.className = 'status status-running';
      info.textContent = d.message || '';
      if (d.screenshot) {
        document.getElementById('loginImg').src = 'data:image/png;base64,' + d.screenshot;
        document.getElementById('loginScreenshot').style.display = '';
      }
    } else if (d.status === 'logged_in') {
      ls.textContent = 'active'; ls.className = 'status status-completed';
      info.textContent = d.message;
      finishLogin();
    } else if (d.status === 'failed') {
      ls.textContent = 'failed'; ls.className = 'status status-failed';
      info.textContent = d.message;
      finishLogin();
    } else if (d.status === 'starting') {
      ls.textContent = 'starting'; ls.className = 'status status-running';
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
  if (!confirm('Clear login session? You will need to re-scan QR code.')) return;
  await fetch('/panel/api/login/clear', { method: 'POST' });
  const ls = document.getElementById('loginStatus');
  ls.textContent = 'cleared'; ls.className = 'status status-idle';
  document.getElementById('loginInfo').textContent = 'Session cleared';
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
      document.getElementById('panelLoginErr').textContent = '密码错误';
    }
  } catch(e) {
    document.getElementById('panelLoginErr').textContent = '登录失败';
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

panelAuthCheck();
</script>
</body>
</html>
"""
