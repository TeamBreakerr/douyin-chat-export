"""Native Douyin IM voice-message transcription.

The web IM client exposes a cookie-authenticated recognition endpoint.  This
module keeps the request/response normalization and persistence separate from
the Playwright scraper so it can be tested without a logged-in browser.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from common.db import upsert_voice_transcription


AUDIO_RECOGNITION_API = (
    "https://www.douyin.com/aweme/v1/web/im/message/audio/recognition/"
)
# The web endpoint accepts at most ten items per req_list (larger payloads
# return status_code=5/"参数不合法").
VOICE_RECOGNITION_BATCH_SIZE = 10
VOICE_MESSAGE_TYPE = 7


class VoiceRequestError(ValueError):
    """The message does not contain all fields required by the IM endpoint."""


@dataclass(frozen=True)
class VoiceRecognitionRequest:
    """One endpoint request item and its local message identity."""

    msg_id: str
    message_id: str
    uri: str
    sec_uid: str
    uuid: str
    skey: str
    conv_short_id: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "sec_uid": self.sec_uid,
            "uuid": self.uuid,
            "message_id": self.message_id,
            "message_type": VOICE_MESSAGE_TYPE,
            "skey": self.skey,
            "conv_short_id": self.conv_short_id,
        }


def _value(message: Any, key: str, default: Any = None) -> Any:
    """Read a key from either a dict or sqlite3.Row."""
    if isinstance(message, Mapping):
        return message.get(key, default)
    try:
        return message[key]
    except (KeyError, IndexError, TypeError):
        return default


def _as_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def message_content_json(message: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return the stored raw message object and decoded ``content_json``.

    Current scraper rows store a JSON object in ``raw_data`` whose
    ``content_json`` value is itself JSON-encoded.  The fallbacks keep older
    rows and direct test fixtures usable as well.
    """
    raw = _as_json(_value(message, "raw_data"))
    if not isinstance(raw, dict):
        raw = {}

    content = _as_json(raw.get("content_json"))
    if not isinstance(content, dict):
        content = _as_json(_value(message, "content_json"))
    if not isinstance(content, dict):
        content = _as_json(_value(message, "content"))
    return raw, content if isinstance(content, dict) else None


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def _resource_value(resource: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = resource.get(key)
        if isinstance(value, (list, tuple)):
            for item in value:
                item_text = _text(item)
                if item_text:
                    return item_text
        else:
            item_text = _text(value)
            if item_text:
                return item_text
    return ""


def _voice_resource(message: Any, content: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(content, dict):
        return None
    resource = content.get("resource_url")
    if resource is None and content.get("tkey"):
        # Older message variants expose the audio identifier as tkey instead
        # of nesting it under resource_url.
        resource = {}
    elif isinstance(resource, str):
        # A few legacy rows kept only the CDN/URI string.
        resource = {"url": resource} if resource.strip() else None
    if not isinstance(resource, dict):
        return None

    duration = content.get("duration")
    if duration in (None, ""):
        duration = resource.get("duration")

    # A resource_url also occurs on image messages.  Normal scraper rows use
    # msg_type=0/"other" for voices, but older versions could classify an
    # aweType=0 voice as text (msg_type=1).  A duration, tkey, or explicit
    # voice marker is strong enough to recognize those legacy rows without
    # treating ordinary image resources as voice messages.
    msg_type = _value(message, "msg_type")
    stored_as_voice = (
        msg_type is None
        or str(msg_type).lower() in {"0", "other"}
    )
    has_voice_marker = (
        duration not in (None, "")
        or bool(resource.get("is_voice"))
        or bool(_text(content.get("tkey")))
        or bool(_text(content.get("voice_wave")))
        or bool(_text(content.get("ai_audio_text")))
    )
    video = content.get("video")
    has_video_marker = isinstance(video, dict) and bool(video.get("vid"))
    explicit_voice_marker = (
        bool(resource.get("is_voice"))
        or bool(_text(content.get("tkey")))
        or bool(_text(content.get("voice_wave")))
        or bool(_text(content.get("ai_audio_text")))
    )
    if (
        str(content.get("aweType")) in {"2702", "2703", "2704"}
        and not explicit_voice_marker
    ):
        return None
    if has_video_marker and not explicit_voice_marker:
        return None
    if not stored_as_voice and not has_voice_marker:
        return None
    uri = _resource_value(
        resource,
        "uri",
        "url",
        "url_list",
        "large_url_list",
        "origin_url_list",
        "medium_url_list",
        "thumb_url_list",
    )
    if not uri and content:
        uri = _resource_value(
            content,
            "uri",
            "url",
            "url_list",
            "tkey",
        )
    if not uri:
        return None
    if (
        duration in (None, "")
        and not resource.get("is_voice")
        and not _text(content.get("tkey"))
    ):
        return None
    return resource


def is_voice_message(message: Any) -> bool:
    """Return whether a stored message has a recognizable voice payload."""
    _raw, content = message_content_json(message)
    return _voice_resource(message, content) is not None


def _remote_message_id(message: Any, raw: Mapping[str, Any]) -> str:
    for key in ("message_id", "server_id", "msg_id"):
        value = _text(raw.get(key)) or _text(_value(message, key))
        if value:
            if value.startswith("srv_"):
                value = value[4:]
            return value
    value = _text(_value(message, "msg_id"))
    return value[4:] if value.startswith("srv_") else value


def build_voice_request(
    message: Any, *, conv_short_id: str, self_uuid: str
) -> VoiceRecognitionRequest:
    """Extract the exact ``req_list`` item expected by Douyin."""
    raw, content = message_content_json(message)
    resource = _voice_resource(message, content)
    if resource is None:
        raise VoiceRequestError("not a voice message")

    # Prefer the canonical URI from the resource object.  URL-list fallbacks
    # cover historical payloads where only the CDN URL was retained.
    uri = _resource_value(
        resource,
        "uri",
        "url",
        "url_list",
        "large_url_list",
        "origin_url_list",
        "medium_url_list",
        "thumb_url_list",
    )
    if not uri and content:
        uri = _resource_value(content, "uri", "url", "url_list", "tkey")
    # ``skey`` is present on some resource variants, but the historical voice
    # payloads returned by the web IM API commonly omit it.  The recognition
    # endpoint still expects the key in each item; an empty value is accepted
    # for those payloads, so keep it in the request instead of discarding the
    # message as malformed.
    skey = (
        _text(resource.get("skey"))
        or _text(content.get("skey") if content else None)
        or _text(raw.get("skey"))
    )
    sec_uid = (
        _text(raw.get("sender_sec_uid"))
        or _text(raw.get("sec_uid"))
        or _text(_value(message, "sender_sec_uid"))
        or _text(content.get("sec_uid") if content else None)
    )
    message_id = _remote_message_id(message, raw)
    msg_id = _text(_value(message, "msg_id"))
    self_uuid = _text(self_uuid)
    conv_short_id = _text(conv_short_id)
    missing = [
        name
        for name, value in (
            ("uri", uri),
            ("sec_uid", sec_uid),
            ("uuid", self_uuid),
            ("message_id", message_id),
            ("conv_short_id", conv_short_id),
            ("msg_id", msg_id),
        )
        if not value
    ]
    if missing:
        raise VoiceRequestError("missing fields: " + ", ".join(missing))

    return VoiceRecognitionRequest(
        msg_id=msg_id,
        message_id=message_id,
        uri=uri,
        sec_uid=sec_uid,
        uuid=self_uuid,
        skey=skey,
        conv_short_id=conv_short_id,
    )


SELF_UUID_EVAL_SCRIPT = """() => {
    // The official IM client sends mainOptions.deviceId || mainOptions.uuid.
    // mainOptions is held by the React context rather than exposed as a
    // stable window global, so inspect the provider props as a fallback.
    const text = (value) => value == null ? '' : String(value).trim();
    const fromProps = (props) => {
        if (!props || typeof props !== 'object') return '';
        const main = props.mainOptions || props.value?.mainOptions;
        if (main && typeof main === 'object') {
            const id = text(main.deviceId || main.uuid);
            if (id) return id;
        }
        const options = props.options;
        if (options && typeof options === 'object') {
            const id = text(options.deviceId || options.uuid);
            if (id) return id;
        }
        // Some builds pass the context value itself as the props object.
        const directId = text(props.deviceId || props.uuid);
        if (directId) return directId;
        return '';
    };

    const direct = [
        window.__IM_MAIN_OPTIONS__,
        window.mainOptions,
        window.imMainOptions,
        window.userInfoStore && window.userInfoStore.mainOptions,
        window.__INITIAL_STATE__ && window.__INITIAL_STATE__.mainOptions,
    ];
    for (const item of direct) {
        const id = fromProps({mainOptions: item});
        if (id) return id;
    }

    const fiberSeen = new WeakSet();
    const walkFiber = (fiber, depth) => {
        if (!fiber || depth > 120 || fiberSeen.has(fiber)) return '';
        fiberSeen.add(fiber);
        for (const key of ['pendingProps', 'memoizedProps']) {
            const id = fromProps(fiber[key]);
            if (id) return id;
        }
        // A few React versions retain the provider value in a hook state.
        let state = fiber.memoizedState;
        for (let i = 0; state && i < 24; i++, state = state.next) {
            const id = fromProps(state) || fromProps(state.memoizedState);
            if (id) return id;
        }
        return walkFiber(fiber.return, depth + 1);
    };

    const nodes = [document.body, document.documentElement];
    if (document.querySelectorAll) {
        const all = document.querySelectorAll('*');
        for (let i = 0; i < all.length && i < 2000; i++) nodes.push(all[i]);
    }
    for (const node of nodes) {
        if (!node) continue;
        for (const key of Object.keys(node)) {
            if (!key.startsWith('__reactFiber$')) continue;
            const id = walkFiber(node[key], 0);
            if (id) return id;
        }
    }
    const me = window.userInfoStore && window.userInfoStore.curLoginUserInfo;
    if (me) return text(me.uuid || '');
    return '';
}"""


FETCH_RECOGNITION_EVAL_SCRIPT = """async (args) => {
    const [api, reqList] = args;
    try {
        const response = await fetch(api, {
            method: 'POST',
            credentials: 'include',
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            },
            body: JSON.stringify({req_list: reqList}),
        });
        const text = await response.text();
        let body = null;
        try { body = text ? JSON.parse(text) : null; } catch {}
        return {status: response.status, body};
    } catch (error) {
        return {status: 0, body: null, error: String(error && error.message || error)};
    }
}"""


def _normal_id(value: Any) -> str:
    value = _text(value)
    return value[4:] if value.startswith("srv_") else value


def _numeric(value: Any) -> int | None:
    try:
        if isinstance(value, bool) or value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _error_from_object(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    for key in ("status_code", "code", "error_code", "errno"):
        code = _numeric(obj.get(key))
        success_codes = (0, 200) if key in {"status_code", "code"} else (0,)
        if code is not None and code not in success_codes:
            return (
                _text(obj.get("status_msg"))
                or _text(obj.get("error_msg"))
                or _text(obj.get("message"))
                or _text(obj.get("msg"))
                or f"api error {code}"
            )
    status = obj.get("status")
    status_code = _numeric(status)
    # Some builds expose the HTTP-like status in the JSON body instead of
    # using status_code=0.  Treat 2xx values as successful metadata too.
    if status_code is not None and not (
        status_code == 0 or 200 <= status_code < 300
    ):
        return _text(obj.get("message")) or _text(obj.get("msg")) or f"status {status}"
    if isinstance(status, str) and status.lower() in {"failed", "fail", "error"}:
        return _text(obj.get("message")) or _text(obj.get("msg")) or status
    for key in ("error", "error_msg"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # ``message``/``msg``/``status_msg`` are often informational success
    # fields.  Do not turn them into errors unless an explicit non-success
    # status above already established that this is an error object.
    return None


def _looks_like_result_item(obj: dict[str, Any]) -> bool:
    return any(
        key in obj
        for key in (
            "text_result",
            "message_id",
            "msg_id",
            "server_id",
            "error_code",
            "error_msg",
        )
    )


def _collect_result_items(value: Any) -> list[dict[str, Any]]:
    """Find per-message result objects across known response wrappers."""
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict) and _looks_like_result_item(item):
                result.append(item)
            elif isinstance(item, str):
                result.append({"text_result": item})
            else:
                result.extend(_collect_result_items(item))
        return result
    if not isinstance(value, dict):
        return []
    if "text_result" in value:
        return [value]

    result: list[dict[str, Any]] = []
    for key in (
        "res_list",
        "recognition_results",
        "result",
        "results",
        "data",
        "items",
        "list",
        "req_list",
    ):
        if key in value:
            result.extend(_collect_result_items(value[key]))
    if result:
        return result

    # A few response versions use {message_id: text_result} under data.  Only
    # accept keys that look like message IDs; otherwise an error wrapper such
    # as {"error": "unauthorized"} would be mistaken for a transcription.
    def is_message_id_key(key: Any) -> bool:
        key_text = _normal_id(key)
        return (
            key_text.isdigit()
            or key_text.startswith("msg_")
            or key_text.startswith("srv_")
            or (len(key_text) >= 16 and key_text.count("-") >= 2)
        )

    for key, item in value.items():
        if not is_message_id_key(key):
            continue
        if isinstance(item, str):
            result.append({"message_id": key, "text_result": item})
        elif isinstance(item, dict):
            nested = dict(item)
            nested.setdefault("message_id", key)
            if _looks_like_result_item(nested):
                result.append(nested)
            else:
                result.extend(_collect_result_items(item))
        elif isinstance(item, list):
            result.extend(_collect_result_items(item))
    return result


def _item_message_id(item: dict[str, Any]) -> str:
    for key in ("message_id", "msg_id", "server_id", "id"):
        value = _normal_id(item.get(key))
        if value:
            return value
    return ""


def parse_recognition_response(
    response: Any, requests: list[VoiceRecognitionRequest]
) -> dict[str, dict[str, str]]:
    """Normalize the endpoint response and correlate results to local IDs.

    Results are correlated by ``message_id`` when present and fall back to
    request order for older response shapes.  An empty ``text_result`` with no
    error is still a successful recognition and is persisted as such.
    """
    requests = list(requests)
    if isinstance(response, dict) and "body" in response:
        http_status = _numeric(response.get("status"))
        body = response.get("body")
        transport_error = _text(response.get("error"))
    else:
        http_status = 200
        body = response
        transport_error = ""

    global_error = transport_error or _error_from_object(body)
    if http_status is not None and not 200 <= http_status < 300:
        global_error = global_error or f"HTTP {http_status}"

    # A transport or top-level API error invalidates every item in the batch,
    # even if a malformed/error response happens to contain a text_result.
    # Persisting such text as success would prevent a later retry.
    if global_error:
        return {
            request.msg_id: {
                "message_id": request.message_id,
                "status": "failed",
                "text_result": "",
                "error": global_error,
            }
            for request in requests
        }

    items = _collect_result_items(body)
    request_remote_ids = {_normal_id(request.message_id) for request in requests}
    by_remote: dict[str, dict[str, Any]] = {}
    positional: list[dict[str, Any]] = []
    has_identified_item = False
    for item in items:
        remote_id = _item_message_id(item)
        if remote_id:
            has_identified_item = True
            if remote_id in request_remote_ids:
                by_remote.setdefault(remote_id, item)
        else:
            positional.append(item)

    # Positional correlation is safe only when the server returned a
    # completely ID-less, one-for-one list.  Do not let an unknown ID or a
    # mixed response shift one transcript onto another message.
    allow_positional = (
        not has_identified_item and len(items) == len(requests)
    )

    output: dict[str, dict[str, str]] = {}
    used_positional = 0
    for request in requests:
        item = by_remote.get(_normal_id(request.message_id))
        if allow_positional and item is None and used_positional < len(positional):
            item = positional[used_positional]
            used_positional += 1

        if item is None:
            output[request.msg_id] = {
                "message_id": request.message_id,
                "status": "failed",
                "text_result": "",
                "error": global_error or "response missing result",
            }
            continue

        item_error = _error_from_object(item)
        raw_text_result = item.get("text_result")
        has_text = isinstance(raw_text_result, str)
        text_result = raw_text_result.strip() if has_text else ""
        if item_error:
            status = "failed"
            error = item_error
        elif not has_text:
            status = "failed"
            error = "response item missing string text_result"
        else:
            status = "success"
            error = ""
        output[request.msg_id] = {
            "message_id": request.message_id,
            "status": status,
            "text_result": text_result,
            "error": error,
        }
    return output


def pending_voice_rows(conn: Any, conversation_names: list[str] | None = None) -> list[Any]:
    """Return likely pending voice rows already stored in the local DB.

    The cheap SQL predicates keep text/image/video rows out of the Python
    pass.  ``is_voice_message`` is still applied by the caller because some
    historical payloads share the same ``resource_url`` field as images.
    This query is used only by the explicit historical backfill task.
    """
    params: list[Any] = []
    where = (
        "m.msg_type IN (0, 1) "
        "AND m.raw_data IS NOT NULL "
        "AND m.raw_data LIKE '%resource_url%' "
        "AND (vt.status IS NULL OR vt.status <> 'success')"
    )
    if conversation_names:
        clauses = []
        for name in conversation_names:
            name = _text(name)
            if name:
                clauses.append("c.name LIKE ?")
                params.append(f"%{name}%")
        if clauses:
            where += " AND (" + " OR ".join(clauses) + ")"
    return conn.execute(
        f"""SELECT m.*, c.name AS conversation_name,
                         vt.status AS voice_transcription_status
                  FROM messages m
                  JOIN conversations c ON c.conv_id = m.conv_id
             LEFT JOIN voice_transcriptions vt ON vt.msg_id = m.msg_id
                 WHERE {where}
              ORDER BY m.conv_id ASC, m.seq ASC""",
        params,
    ).fetchall()


class VoiceTranscriber:
    """Batch native recognition requests for messages in one conversation."""

    def __init__(
        self,
        page: Any,
        conn: Any,
        *,
        batch_size: int = VOICE_RECOGNITION_BATCH_SIZE,
        api_url: str = AUDIO_RECOGNITION_API,
    ):
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.page = page
        self.conn = conn
        self.batch_size = batch_size
        self.api_url = api_url

    async def _get_self_uuid(self) -> str:
        value = await self.page.evaluate(SELF_UUID_EVAL_SCRIPT)
        if isinstance(value, dict):
            value = (
                value.get("uuid")
                or value.get("device_id")
                or value.get("deviceId")
            )
        return _text(value)

    @staticmethod
    def _fallback_message_id(row: Any) -> str:
        value = _text(_value(row, "msg_id"))
        return value[4:] if value.startswith("srv_") else value

    def _save(self, request: VoiceRecognitionRequest, result: dict[str, str]) -> None:
        upsert_voice_transcription(
            self.conn,
            request.msg_id,
            result.get("message_id") or request.message_id,
            result.get("text_result", ""),
            result.get("status", "failed"),
            result.get("error") or None,
        )

    async def transcribe_conversation(
        self,
        conv_id: str,
        conv_short_id: str,
        self_uuid: str | None = None,
        message_ids: list[str] | None = None,
    ) -> dict[str, int]:
        """Transcribe a conversation, or only the supplied message IDs.

        The full-conversation form is reserved for an explicit historical
        backfill.  Normal incremental scraping passes the IDs seen in its API
        batches so it does not rescan an entire conversation on every run.
        """
        if message_ids is not None and not message_ids:
            return {"voices": 0, "cached": 0, "requested": 0,
                    "succeeded": 0, "failed": 0, "skipped": 0}
        if message_ids is not None:
            ids = list(dict.fromkeys(_text(value) for value in message_ids if _text(value)))
            if not ids:
                return {"voices": 0, "cached": 0, "requested": 0,
                        "succeeded": 0, "failed": 0, "skipped": 0}
            rows = []
            # Stay below SQLite's common 999-bound-variable limit on full
            # scrapes containing many voice messages.
            for start in range(0, len(ids), 900):
                chunk = ids[start:start + 900]
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(self.conn.execute(
                    f"""SELECT m.*, vt.status AS voice_transcription_status
                           FROM messages m
                      LEFT JOIN voice_transcriptions vt ON vt.msg_id = m.msg_id
                          WHERE m.conv_id = ?
                            AND m.msg_id IN ({placeholders})
                       ORDER BY m.seq ASC""",
                    [conv_id, *chunk],
                ).fetchall())
            rows.sort(key=lambda row: row["seq"] or 0)
        else:
            rows = self.conn.execute(
                """SELECT m.*, vt.status AS voice_transcription_status
                     FROM messages m
                LEFT JOIN voice_transcriptions vt ON vt.msg_id = m.msg_id
                    WHERE m.conv_id = ?
                 ORDER BY m.seq ASC""",
                (conv_id,),
            ).fetchall()
        return await self.transcribe_rows(rows, conv_short_id, self_uuid)

    async def transcribe_rows(
        self,
        rows: list[Any],
        conv_short_id: str,
        self_uuid: str | None = None,
    ) -> dict[str, int]:
        """Transcribe the supplied database rows without another DB scan."""
        stats = {"voices": 0, "cached": 0, "requested": 0,
                 "succeeded": 0, "failed": 0, "skipped": 0}
        candidates: list[Any] = []
        for row in rows:
            if not is_voice_message(row):
                continue
            stats["voices"] += 1
            if _text(_value(row, "voice_transcription_status")) == "success":
                stats["cached"] += 1
            else:
                candidates.append(row)

        if not candidates:
            return stats

        self_uuid = _text(self_uuid)
        if not self_uuid:
            try:
                self_uuid = await self._get_self_uuid()
            except Exception as exc:
                missing_uuid_error = f"self uuid lookup failed: {exc}"
            else:
                missing_uuid_error = "missing self uuid"
        else:
            missing_uuid_error = "missing self uuid"
        requests: list[VoiceRecognitionRequest] = []
        for row in candidates:
            try:
                request = build_voice_request(
                    row, conv_short_id=conv_short_id, self_uuid=self_uuid
                )
            except VoiceRequestError as exc:
                msg_id = _text(_value(row, "msg_id"))
                upsert_voice_transcription(
                    self.conn,
                    msg_id,
                    self._fallback_message_id(row),
                    "",
                    "skipped",
                    str(exc) if self_uuid else missing_uuid_error,
                )
                stats["skipped"] += 1
            else:
                requests.append(request)

        self.conn.commit()
        stats["requested"] = len(requests)
        for start in range(0, len(requests), self.batch_size):
            batch = requests[start:start + self.batch_size]
            payload = [request.as_payload() for request in batch]
            try:
                response = await self.page.evaluate(
                    FETCH_RECOGNITION_EVAL_SCRIPT,
                    [self.api_url, payload],
                )
                parsed = parse_recognition_response(response, batch)
            except Exception as exc:
                parsed = {
                    request.msg_id: {
                        "message_id": request.message_id,
                        "status": "failed",
                        "text_result": "",
                        "error": str(exc),
                    }
                    for request in batch
                }

            for request in batch:
                result = parsed.get(request.msg_id) or {
                    "message_id": request.message_id,
                    "status": "failed",
                    "text_result": "",
                    "error": "missing normalized result",
                }
                self._save(request, result)
                if result.get("status") == "success":
                    stats["succeeded"] += 1
                else:
                    stats["failed"] += 1
            self.conn.commit()

        return stats
