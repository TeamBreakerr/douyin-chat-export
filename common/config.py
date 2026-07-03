"""Load/save the panel config (data/panel_config.json).

Centralizes the atomic write + corrupt-file fallback that previously lived in
`backend/control_panel.py` (`_load_config`/`_save_config`) and the password-hash
read in `backend/main.py` (`_get_password_hash`). Reads `common.paths.CONFIG_PATH`
at call time so tests can repoint it.
"""
import json
import os

from common import paths

_DEFAULTS = {"custom_filters": [], "schedule": ""}


def load_config() -> dict:
    path = paths.CONFIG_PATH
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # Don't let a corrupted config file block service startup —
            # fall back to defaults and let the next save overwrite it.
            print(f"[!] panel_config.json 损坏 ({e})，使用默认配置")
            return dict(_DEFAULTS)
    return dict(_DEFAULTS)


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
