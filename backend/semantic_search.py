"""Semantic search module: Embedding generation + ChromaDB vector storage."""
import json
import os
import threading
import time

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
MODELS_DIR = os.path.join(DATA_DIR, "models")
CHROMA_DIR = os.path.join(DATA_DIR, "chroma_db")
CONFIG_PATH = os.path.join(DATA_DIR, "panel_config.json")

AVAILABLE_MODELS = {
    "bge-small-zh-v1.5": {
        "name": "bge-small-zh-v1.5",
        "hf_repo": "BAAI/bge-small-zh-v1.5",
        "display_name": "BGE Small Chinese v1.5 (Recommended)",
        "disk_size": "~95MB",
        "memory_size": "~250MB",
        "description": "Lightweight Chinese embedding model, good for chat messages",
    },
    "text2vec-base-chinese": {
        "name": "text2vec-base-chinese",
        "hf_repo": "shibing624/text2vec-base-chinese",
        "display_name": "Text2Vec Base Chinese",
        "disk_size": "~400MB",
        "memory_size": "~700MB",
        "description": "Larger model, slightly higher accuracy",
    },
}

# Runtime state
_model = None
_model_name = None
_chroma_collection = None
_download_state = {"status": "idle", "progress": 0, "message": ""}
_index_state = {"status": "idle", "progress": 0, "indexed": 0, "total": 0, "message": ""}
_lock = threading.Lock()


def _load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_status():
    """Return current semantic search status."""
    cfg = _load_config()
    ss = cfg.get("semantic_search", {})
    enabled = ss.get("enabled", False)
    model_id = ss.get("model", "")
    model_downloaded = False
    if model_id:
        model_path = os.path.join(MODELS_DIR, model_id)
        model_downloaded = os.path.isdir(model_path) and any(
            f.endswith((".bin", ".safetensors")) for f in os.listdir(model_path)
        )
    # Check index count (reuse cached collection to avoid memory leak)
    index_count = 0
    if _chroma_collection is not None:
        try:
            index_count = _chroma_collection.count()
        except Exception:
            pass
    elif enabled and model_downloaded and os.path.isdir(CHROMA_DIR):
        try:
            index_count = _get_collection().count()
        except Exception:
            pass

    return {
        "enabled": enabled,
        "model": model_id,
        "model_downloaded": model_downloaded,
        "model_loaded": _model is not None,
        "download": dict(_download_state),
        "index": dict(_index_state),
        "index_count": index_count,
        "threshold": ss.get("threshold", 0.6),
        "available_models": list(AVAILABLE_MODELS.values()),
    }


def set_enabled(enabled: bool, model: str = None, threshold: float = None):
    """Enable or disable semantic search."""
    cfg = _load_config()
    ss = cfg.get("semantic_search", {})
    ss["enabled"] = enabled
    if model:
        ss["model"] = model
    if threshold is not None:
        ss["threshold"] = max(0.1, min(1.0, round(threshold, 2)))
    cfg["semantic_search"] = ss
    _save_config(cfg)
    if not enabled:
        unload_model()
    return get_status()


def download_model(model_id: str):
    """Download model from HuggingFace in a background thread."""
    if model_id not in AVAILABLE_MODELS:
        raise ValueError(f"Unknown model: {model_id}")
    if _download_state["status"] == "downloading":
        raise RuntimeError("Download already in progress")

    _download_state["status"] = "downloading"
    _download_state["progress"] = 0
    _download_state["message"] = f"Downloading {model_id}..."

    def _download():
        try:
            from huggingface_hub import snapshot_download
            model_info = AVAILABLE_MODELS[model_id]
            target_dir = os.path.join(MODELS_DIR, model_id)
            os.makedirs(MODELS_DIR, exist_ok=True)

            _download_state["progress"] = 10
            _download_state["message"] = f"Downloading {model_info['hf_repo']}..."

            snapshot_download(
                repo_id=model_info["hf_repo"],
                local_dir=target_dir,
                local_dir_use_symlinks=False,
            )

            _download_state["status"] = "completed"
            _download_state["progress"] = 100
            _download_state["message"] = f"{model_id} downloaded successfully"

            # Save model selection to config
            cfg = _load_config()
            ss = cfg.get("semantic_search", {})
            ss["model"] = model_id
            cfg["semantic_search"] = ss
            _save_config(cfg)

        except Exception as e:
            _download_state["status"] = "failed"
            _download_state["message"] = f"Download failed: {e}"

    thread = threading.Thread(target=_download, daemon=True)
    thread.start()
    return _download_state


