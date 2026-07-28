"""Render a message range to a PNG long-image via headless Chromium.

Opens our own frontend's /screenshot page (a chrome-less static view of a
seq range) in Playwright and screenshots the root element. Everything stays
on 127.0.0.1 — no content ever leaves the machine.

The page authenticates with a short-lived token minted here and revoked
right after the render, so this works whether or not a panel password is set.
"""
import asyncio
import secrets
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from backend import database

screenshot_router = APIRouter()

_THEMES = {"dark", "wechat", "light", "warm", "purple"}

# One render at a time — this is a once-a-day feature, not a thumbnail farm.
_render_lock = asyncio.Lock()

_PAGE_TIMEOUT_MS = 30_000
_READY_TIMEOUT_MS = 30_000
_EPHEMERAL_TOKEN_TTL = 300  # seconds


def _mint_ephemeral_token() -> str:
    from backend import main  # lazy: avoid circular import at module load

    token = secrets.token_urlsafe(24)
    main._active_tokens[token] = time.time() + _EPHEMERAL_TOKEN_TTL
    return token


def _revoke_ephemeral_token(token: str) -> None:
    from backend import main

    main._active_tokens.pop(token, None)


async def _render_page_png(url: str, width: int, scale: int) -> bytes:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page(
                viewport={"width": width, "height": 900},
                device_scale_factor=scale,
                locale="zh-CN",
            )
            await page.goto(url, wait_until="networkidle", timeout=_PAGE_TIMEOUT_MS)
            await page.wait_for_function(
                "window.__shotReady === true", timeout=_READY_TIMEOUT_MS
            )
            await page.evaluate("document.fonts.ready")
            root = page.locator("#shot-root")
            return await root.screenshot(type="png")
        finally:
            await browser.close()


@screenshot_router.get("/api/conversations/{conv_id}/screenshot")
async def render_screenshot(
    request: Request,
    conv_id: str,
    start_seq: int = Query(..., ge=0),
    end_seq: int = Query(..., ge=0),
    theme: str = Query("dark"),
    title: str = Query("", max_length=100),
    subtitle: str = Query("", max_length=100),
    self_uid: str = Query(""),
    width: int = Query(520, ge=320, le=1200),
    scale: int = Query(2, ge=1, le=3),
):
    if end_seq < start_seq:
        raise HTTPException(400, "end_seq 不能小于 start_seq")
    if end_seq - start_seq > 2000:
        raise HTTPException(400, "区间过大（seq 跨度上限 2000）")
    if theme not in _THEMES:
        raise HTTPException(400, f"未知主题（可选：{'/'.join(sorted(_THEMES))}）")
    conv = database.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")

    server = request.scope.get("server") or ("127.0.0.1", 8000)
    port = server[1] or 8000
    token = _mint_ephemeral_token()
    from urllib.parse import urlencode

    qs = urlencode(
        {
            "conv_id": conv_id,
            "start_seq": start_seq,
            "end_seq": end_seq,
            "theme": theme,
            "title": title,
            "subtitle": subtitle,
            "self_uid": self_uid,
            "token": token,
        }
    )
    url = f"http://127.0.0.1:{port}/screenshot?{qs}"

    try:
        async with _render_lock:
            png = await _render_page_png(url, width=width, scale=scale)
    except Exception as e:  # playwright timeout / launch failure
        raise HTTPException(503, f"渲染失败: {e}")
    finally:
        _revoke_ephemeral_token(token)

    return Response(content=png, media_type="image/png")
