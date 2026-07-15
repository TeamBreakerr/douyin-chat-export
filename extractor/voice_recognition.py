"""Persistent voice-message recognition shared by export and the panel job."""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Iterable

from common import paths
from extractor.asr import ASRResult, QwenASRClient
from extractor.models import get_db


def ensure_transcription_table(conn) -> None:
    """Create the cache table for a hot-reloaded process with an older DB."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_transcriptions (
            msg_id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            language TEXT,
            model TEXT,
            emotion TEXT,
            source_size INTEGER,
            source_mtime_ns INTEGER,
            asr_url TEXT,
            transcribed_at INTEGER NOT NULL DEFAULT 0
        )
    """)


def get_content_json(msg) -> dict | None:
    """Extract the complete content_json object from a stored message row."""
    raw = msg["raw_data"]
    if not raw:
        return None
    try:
        raw_obj = json.loads(raw) if isinstance(raw, str) else raw
        cj_raw = raw_obj.get("content_json", "")
        if cj_raw:
            return json.loads(cj_raw) if isinstance(cj_raw, str) else cj_raw
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


def is_voice_message(cj: dict | None) -> bool:
    return bool(cj and cj.get("resource_url") and cj.get("duration"))


def local_media_path(media_dir: str, stored_path: str | None) -> str | None:
    """Resolve a DB media path written on either Windows or POSIX."""
    if not stored_path:
        return None
    value = os.fspath(stored_path)
    if os.path.isabs(value):
        return value if os.path.isfile(value) else None
    relative = value.replace("\\", os.sep).replace("/", os.sep)
    candidate = os.path.abspath(os.path.join(media_dir, relative))
    return candidate if os.path.isfile(candidate) else None


def voice_audio_path(msg, cj: dict | None, media_dir: str) -> str | None:
    """Find the local audio downloaded by the scraper, including old rows."""
    stored = local_media_path(media_dir, msg["media_local_path"])
    if stored:
        return stored
    if not is_voice_message(cj):
        return None

    msg_id = str(msg["msg_id"] or "")
    stem = msg_id[4:] if msg_id.startswith("srv_") else msg_id
    if not stem:
        return None
    voice_dir = os.path.join(media_dir, "voice")
    extensions = (
        ".mpeg", ".mp3", ".wav", ".m4a", ".mp4",
        ".aac", ".ogg", ".flac", ".webm",
    )
    for ext in extensions:
        candidate = os.path.join(voice_dir, stem + ext)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def _source_fingerprint(audio_path: str | None) -> tuple[int | None, int | None]:
    if not audio_path or not os.path.isfile(audio_path):
        return None, None
    stat = os.stat(audio_path)
    return stat.st_size, stat.st_mtime_ns


def load_cached_transcription(
    conn,
    msg_id: str,
    audio_path: str | None,
) -> ASRResult | None:
    """Load a non-stale cached result for one message."""
    ensure_transcription_table(conn)
    row = conn.execute(
        "SELECT text, language, model, emotion, source_size, source_mtime_ns "
        "FROM voice_transcriptions WHERE msg_id = ?",
        (msg_id,),
    ).fetchone()
    if not row or not (row["text"] or "").strip():
        return None

    size, mtime_ns = _source_fingerprint(audio_path)
    if size is not None:
        if row["source_size"] is not None and row["source_size"] != size:
            return None
        if (
            row["source_mtime_ns"] is not None
            and row["source_mtime_ns"] != mtime_ns
        ):
            return None

    return ASRResult(
        text=row["text"].strip(),
        language=row["language"],
        model=row["model"],
        emotion=row["emotion"],
    )


def save_cached_transcription(
    conn,
    msg_id: str,
    result: ASRResult,
    audio_path: str | None,
    asr_url: str,
) -> None:
    """Upsert one successful non-empty transcription."""
    if not result.text.strip():
        return
    ensure_transcription_table(conn)
    size, mtime_ns = _source_fingerprint(audio_path)
    conn.execute(
        """INSERT INTO voice_transcriptions
           (msg_id, text, language, model, emotion, source_size,
            source_mtime_ns, asr_url, transcribed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(msg_id) DO UPDATE SET
             text=excluded.text,
             language=excluded.language,
             model=excluded.model,
             emotion=excluded.emotion,
             source_size=excluded.source_size,
             source_mtime_ns=excluded.source_mtime_ns,
             asr_url=excluded.asr_url,
             transcribed_at=excluded.transcribed_at""",
        (
            msg_id,
            result.text.strip(),
            result.language,
            result.model,
            result.emotion,
            size,
            mtime_ns,
            asr_url,
            int(time.time()),
        ),
    )


