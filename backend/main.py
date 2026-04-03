"""FastAPI backend for browsing exported Douyin chat data."""
import os
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from . import database

app = FastAPI(title="抖音聊天记录浏览器", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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


# Control panel
from backend.control_panel import control_router
app.include_router(control_router)

# Serve Vue frontend (must be last)
_frontend_dist = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")
if os.path.isdir(_frontend_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(_frontend_dist, "assets")), name="assets")

    _index_html = os.path.join(_frontend_dist, "index.html")

    @app.get("/")
    def serve_frontend_root():
        return FileResponse(_index_html)

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        if full_path.startswith("panel"):
            raise HTTPException(404)
        return FileResponse(_index_html)
