"""FastAPI backend for browsing exported Douyin chat data."""
import hashlib
import hmac
import os
import secrets
import time

from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from . import database

app = FastAPI(title="抖音聊天记录浏览器", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth system ──
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "panel_config.json")
_active_tokens: dict[str, float] = {}  # token -> expire_timestamp
_TOKEN_TTL = 7 * 24 * 3600  # 7 days


def _get_password_hash() -> str | None:
    """Read password hash from config."""
    import json
    try:
        with open(_CONFIG_PATH) as f:
            cfg = json.load(f)
        return cfg.get("password_hash") or None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def _verify_token(token: str) -> bool:
    if not token:
        return False
    exp = _active_tokens.get(token)
    if exp and time.time() < exp:
        return True
    _active_tokens.pop(token, None)
    return False


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # Public paths: auth endpoints, static assets, favicon
    if (path.startswith("/api/auth/") or
        path.startswith("/assets") or
        path.startswith("/media") or
        path == "/favicon.svg"):
        return await call_next(request)
    # Protected paths: /api/* and /panel*
    needs_auth = path.startswith("/api/") or path.startswith("/panel")
    if not needs_auth:
        return await call_next(request)
    # If no password set, allow all
    if not _get_password_hash():
        return await call_next(request)
    # Panel HTML page itself is allowed (login screen is embedded)
    if path in ("/panel", "/panel/"):
        return await call_next(request)
    # Check token from header, query param, or cookie
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        token = request.query_params.get("token", "")
    if not token:
        token = request.cookies.get("auth_token", "")
    if _verify_token(token):
        return await call_next(request)
    return JSONResponse({"error": "unauthorized"}, status_code=401)


from pydantic import BaseModel


class AuthLoginRequest(BaseModel):
    password: str


@app.get("/api/auth/check")
def auth_check(request: Request):
    """Check if password is set and if current token is valid."""
    pw_hash = _get_password_hash()
    if not pw_hash:
        return {"need_password": False, "authenticated": True}
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        token = request.query_params.get("token", "")
    return {"need_password": True, "authenticated": _verify_token(token)}


@app.post("/api/auth/login")
def auth_login(req: AuthLoginRequest):
    pw_hash = _get_password_hash()
    if not pw_hash:
        return {"error": "no password set"}, 400
    if not hmac.compare_digest(_hash_password(req.password), pw_hash):
        raise HTTPException(403, "密码错误")
    token = secrets.token_urlsafe(32)
    _active_tokens[token] = time.time() + _TOKEN_TTL
    return {"token": token}

# Serve media files
media_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media")
os.makedirs(media_dir, exist_ok=True)
app.mount("/media", StaticFiles(directory=media_dir), name="media")


@app.get("/api/stats")
def stats():
    return database.get_stats()


@app.get("/api/conversations")
def list_conversations(
    search: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    items, total = database.get_conversations(search=search, page=page, page_size=page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: str):
    conv = database.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    return conv


@app.get("/api/conversations/{conv_id}/messages")
def list_messages(
    conv_id: str,
    page_size: int = Query(100, ge=1, le=500),
    before_seq: int = Query(None),
    after_seq: int = Query(None),
):
    conv = database.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    items, total = database.get_messages(conv_id, page_size=page_size, before_seq=before_seq, after_seq=after_seq)
    return {"items": items, "total": total}


@app.get("/api/conversations/{conv_id}/senders")
def list_senders(conv_id: str):
    conv = database.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    return database.get_senders(conv_id)


@app.get("/api/search")
def search(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    items, total = database.search_messages(q, page=page, page_size=page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/api/messages/{msg_id}")
def get_message(msg_id: str):
    msg = database.get_message(msg_id)
    if not msg:
        raise HTTPException(404, "消息不存在")
    return msg


@app.get("/api/users")
def list_users():
    return database.get_all_users()


@app.get("/api/users/{uid}")
def get_user(uid: str):
    user = database.get_user(uid)
    if not user:
        raise HTTPException(404, "用户不存在")
    return user


# ── Semantic search API ──
from backend import semantic_search


class SemanticToggleRequest(BaseModel):
    enabled: bool
    model: str = None
    threshold: float = None


class SemanticDownloadRequest(BaseModel):
    model: str


class SemanticSearchRequest(BaseModel):
    query: str
    conversation_id: str = None


@app.get("/api/semantic/status")
def semantic_status():
    return semantic_search.get_status()


@app.post("/api/semantic/toggle")
def semantic_toggle(req: SemanticToggleRequest):
    return semantic_search.set_enabled(req.enabled, req.model, req.threshold)


@app.post("/api/semantic/download")
def semantic_download(req: SemanticDownloadRequest):
    try:
        return semantic_search.download_model(req.model)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/semantic/load")
def semantic_load():
    cfg = semantic_search._load_config()
    model_id = cfg.get("semantic_search", {}).get("model", "")
    if not model_id:
        raise HTTPException(400, "No model configured")
    ok = semantic_search.load_model(model_id)
    if not ok:
        raise HTTPException(400, "Failed to load model")
    return {"loaded": True, "model": model_id}


class SemanticIndexRequest(BaseModel):
    force: bool = False


@app.post("/api/semantic/index")
def semantic_index(req: SemanticIndexRequest):
    try:
        semantic_search.build_index(force=req.force)
        return {"status": "started"}
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.post("/api/search/semantic")
def semantic_search_api(req: SemanticSearchRequest):
    cfg = semantic_search._load_config()
    ss = cfg.get("semantic_search", {})
    if not ss.get("enabled"):
        raise HTTPException(403, "Semantic search is not enabled")
    try:
        items, total = semantic_search.search(
            req.query, conv_id=req.conversation_id
        )
        return {"items": items, "total": total}
    except RuntimeError as e:
        raise HTTPException(400, str(e))


# Control panel
from backend.control_panel import control_router, restore_schedule_on_startup
app.include_router(control_router)


@app.on_event("startup")
async def startup():
    await restore_schedule_on_startup()
    # Auto-load semantic search model if enabled
    cfg = semantic_search._load_config()
    ss = cfg.get("semantic_search", {})
    if ss.get("enabled") and ss.get("model"):
        try:
            semantic_search.load_model(ss["model"])
            print(f"[semantic] Model loaded: {ss['model']}")
        except Exception as e:
            print(f"[semantic] Failed to auto-load model: {e}")

# Serve Vue frontend (must be last)
_frontend_dist = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")
if os.path.isdir(_frontend_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(_frontend_dist, "assets")), name="assets")

    _index_html = os.path.join(_frontend_dist, "index.html")

    @app.get("/favicon.svg")
    def serve_favicon():
        return FileResponse(os.path.join(_frontend_dist, "favicon.svg"), media_type="image/svg+xml")

    @app.get("/")
    def serve_frontend_root():
        return FileResponse(_index_html)

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        if full_path.startswith("panel"):
            raise HTTPException(404)
        return FileResponse(_index_html)
