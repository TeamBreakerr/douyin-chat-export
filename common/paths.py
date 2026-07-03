"""Single source of truth for every filesystem path in the project.

Before this module, `os.path.join(os.path.dirname(os.path.dirname(__file__)), ...)`
was hand-rolled in 8+ places across backend/, extractor/, and the root scripts.
All state lives under DATA_DIR, which is the single bind-mount in Docker
(./data:/app/data) and is git-ignored.
"""
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(REPO_ROOT, "data")

# Databases / config
DB_PATH = os.path.join(DATA_DIR, "chat.db")
CONFIG_PATH = os.path.join(DATA_DIR, "panel_config.json")

# Logs & discovery artifacts
SCRAPE_LOG = os.path.join(DATA_DIR, "scrape.log")
DISCOVER_LOG = os.path.join(DATA_DIR, "discover.log")
CONVERSATIONS_LIST = os.path.join(DATA_DIR, "conversations_list.json")

# Browser profile (the persistent Chromium context that *is* the login state)
BROWSER_PROFILE = os.path.join(DATA_DIR, "browser_profile")

# Media tree
MEDIA_DIR = os.path.join(DATA_DIR, "media")
IMAGES_DIR = os.path.join(MEDIA_DIR, "images")
EMOJI_DIR = os.path.join(MEDIA_DIR, "emoji")
VOICE_DIR = os.path.join(MEDIA_DIR, "voice")
AVATARS_DIR = os.path.join(MEDIA_DIR, "avatars")
VIDEOS_DIR = os.path.join(MEDIA_DIR, "videos")

# Frontend build output served by the backend
FRONTEND_DIST = os.path.join(REPO_ROOT, "frontend", "dist")
