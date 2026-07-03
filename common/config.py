"""Load/save the panel config (data/panel_config.json).

Centralizes the atomic write + corrupt-file fallback that previously lived in
`backend/control_panel.py` (`_load_config`/`_save_config`) and the password-hash
read in `backend/main.py` (`_get_password_hash`). Reads `common.paths.CONFIG_PATH`
at call time so tests can repoint it.
"""
import json
import os

from common import paths

def _defaults() -> dict:
    # A fresh dict (with a fresh list) each call, so callers that mutate the
    # returned config in place can't pollute a shared default.
    return {"custom_filters": [], "schedule": ""}


def load_config() -> dict:
    # Open-and-catch (no os.path.exists pre-check) so a file deleted mid-call
    # can't raise FileNotFoundError out to the auth path as an HTTP 500.
    try:
        with open(paths.CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return _defaults()  # no config yet — normal, stay silent
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # Don't let a corrupted config file block service startup —
        # fall back to defaults and let the next save overwrite it.
        print(f"[!] panel_config.json 损坏 ({e})，使用默认配置")
        return _defaults()


def save_config(cfg: dict) -> None:
    path = paths.CONFIG_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Atomic write: serialize to tmp file then rename, so a crash mid-write
    # (e.g. UnicodeEncodeError on Windows gbk) can't leave a half-written file.
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    os.replace(tmp_path, path)


def get_password_hash() -> str | None:
    """Return the configured password hash, or None if unset/missing/corrupt."""
    return load_config().get("password_hash") or None
