"""Client for the Qwen3-ASR Custom Server HTTP API.

The server intentionally resembles OpenAI's audio transcription path, but its
multipart fields and JSON response are project-specific.  Keeping that
contract in one small module makes the exporter easy to test and prevents HTTP
details from leaking into the ChatLab transformation code.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import json
import mimetypes
import os
from typing import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

import httpx


_SINGLE_PATH = "/v1/audio/transcriptions"
_BATCH_PATH = "/v1/audio/transcriptions/batch"
_KNOWN_ENDPOINTS = (_BATCH_PATH, _SINGLE_PATH, "/healthz")


class QwenASRError(RuntimeError):
    """Raised when the ASR server cannot produce a valid transcription."""


@dataclass(frozen=True)
class ASRResult:
    """Normalized subset of a Qwen3-ASR transcription response."""

    text: str
    language: str | None = None
    model: str | None = None
    # Qwen3-ASR itself currently does not expose emotion classification.  Keep
    # this optional field so a future/custom server extension can be consumed
    # without another exporter format change.
    emotion: str | None = None


def normalize_server_url(value: str) -> str:
    """Return a validated base URL, accepting a pasted full API endpoint too."""
    raw = (value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("ASR 服务地址必须是有效的 http:// 或 https:// URL")

    path = parsed.path.rstrip("/")
    for endpoint in _KNOWN_ENDPOINTS:
        if path.endswith(endpoint):
            path = path[: -len(endpoint)].rstrip("/")
            break

    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _response_error(response: httpx.Response) -> str:
    """Extract FastAPI's useful error detail without dumping an HTML page."""
    detail = None
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("error")
    except (ValueError, json.JSONDecodeError):
        pass

    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, ensure_ascii=False)
    if not detail:
        detail = (response.text or response.reason_phrase or "未知错误").strip()
    detail = str(detail).replace("\r", " ").replace("\n", " ")[:500]
    return f"ASR 服务返回 HTTP {response.status_code}: {detail}"


