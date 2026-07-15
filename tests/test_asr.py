"""Contract tests for the Qwen3-ASR HTTP client."""
import os

import httpx

from extractor.asr import ASRResult, QwenASRClient, normalize_server_url


def test_normalize_server_url_accepts_base_or_full_endpoint():
    assert normalize_server_url("http://asr:8000/") == "http://asr:8000"
    assert normalize_server_url(
        "https://example.test/prefix/v1/audio/transcriptions/batch"
    ) == "https://example.test/prefix"


def test_batch_transcription_contract_and_optional_emotion(tmp_path):
    first = tmp_path / "a.wav"
    second = tmp_path / "b.wav"
    first.write_bytes(b"wav-a")
    second.write_bytes(b"wav-b")
    seen_body = b""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_body
        if request.url.path == "/prefix/healthz":
            return httpx.Response(200, json={"ok": True})
        assert request.url.path == "/prefix/v1/audio/transcriptions/batch"
        assert request.headers["content-type"].startswith("multipart/form-data")
        seen_body = request.read()
        return httpx.Response(
            200,
            json={
                "model": "Qwen/Qwen3-ASR-1.7B-hf",
                "results": [
                    {"index": 0, "text": "第一条", "language": "Chinese"},
                    {
                        "index": 1,
                        "text": "第二条",
                        "language": "Chinese",
                        "emotion": "happy",
                    },
                ],
            },
        )

    with QwenASRClient(
        "http://asr.test/prefix",
        language="Chinese",
        prompt="专有名词",
        transport=httpx.MockTransport(handler),
    ) as client:
        results, errors = client.transcribe_files([str(first), str(second)])

    assert errors == {}
    assert results[os.path.abspath(first)] == ASRResult(
        text="第一条", language="Chinese", model="Qwen/Qwen3-ASR-1.7B-hf"
    )
    assert results[os.path.abspath(second)].emotion == "happy"
    assert b'a.wav' in seen_body and b'b.wav' in seen_body
    assert "Chinese".encode() in seen_body and "专有名词".encode() in seen_body
    assert b'convert_audio' in seen_body and b'true' in seen_body


def test_failed_batch_falls_back_to_single_file_requests(tmp_path):
    files = [tmp_path / "a.wav", tmp_path / "b.wav"]
    for path in files:
        path.write_bytes(b"audio")
    single_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal single_calls
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("/batch"):
            return httpx.Response(400, json={"detail": "one file is invalid"})
        single_calls += 1
        return httpx.Response(
            200,
            json={"text": f"文本{single_calls}", "language": "Chinese"},
        )

    with QwenASRClient(
        "http://asr.test", transport=httpx.MockTransport(handler)
    ) as client:
        results, errors = client.transcribe_files(files)

    assert errors == {}
    assert single_calls == 2
    assert [results[os.path.abspath(path)].text for path in files] == ["文本1", "文本2"]


def test_one_file_uses_single_endpoint_without_batch(tmp_path):
    audio = tmp_path / "one.mpeg"
    audio.write_bytes(b"audio")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        assert request.url.path == "/v1/audio/transcriptions"
        return httpx.Response(200, json={"text": "单文件", "language": "Chinese"})

    with QwenASRClient(
        "http://asr.test", transport=httpx.MockTransport(handler)
    ) as client:
        results, errors = client.transcribe_files([audio])

    assert errors == {}
    assert results[os.path.abspath(audio)].text == "单文件"


def test_health_failure_is_reported_for_every_file(tmp_path):
    audio = tmp_path / "voice.mpeg"
    audio.write_bytes(b"audio")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "Model is not ready yet."})

    with QwenASRClient(
        "http://asr.test", transport=httpx.MockTransport(handler)
    ) as client:
        results, errors = client.transcribe_files([audio])

    assert results == {}
    assert "HTTP 503" in errors[os.path.abspath(audio)]
    assert "Model is not ready yet" in errors[os.path.abspath(audio)]


def test_missing_file_does_not_contact_server(tmp_path):
    missing = tmp_path / "missing.mpeg"

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("HTTP must not be called for missing files")

    with QwenASRClient(
        "http://asr.test", transport=httpx.MockTransport(handler)
    ) as client:
        results, errors = client.transcribe_files([missing])

    assert results == {}
    assert errors[os.path.abspath(missing)] == "本地语音文件不存在"


def test_batch_size_is_capped_at_ten_and_reports_progress(tmp_path):
    files = []
    for index in range(23):
        path = tmp_path / f"{index}.wav"
        path.write_bytes(b"audio")
        files.append(path)
    batch_sizes = []
    progress = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        body = request.read()
        count = body.count(b'name="files"')
        batch_sizes.append(count)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "text": f"文本{index}"}
                    for index in range(count)
                ]
            },
        )

    with QwenASRClient(
        "http://asr.test",
        batch_size=99,
        transport=httpx.MockTransport(handler),
    ) as client:
        results, errors = client.transcribe_files(files, progress_cb=progress.append)

    assert errors == {}
    assert len(results) == 23
    assert batch_sizes == [10, 10, 3]
    assert len(progress) == 23
    assert progress[-1]["done"] == 23
    assert progress[-1]["total"] == 23
