"""Shared message-type predicates used by scraping and data maintenance."""


MESSAGE_TYPE_CODES = {
    "other": 0,
    "text": 1,
    "emoji": 2,
    "image": 3,
    "share": 4,
    "video": 5,
    "merged_forward": 6,
}


def is_video_note_payload(content):
    """Return whether *content* is a direct-message video note payload."""
    if not isinstance(content, dict):
        return False
    video = content.get("video")
    return isinstance(video, dict) and bool(video.get("vid"))


def is_merged_forward_payload(content):
    """Return whether *content* describes a merged-forward message."""
    if not isinstance(content, dict) or is_video_note_payload(content):
        return False
    if str(content.get("aweType", "")) == "13600":
        return True
    return any(
        isinstance(content.get(key), list) and bool(content.get(key))
        for key in ("list_content", "msg_ids")
    )


def message_type_code(kind):
    """Translate the scraper's symbolic type to the persisted integer code."""
    return MESSAGE_TYPE_CODES.get(kind, 0)