def _optional_text(payload: dict, *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


class QwenASRClient:
    """Synchronous client used by the synchronous export pipeline.

    Files are sent to the batch endpoint in bounded groups.  If a batch is
    rejected (for example, one file is corrupt), it is retried file-by-file so
    one bad voice message does not discard all other transcriptions.
    """

    def __init__(
        self,
        server_url: str,
        *,
        language: str | None = "Chinese",
        prompt: str = "",
        timeout: float = 300,
        batch_size: int = 10,
        transport: httpx.BaseTransport | None = None,
        trust_env: bool = False,
    ):
        self.server_url = normalize_server_url(server_url)
        self.language = (language or "").strip() or None
        self.prompt = (prompt or "").strip()
        if timeout <= 0:
            raise ValueError("ASR 超时时间必须大于 0 秒")
        if batch_size <= 0:
            raise ValueError("ASR 批量大小必须大于 0")
        self.timeout = float(timeout)
        self.batch_size = min(int(batch_size), 10)
        self._client = httpx.Client(
            base_url=self.server_url.rstrip("/") + "/",
            timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
            follow_redirects=True,
            headers={"User-Agent": "douyin-chat-export/qwen3-asr"},
            transport=transport,
            # ASR servers are commonly reached over localhost, Docker DNS, a
            # LAN, or a private overlay network.  System HTTP_PROXY settings
            # can turn those otherwise valid requests into proxy-side 502s and
            # may disclose private audio to an unintended proxy.
            trust_env=trust_env,
        )

    def __enter__(self) -> "QwenASRClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def health(self) -> dict:
        """Check that the service and model are actually ready."""
        try:
            response = self._client.get("healthz")
        except httpx.TimeoutException as exc:
            raise QwenASRError(f"ASR 健康检查超时（{self.timeout:g} 秒）") from exc
        except httpx.RequestError as exc:
            raise QwenASRError(f"无法连接 ASR 服务: {exc}") from exc

        if response.status_code >= 400:
            raise QwenASRError(_response_error(response))
        try:
            payload = response.json()
        except ValueError as exc:
            raise QwenASRError("ASR 健康检查未返回 JSON") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise QwenASRError("ASR 服务尚未就绪")
        return payload

    def transcribe_files(
        self,
        file_paths: Iterable[str],
        progress_cb: Callable[[dict], None] | None = None,
    ) -> tuple[dict[str, ASRResult], dict[str, str]]:
        """Transcribe files and return ``(successful_results, errors)``.

        Both dictionaries use normalized absolute file paths as keys. Missing
        files are reported without making an HTTP request.
        """
        ordered_paths: list[str] = []
        seen: set[str] = set()
        errors: dict[str, str] = {}
        missing_paths: list[str] = []
        completed = 0

        def report(
            path: str,
            *,
            result: ASRResult | None = None,
            error: str | None = None,
        ) -> None:
            nonlocal completed
            completed += 1
            if not progress_cb:
                return
            try:
                progress_cb({
                    "path": path,
                    "result": result,
                    "error": error,
                    "done": completed,
                    "total": len(seen),
                })
            except Exception:
                # UI progress must never be able to break transcription.
                pass

        for raw_path in file_paths:
            path = os.path.abspath(os.fspath(raw_path))
            if path in seen:
                continue
            seen.add(path)
            if not os.path.isfile(path):
                errors[path] = "本地语音文件不存在"
                missing_paths.append(path)
                continue
            ordered_paths.append(path)

        for path in missing_paths:
            report(path, error=errors[path])

        results: dict[str, ASRResult] = {}
        if not ordered_paths:
            return results, errors

        try:
            self.health()
        except QwenASRError as exc:
            for path in ordered_paths:
                errors[path] = str(exc)
                report(path, error=errors[path])
            return results, errors

        batch_available = self.batch_size > 1
        for start in range(0, len(ordered_paths), self.batch_size):
            batch = ordered_paths[start : start + self.batch_size]
            if batch_available and len(batch) > 1:
                try:
                    batch_results = self._transcribe_batch(batch)
                    results.update(batch_results)
                    for path in batch:
                        report(path, result=batch_results[path])
                    continue
                except QwenASRError:
                    # If the deployment does not support/has broken the batch
                    # endpoint, do not pay the same failure latency again for
                    # every later group in a large export.
                    batch_available = False

            # Salvage valid files when a multi-file request failed because of
            # one malformed upload/response item, or use the single endpoint
            # directly when this group contains one file.
            for path in batch:
                try:
                    results[path] = self._transcribe_one(path)
                    report(path, result=results[path])
                except QwenASRError as exc:
                    errors[path] = str(exc)
                    report(path, error=errors[path])

        return results, errors

    def _form_data(self) -> dict[str, str]:
        # Douyin voice files are commonly AAC in an MP4 container despite the
        # *.mpeg filename. Ask the custom server to normalize the upload before
        # soundfile/Transformers attempts to decode it.
        data: dict[str, str] = {"convert_audio": "true"}
        if self.language:
            data["language"] = self.language
        if self.prompt:
            data["prompt"] = self.prompt
        return data

    @staticmethod
    def _file_tuple(path: str, handle) -> tuple[str, object, str]:
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return os.path.basename(path), handle, mime

    def _request(self, path: str, *, files, data: dict[str, str]) -> httpx.Response:
        try:
            response = self._client.post(path.lstrip("/"), files=files, data=data)
        except httpx.TimeoutException as exc:
            raise QwenASRError(f"ASR 转写超时（{self.timeout:g} 秒）") from exc
        except httpx.RequestError as exc:
            raise QwenASRError(f"ASR 请求失败: {exc}") from exc
        if response.status_code >= 400:
            raise QwenASRError(_response_error(response))
        return response

    @staticmethod
    def _parse_result(payload: object, *, model: str | None = None) -> ASRResult:
        if not isinstance(payload, dict):
            raise QwenASRError("ASR 响应中的转写结果不是 JSON 对象")
        text = payload.get("text")
        if not isinstance(text, str):
            raise QwenASRError("ASR 响应缺少字符串字段 text")
        return ASRResult(
            text=text.strip(),
            language=_optional_text(payload, "language"),
            model=_optional_text(payload, "model") or model,
            emotion=_optional_text(payload, "emotion", "emotion_label"),
        )

    def _transcribe_one(self, path: str) -> ASRResult:
        with open(path, "rb") as handle:
            response = self._request(
                _SINGLE_PATH,
                files={"file": self._file_tuple(path, handle)},
                data=self._form_data(),
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise QwenASRError("ASR 单文件接口未返回 JSON") from exc
        return self._parse_result(payload)

    def _transcribe_batch(self, paths: list[str]) -> dict[str, ASRResult]:
        with ExitStack() as stack:
            multipart = [
                ("files", self._file_tuple(path, stack.enter_context(open(path, "rb"))))
                for path in paths
            ]
            response = self._request(_BATCH_PATH, files=multipart, data=self._form_data())

        try:
            payload = response.json()
        except ValueError as exc:
            raise QwenASRError("ASR 批量接口未返回 JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise QwenASRError("ASR 批量响应缺少 results 数组")

        model = _optional_text(payload, "model")
        indexed: dict[int, dict] = {}
        for item in payload["results"]:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                raise QwenASRError("ASR 批量响应包含无效的 index")
            indexed[item["index"]] = item
        if set(indexed) != set(range(len(paths))):
            raise QwenASRError("ASR 批量响应数量或顺序不完整")

        return {
            path: self._parse_result(indexed[index], model=model)
            for index, path in enumerate(paths)
        }