def _selected_conversation_ids(conn, conversations: Iterable[str] | None) -> list[str]:
    requested = [str(item).strip() for item in (conversations or []) if str(item).strip()]
    if not requested:
        row = conn.execute(
            "SELECT conv_id FROM conversations ORDER BY last_message_time DESC LIMIT 1"
        ).fetchone()
        return [row["conv_id"]] if row else []

    found: list[str] = []
    for name in requested:
        row = conn.execute(
            "SELECT conv_id FROM conversations WHERE name LIKE ? "
            "ORDER BY last_message_time DESC LIMIT 1",
            (f"%{name}%",),
        ).fetchone()
        if row and row["conv_id"] not in found:
            found.append(row["conv_id"])
    return found


def recognize_voice_messages(
    *,
    conversations: Iterable[str] | None,
    asr_url: str,
    language: str | None = "Chinese",
    prompt: str = "",
    timeout: float = 300,
    batch_size: int = 10,
    force: bool = False,
    progress_cb: Callable[[dict], None] | None = None,
) -> dict:
    """Recognize selected conversations and persist results with progress."""
    summary = {
        "total": 0,
        "done": 0,
        "ok": 0,
        "failed": 0,
        "empty": 0,
        "missing": 0,
        "skipped": 0,
        "current": "",
        "phase": "scanning",
    }

    def emit(**changes) -> None:
        summary.update(changes)
        if progress_cb:
            try:
                progress_cb(dict(summary))
            except Exception:
                pass

    conn = get_db()
    ensure_transcription_table(conn)
    try:
        conv_ids = _selected_conversation_ids(conn, conversations)
        if not conv_ids:
            emit(phase="completed")
            return summary

        placeholders = ",".join("?" for _ in conv_ids)
        rows = conn.execute(
            f"SELECT * FROM messages WHERE conv_id IN ({placeholders}) ORDER BY seq ASC",
            tuple(conv_ids),
        ).fetchall()
        voices = []
        for msg in rows:
            cj = get_content_json(msg)
            if is_voice_message(cj):
                voices.append((msg, cj))
        emit(total=len(voices), phase="scanning")

        file_to_messages: dict[str, list] = {}
        for msg, cj in voices:
            msg_id = str(msg["msg_id"])
            audio_path = voice_audio_path(msg, cj, paths.MEDIA_DIR)
            current = os.path.basename(audio_path) if audio_path else msg_id
            if not force and load_cached_transcription(conn, msg_id, audio_path):
                emit(
                    done=summary["done"] + 1,
                    skipped=summary["skipped"] + 1,
                    current=current,
                )
                continue
            if not audio_path:
                emit(
                    done=summary["done"] + 1,
                    missing=summary["missing"] + 1,
                    current=current,
                )
                continue
            normalized = os.path.abspath(audio_path)
            file_to_messages.setdefault(normalized, []).append(msg)

        if not file_to_messages:
            emit(phase="completed", current="")
            return summary

        emit(phase="recognizing")

        def on_asr_progress(event: dict) -> None:
            path = event["path"]
            linked = file_to_messages.get(path, [])
            count = len(linked)
            result = event.get("result")
            changes = {
                "done": summary["done"] + count,
                "current": os.path.basename(path),
                "phase": "recognizing",
            }
            if result and result.text.strip():
                for msg in linked:
                    save_cached_transcription(
                        conn, str(msg["msg_id"]), result, path, asr_url
                    )
                conn.commit()
                changes["ok"] = summary["ok"] + count
            elif result:
                changes["empty"] = summary["empty"] + count
            else:
                changes["failed"] = summary["failed"] + count
            emit(**changes)

        with QwenASRClient(
            asr_url,
            language=language,
            prompt=prompt,
            timeout=timeout,
            batch_size=min(int(batch_size), 10),
        ) as client:
            client.transcribe_files(file_to_messages.keys(), progress_cb=on_asr_progress)

        emit(phase="completed", current="")
        return summary
    finally:
        conn.commit()
        conn.close()
