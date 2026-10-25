"""Mock Happy Horse gateway shapes and recovery without starting the proxy."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.responses import StreamingResponse
from starlette.requests import Request

root = Path(__file__).resolve().parents[4] / "litellm"
for name, path in (
    ("litellm", root),
    ("litellm.proxy", root / "proxy"),
    ("litellm.proxy.auth", root / "proxy" / "auth"),
):
    if name not in sys.modules:
        package = ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package
if "litellm.proxy._types" not in sys.modules:
    types = ModuleType("litellm.proxy._types")
    types.UserAPIKeyAuth = object
    sys.modules["litellm.proxy._types"] = types
if "litellm.proxy.auth.auth_checks" not in sys.modules:
    checks = ModuleType("litellm.proxy.auth.auth_checks")
    checks.can_key_call_model = AsyncMock()
    checks._kollab_stub = True
    sys.modules["litellm.proxy.auth.auth_checks"] = checks
if "litellm.proxy.auth.user_api_key_auth" not in sys.modules:
    auth_module = ModuleType("litellm.proxy.auth.user_api_key_auth")
    auth_module.user_api_key_auth = AsyncMock()
    sys.modules["litellm.proxy.auth.user_api_key_auth"] = auth_module

subject = importlib.import_module("litellm.proxy.happy_horse_video_endpoints.endpoints")


def _request(body: dict) -> Request:
    """Feed a JSON body to the direct submit handler."""
    content = json.dumps(body).encode()

    async def receive() -> dict:
        """Return one request body from memory."""
        return {"type": "http.request", "body": content, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/v1/happy-horse/videos", "headers": []}, receive)


def _upstream(body: dict) -> httpx.Response:
    """Create a mocked provider response for submit and poll."""
    return httpx.Response(200, json=body, request=httpx.Request("GET", "https://example.com"))


@pytest.mark.parametrize(
    ("controls", "model", "media_key"),
    [
        ({}, "happyhorse-1.1-t2v", None),
        ({"first_frame_image_url": "https://example.com/first.png"}, "happyhorse-1.1-i2v", "img_url"),
        ({"reference_image_urls": ["https://example.com/ref.png"]}, "happyhorse-1.1-r2v", "media"),
        ({"operation": "edit", "video_urls": ["https://example.com/source.mp4"], "audio_setting": "copy"}, "happyhorse-1.0-video-edit", "media"),
    ],
)
def test_submit_shapes(controls: dict, model: str, media_key: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Map public controls to each DashScope model and media shape."""
    monkeypatch.setattr(subject.common, "validate_public_https_url", lambda value, _field: value)
    body = subject._submit_body({"model": "happy-horse", "prompt": " camera move ", "resolution": "720p", **controls})
    assert body["model"] == model
    assert body["input"]["prompt"] == "camera move"
    assert body["parameters"]["resolution"] == "720P"
    if media_key:
        assert media_key in body["input"]
    if model.endswith("edit"):
        assert body["parameters"]["audio_setting"] == "origin"


@pytest.mark.asyncio
async def test_submit_poll_and_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """A signed job keeps terminal usage and fetches bytes via the gateway."""
    monkeypatch.setenv("LITELLM_SALT_KEY", "unit-test-salt")
    monkeypatch.setattr(subject.common, "require_model_access", AsyncMock())
    monkeypatch.setattr(subject.common, "validate_public_https_url", lambda value, _field: value)
    calls: list[tuple[str, str]] = []

    async def fake_provider(method: str, path: str, *, payload=None) -> httpx.Response:
        """Return one queued submit or completed poll from memory."""
        calls.append((method, path))
        if method == "POST":
            assert payload["model"] == "happyhorse-1.1-t2v"
            return _upstream({"output": {"task_id": "provider-1", "task_status": "PENDING"}})
        return _upstream({"output": {"task_id": "provider-1", "task_status": "SUCCEEDED", "video_url": "https://example.com/final.mp4"}, "usage": {"video_tokens": 1000}})

    class FakeClient:
        closed = False
        fetched = False

        def __init__(self, *, timeout: int, follow_redirects: bool) -> None:
            """Accept the content fetch settings."""

        def build_request(self, method: str, url: str) -> httpx.Request:
            assert method == "GET" and url == "https://example.com/final.mp4"
            return httpx.Request(method, url)

        async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
            assert stream and request.url.path == "/final.mp4"
            type(self).fetched = True
            return httpx.Response(200, stream=httpx.ByteStream(b"video-bytes"), headers={"content-type": "video/mp4", "content-length": "11"})

        async def aclose(self) -> None:
            type(self).closed = True

    monkeypatch.setattr(subject, "_request", fake_provider)
    auth = SimpleNamespace(token="hashed-virtual-key")
    submitted = await subject.submit_happy_horse_video(_request({"model": "happy-horse", "prompt": "camera move"}), auth)
    task_id = json.loads(submitted.body)["output"]["task_id"]
    assert task_id.startswith("provider-1~")
    polled = await subject.get_happy_horse_video(task_id, auth)
    usage = json.loads(polled.body)["usage"]
    assert usage["output_tokens"] == 1000
    assert usage["cost_usd"] == 0.0056
    monkeypatch.setattr(subject.httpx, "AsyncClient", FakeClient)
    content = await subject.get_happy_horse_video_content(task_id, auth)
    assert isinstance(content, StreamingResponse)
    assert FakeClient.fetched and not FakeClient.closed
    assert content.headers["content-length"] == "11"
    assert b"".join([part async for part in content.body_iterator]) == b"video-bytes"
    assert FakeClient.closed
    assert calls == [("POST", "/services/aigc/video-generation/video-synthesis"), ("GET", "/tasks/provider-1"), ("GET", "/tasks/provider-1")]