def load_model(model_id: str = None):
    """Load the embedding model into memory."""
    global _model, _model_name

    if _model is not None and _model_name == model_id:
        return True

    if not model_id:
        cfg = _load_config()
        model_id = cfg.get("semantic_search", {}).get("model", "")
    if not model_id:
        return False

    model_path = os.path.join(MODELS_DIR, model_id)
    if not os.path.isdir(model_path):
        return False

    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(model_path)
        _model_name = model_id
        return True
    except Exception as e:
        print(f"[semantic] Failed to load model: {e}")
        return False


def unload_model():
    """Unload model from memory."""
    global _model, _model_name, _chroma_collection, _chroma_client
    _model = None
    _model_name = None
    _chroma_collection = None
    _chroma_client = None


_chroma_client = None


def _get_client():
    """Get or create ChromaDB client."""
    global _chroma_client
    if _chroma_client is None:
        import chromadb
        _chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
    return _chroma_client


def _get_collection():
    """Get or create ChromaDB collection."""
    global _chroma_collection
    if _chroma_collection is not None:
        return _chroma_collection

    client = _get_client()
    _chroma_collection = client.get_or_create_collection(
        name="messages",
        metadata={"hnsw:space": "cosine"},
    )
    return _chroma_collection


def _extract_share_text(raw_data: str, content: str = "") -> str:
    """Extract text content from a share message's raw_data JSON."""
    if raw_data:
        try:
            raw = json.loads(raw_data)
            cj_str = raw.get("content_json", "")
            if cj_str:
                cj = json.loads(cj_str)
                title = cj.get("content_title", "").strip()
                if title:
                    return title
        except (json.JSONDecodeError, AttributeError):
            pass
    # Fallback: strip prefix from content field (e.g. "[分享视频]标题...")
    for prefix in ("[分享视频]", "[分享]"):
        if content.startswith(prefix):
            return content[len(prefix):].strip()
    return ""


def build_index(force: bool = False):
    """Build vector index for all messages. Runs in background thread."""
    if _index_state["status"] == "running":
        raise RuntimeError("Indexing already in progress")
    if _model is None:
        raise RuntimeError("Model not loaded")

    _index_state["status"] = "running"
    _index_state["progress"] = 0
    _index_state["message"] = "Preparing..."

    def _build():
        try:
            from backend.database import get_db
            conn = get_db()

            # Get text messages (type 1) and shares (type 4)
            rows = conn.execute(
                """SELECT msg_id, conv_id, sender_uid, content, timestamp, seq,
                          msg_type, raw_data
                   FROM messages
                   WHERE content IS NOT NULL AND content != ''
                     AND msg_type IN (1, 4)
                   ORDER BY seq"""
            ).fetchall()
            conn.close()

            total = len(rows)
            _index_state["total"] = total
            _index_state["message"] = f"Indexing {total} messages..."

            if total == 0:
                _index_state["status"] = "completed"
                _index_state["progress"] = 100
                _index_state["message"] = "No messages to index"
                return

            collection = _get_collection()

            # Check what's already indexed
            if not force:
                existing = set()
                try:
                    result = collection.get(include=[])
                    existing = set(result["ids"])
                except Exception:
                    pass
                rows = [r for r in rows if r[0] not in existing]
                if not rows:
                    _index_state["status"] = "completed"
                    _index_state["progress"] = 100
                    _index_state["indexed"] = total
                    _index_state["message"] = "All messages already indexed"
                    return
            else:
                # Force rebuild: clear collection using same client
                global _chroma_collection
                client = _get_client()
                try:
                    client.delete_collection("messages")
                except Exception:
                    pass
                _chroma_collection = None
                collection = _get_collection()

            # Batch encode and insert
            batch_size = 256
            processed = 0
            actually_indexed = 0
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]

                # Extract text content, skip non-text content
                _skip_prefixes = ("[表情", "[图片", "[语音", "[视频", "表情包")
                texts = []
                valid_batch = []
                for row in batch:
                    msg_type = row[6]
                    content = row[3]
                    raw_data = row[7]

                    if msg_type == 4:
                        # Share message: extract title from raw_data
                        text = _extract_share_text(raw_data, content)
                        if not text:
                            continue
                        content = text
                    else:
                        # Text message
                        if any(content.startswith(p) for p in _skip_prefixes):
                            continue
                        if content.startswith("{"):
                            try:
                                parsed = json.loads(content)
                                text = (parsed.get("content_title", "")
                                        or parsed.get("text", "")
                                        or parsed.get("desc", "") or "")
                                if not text:
                                    continue
                                content = text
                            except json.JSONDecodeError:
                                pass
                        elif content.startswith("["):
                            try:
                                parsed = json.loads(content)
                                text = parsed.get("text", "") or parsed.get("desc", "") or ""
                                if not text:
                                    continue
                                content = text
                            except json.JSONDecodeError:
                                pass

                    if len(content.strip()) < 2:
                        continue
                    texts.append(content[:512])
                    valid_batch.append(row)

                processed += len(batch)

                if not texts:
                    _index_state["progress"] = int(processed / len(rows) * 100)
                    continue

                actually_indexed += len(valid_batch)
                embeddings = _model.encode(texts, show_progress_bar=False).tolist()

                ids = [r[0] for r in valid_batch]
                metadatas = [
                    {
                        "conv_id": r[1],
                        "sender_uid": r[2] or "",
                        "timestamp": r[4] or 0,
                        "seq": r[5] or 0,
                    }
                    for r in valid_batch
                ]
                documents = texts

                collection.add(
                    ids=ids,
                    embeddings=embeddings,
                    metadatas=metadatas,
                    documents=documents,
                )

                _index_state["indexed"] = actually_indexed
                _index_state["progress"] = int(processed / len(rows) * 100)
                _index_state["message"] = f"Indexed {actually_indexed} messages ({processed}/{len(rows)} processed)"

            _index_state["status"] = "completed"
            _index_state["progress"] = 100
            _index_state["indexed"] = actually_indexed
            _index_state["message"] = f"Indexing complete: {actually_indexed} messages indexed"

        except Exception as e:
            _index_state["status"] = "failed"
            _index_state["message"] = f"Indexing failed: {e}"
            import traceback
            traceback.print_exc()

    thread = threading.Thread(target=_build, daemon=True)
    thread.start()


