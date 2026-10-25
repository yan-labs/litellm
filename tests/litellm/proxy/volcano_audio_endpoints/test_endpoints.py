"""Mock-only Doubao gateway tests for streaming and asynchronous ASR.

The route functions are called directly with fake requests and provider HTTP;
no proxy app, live credentials, or paid provider request is used.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from litellm.proxy.volcano_audio_endpoints import endpoints as audio


class _Request:
    """Provide only the body and request id consumed by media endpoints."""

    headers = {"X-Request-Id": "request-1"}

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    async def stream(self):
        yield json.dumps(self.body).encode()


@pytest.fixture(autouse=True)
def _mock_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept a fake virtual key while keeping provider HTTP mocked."""
    async def permit(_alias: str, _key: Any) -> None:
        return None
    monkeypatch.setattr(audio.common, "require_model_access", permit)
    monkeypatch.setenv("VOLCANO_AUDIO_APP_ID", "mock-app")
    monkeypatch.setenv("VOLCANO_AUDIO_ACCESS_KEY", "mock-access")
    monkeypatch.setenv("LITELLM_SALT_KEY", "unit-test-salt")


class _Stream:
    """Yield split JSON lines like the provider's chunked TTS response."""

    status_code = 200

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        pass

    async def aiter_bytes(self):
        data = json.dumps({"code": 0, "data": base64.b64encode(b"audio").decode()}).encode() + b"\n"
        yield data[:6]
        yield data[6:] + b'{"code": 20000000}\n'


class _Client:
    """Capture both streamed TTS and regular ASR provider requests."""

    captured: dict[str, Any] = {}

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        pass

    def stream(self, method: str, url: str, **kwargs: Any) -> _Stream:
        type(self).captured = {"method": method, "url": url, **kwargs}
        return _Stream()


async def test_tts_assembles_chunks_and_returns_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway keeps provider key local and returns completed audio plus chars."""
    monkeypatch.setattr(audio.httpx, "AsyncClient", _Client)
    result = await audio.tts(_Request({"model": "doubao-tts", "text": "你好", "voice": "v", "language": "ja", "emotion": "happy", "format": "mp3"}), SimpleNamespace())
    assert base64.b64decode(result["audio_base64"]) == b"audio"
    assert result["usage"] == {"text_chars": 2, "model": "doubao-tts"}
    assert _Client.captured["headers"]["X-Api-App-Id"] == "mock-app"
    assert _Client.captured["json"]["req_params"]["audio_params"]["emotion"] == "happy"


async def test_tts_accepts_80000_chinese_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio.httpx, "AsyncClient", _Client)
    result = await audio.tts(_Request({"model": "doubao-tts", "text": "汉" * 80000}), SimpleNamespace())
    assert result["usage"]["text_chars"] == 80000


async def test_tts_rejects_body_over_its_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio.httpx, "AsyncClient", _Client)
    with pytest.raises(HTTPException) as denied:
        await audio.tts(_Request({"model": "doubao-tts", "text": "汉" * 90000}), SimpleNamespace())
    assert denied.value.status_code == 413


async def test_asr_submit_and_query_return_id_status_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider header status becomes an async task and terminal usage signal."""
    calls = []

    async def post(path: str, headers: dict[str, str], body: dict[str, Any], _timeout: float) -> Any:
        calls.append((path, headers, body))
        if path.endswith("submit"):
            return SimpleNamespace(status_code=200, headers={"x-api-status-code": "20000000"})
        return SimpleNamespace(status_code=200, headers={"x-api-status-code": "20000000"}, json=lambda: {"result": {"text": "hello"}, "audio_info": {"duration": 3000}})

    monkeypatch.setattr(audio, "_post", post)
    submitted = await audio.asr_submit(_Request({"model": "doubao-asr", "audio_url": "https://example.com/a.mp3", "request_id": "task-1", "enable_itn": False}), SimpleNamespace(token="key-a"))
    assert submitted["request_id"].startswith("task-1~")
    assert calls[0][1]["X-Api-Sequence"] == "-1"
    assert calls[0][2]["request"]["enable_itn"] is False
    with pytest.raises(HTTPException) as denied:
        await audio.asr_query(_Request({"model": "doubao-asr", "request_id": submitted["request_id"]}), SimpleNamespace(token="key-b"))
    assert denied.value.status_code == 404
    assert len(calls) == 1
    queried = await audio.asr_query(_Request({"model": "doubao-asr", "request_id": submitted["request_id"]}), SimpleNamespace(token="key-a"))
    assert queried["result"]["result"]["text"] == "hello"
    assert queried["usage"]["audio_duration_ms"] == 3000
    assert "X-Api-Sequence" not in calls[1][1]
    assert calls[1][1]["X-Api-Request-Id"] == "task-1"


async def test_asr_entitlement_error_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The known account entitlement failure cannot look like processing."""
    async def denied(*_args: Any) -> Any:
        return SimpleNamespace(status_code=200, headers={"x-api-status-code": "45000030"})
    monkeypatch.setattr(audio, "_post", denied)
    signed_id = audio._sign("task-1", "key-a")
    with pytest.raises(HTTPException, match="not enabled"):
        await audio.asr_query(_Request({"model": "doubao-asr", "request_id": signed_id}), SimpleNamespace(token="key-a"))


@pytest.mark.parametrize("request_id", [123, ["task-1"], None, "bad-id"])
async def test_asr_query_invalid_ids_return_404(request_id: Any) -> None:
    with pytest.raises(HTTPException) as denied:
        await audio.asr_query(_Request({"model": "doubao-asr", "request_id": request_id}), SimpleNamespace(token="key-a"))
    assert denied.value.status_code == 404


async def test_tts_rejects_audio_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(audio, "_MAX_AUDIO_BYTES", 4)
    with pytest.raises(HTTPException) as denied:
        await audio.tts(_Request({"model": "doubao-tts", "text": "hello"}), SimpleNamespace())
    assert denied.value.status_code == 413