def search(query: str, conv_id: str = None, threshold: float = None):
    """Semantic search for messages, filtered by similarity threshold."""
    if _model is None:
        raise RuntimeError("Model not loaded")

    if threshold is None:
        cfg = _load_config()
        threshold = cfg.get("semantic_search", {}).get("threshold", 0.6)

    collection = _get_collection()
    if collection.count() == 0:
        return [], 0

    query_embedding = _model.encode([query], show_progress_bar=False).tolist()

    where = {"conv_id": conv_id} if conv_id else None

    results = collection.query(
        query_embeddings=query_embedding,
        n_results=500,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    items = []
    if results and results["ids"] and results["ids"][0]:
        from backend.database import get_db
        conn = get_db()

        for i, msg_id in enumerate(results["ids"][0]):
            distance = results["distances"][0][i]
            similarity = max(0, 1 - distance)

            if similarity < threshold:
                continue

            row = conn.execute(
                """SELECT m.*, c.name as conv_name,
                          COALESCE(u.nickname, m.sender_name, '') as sender_display_name
                   FROM messages m
                   JOIN conversations c ON m.conv_id = c.conv_id
                   LEFT JOIN users u ON m.sender_uid = u.uid
                   WHERE m.msg_id = ?""",
                (msg_id,),
            ).fetchone()

            if row:
                item = dict(row)
                item["similarity"] = round(similarity, 4)
                items.append(item)

        conn.close()

    return items, len(items)


def index_new_messages(msg_ids: list):
    """Index newly added messages (for incremental updates)."""
    if _model is None:
        return

    from backend.database import get_db
    conn = get_db()

    rows = conn.execute(
        f"""SELECT msg_id, conv_id, sender_uid, content, timestamp, seq,
                   msg_type, raw_data
            FROM messages
            WHERE msg_id IN ({','.join('?' * len(msg_ids))})
              AND content IS NOT NULL AND content != ''
              AND msg_type IN (1, 4)""",
        msg_ids,
    ).fetchall()
    conn.close()

    if not rows:
        return

    _skip_prefixes = ("[表情", "[图片", "[语音", "[视频", "表情包")
    texts = []
    valid_rows = []
    for row in rows:
        msg_type = row[6]
        content = row[3]
        raw_data = row[7]

        if msg_type == 4:
            text = _extract_share_text(raw_data)
            if not text:
                continue
            content = text
        else:
            if any(content.startswith(p) for p in _skip_prefixes):
                continue
            if content.startswith("{"):
                try:
                    parsed = json.loads(content)
                    text = (parsed.get("content_title", "")
                            or parsed.get("text", "")
                            or parsed.get("desc", "") or "")
                    if not text:
                        continue
                    content = text
                except json.JSONDecodeError:
                    pass
            elif content.startswith("["):
                try:
                    parsed = json.loads(content)
                    text = parsed.get("text", "") or parsed.get("desc", "") or ""
                    if not text:
                        continue
                    content = text
                except json.JSONDecodeError:
                    pass

        if len(content.strip()) < 2:
            continue
        texts.append(content[:512])
        valid_rows.append(row)

    if not texts:
        return

    embeddings = _model.encode(texts, show_progress_bar=False).tolist()
    collection = _get_collection()

    collection.add(
        ids=[r[0] for r in valid_rows],
        embeddings=embeddings,
        metadatas=[
            {"conv_id": r[1], "sender_uid": r[2] or "", "timestamp": r[4] or 0, "seq": r[5] or 0}
            for r in valid_rows
        ],
        documents=texts,
    )
