"""Verify the MiniMax Music gateway contract, credential ownership, and pass-through."""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

# Keep these focused adapter tests independent from the proxy's database/JWT
# extras; deployment imports the real modules from the complete proxy image.
# The stubs are installed once per session so several Kollab media endpoint
# test files can share them (the shared guard module binds them on import).
source_root = Path(__file__).resolve().parents[4] / "litellm"
if not getattr(sys.modules.get("litellm.proxy.auth.auth_checks"), "_kollab_stub", False):
    for name, path in (
        ("litellm", source_root),
        ("litellm.proxy", source_root / "proxy"),
        ("litellm.proxy.auth", source_root / "proxy" / "auth"),
    ):
        package = ModuleType(name)
        package.__path__ = [str(path)]  # type: ignore[attr-defined]
        sys.modules[name] = package
    fake_types = ModuleType("litellm.proxy._types")
    fake_types.UserAPIKeyAuth = object  # type: ignore[attr-defined]
    sys.modules["litellm.proxy._types"] = fake_types
    stub_checks = ModuleType("litellm.proxy.auth.auth_checks")
    stub_checks.can_key_call_model = AsyncMock()  # type: ignore[attr-defined]
    stub_checks._kollab_stub = True  # type: ignore[attr-defined]
    sys.modules["litellm.proxy.auth.auth_checks"] = stub_checks
    fake_user_auth = ModuleType("litellm.proxy.auth.user_api_key_auth")
    fake_user_auth.user_api_key_auth = AsyncMock()  # type: ignore[attr-defined]
    sys.modules["litellm.proxy.auth.user_api_key_auth"] = fake_user_auth
fake_auth_checks = sys.modules["litellm.proxy.auth.auth_checks"]

MODULE = "litellm.proxy.minimax_music_endpoints.endpoints"
subject = importlib.import_module(MODULE)
common = importlib.import_module("litellm.proxy.kollab_media_common")

BASE = {"model": "minimax-music", "prompt": " upbeat pop ", "lyrics": " [verse] la la "}
EXPECTED = {
    "model": "music-3.0",
    "prompt": "upbeat pop",
    "lyrics": "[verse] la la",
    "stream": False,
    "output_format": "hex",
    "audio_setting": {"sample_rate": 44100, "bitrate": 256000, "format": "mp3"},
}


def _request(body: bytes, request_id: str | None = "req-1") -> Request:
    """Build a minimal JSON Starlette request for direct handler calls."""
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [(b"content-type", b"application/json"), (b"authorization", b"Bearer vk")]
    if request_id is not None:
        headers.append((b"x-request-id", request_id.encode()))
    return Request(
        {"type": "http", "method": "POST", "path": "/v1/music/generations", "headers": headers},
        receive,
    )


def _client(upstream: httpx.Response | Exception) -> tuple[AsyncMock, AsyncMock]:
    """Stub httpx.AsyncClient so no test can reach MiniMax."""
    post = AsyncMock(side_effect=upstream) if isinstance(upstream, Exception) else AsyncMock(return_value=upstream)
    client = AsyncMock()
    client.__aenter__.return_value.post = post
    return client, post


def test_payload_matches_direct_adapter_defaults() -> None:
    """Defaults mirror Kollab's direct request: music-3.0, non-stream, hex, 44.1k/256k mp3."""
    assert subject._music_payload(dict(BASE)) == EXPECTED


def test_optional_flags_and_audio_setting_are_forwarded() -> None:
    """Only true flags are sent; audio_setting overrides merge onto defaults."""
    payload = subject._music_payload(
        {
            **BASE,
            "is_instrumental": True,
            "lyrics_optimizer": False,
            "output_format": "url",
            "audio_setting": {"format": "wav", "sample_rate": 32000},
        }
    )
    assert payload["is_instrumental"] is True
    assert "lyrics_optimizer" not in payload
    assert payload["output_format"] == "url"
    assert payload["audio_setting"] == {"sample_rate": 32000, "bitrate": 256000, "format": "wav"}


@pytest.mark.parametrize(
    "override",
    [
        {"model": "music-3.0"},
        {"model": "minimax-music-2"},
        {"lyrics": "   "},
        {"lyrics": None},
        {"prompt": ""},
        {"prompt": "x" * 2001},
        {"lyrics": "x" * 3501},
        {"stream": True},
        {"output_format": "base64"},
        {"is_instrumental": "yes"},
        {"audio_setting": {"bitrate": 999}},
        {"audio_setting": {"channel": 2}},
        {"audio_setting": {"sample_rate": True}},
        {"audio_setting": "mp3"},
        {"voice_id": "abc"},
    ],
)
def test_payload_rejects_values_outside_contract(override: dict) -> None:
    """Anything that could change model, price, or delivery shape fails with 422."""
    with pytest.raises(HTTPException) as error:
        subject._music_payload({**BASE, **override})
    assert error.value.status_code == 422


@pytest.mark.parametrize("field", ["api_key", "Authorization", "minimax_api_key", "group_id", "token"])
def test_payload_rejects_caller_credentials(field: str) -> None:
    """Callers can never bring their own MiniMax key or account through the body."""
    with pytest.raises(HTTPException) as error:
        subject._music_payload({**BASE, field: "caller"})
    assert error.value.status_code == 422
    assert "credentials" in error.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("env", [{}, {"MINIMAX_API_KEY": "  "}])
async def test_missing_server_key_fails_closed(env: dict) -> None:
    """No gateway key means 503 before any upstream call."""
    client, post = _client(httpx.Response(200, json={}))
    with patch.dict(os.environ, env, clear=True), patch(
        f"{MODULE}.httpx.AsyncClient", return_value=client
    ), pytest.raises(HTTPException) as error:
        await subject._call_minimax(EXPECTED, "req-1")
    assert error.value.status_code == 503
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_call_uses_server_key_and_relays_body_bytes() -> None:
    """The gateway key is the only credential sent and billing fields survive byte-for-byte."""
    upstream_body = json.dumps(
        {
            "data": {"audio": "4944330400", "status": 2},
            "extra_info": {"music_duration": 12345, "music_size": 5},
            "trace_id": "t-1",
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
    ).encode()
    client, post = _client(
        httpx.Response(200, content=upstream_body, headers={"Trace-Id": "t-1"})
    )
    with patch.dict(os.environ, {"MINIMAX_API_KEY": "server-secret"}), patch(
        f"{MODULE}.httpx.AsyncClient", return_value=client
    ):
        response = await subject._call_minimax(EXPECTED, "req-1")
    assert response.status_code == 200
    assert response.body == upstream_body
    assert response.headers["X-Request-Id"] == "req-1"
    assert response.headers["X-MiniMax-Trace-Id"] == "t-1"
    args = post.await_args
    assert args.args == ("https://api.minimax.io/v1/music_generation",)
    assert args.kwargs["headers"] == {
        "Authorization": "Bearer server-secret",
        "Content-Type": "application/json",
    }
    assert args.kwargs["json"] == EXPECTED


@pytest.mark.asyncio
async def test_business_errors_pass_through_with_upstream_status() -> None:
    """MiniMax reports failures as HTTP 200 + base_resp; the gateway must not hide it."""
    body = json.dumps({"base_resp": {"status_code": 2013, "status_msg": "invalid params"}}).encode()
    client, _ = _client(httpx.Response(200, content=body))
    with patch.dict(os.environ, {"MINIMAX_API_KEY": "server-secret"}), patch(
        f"{MODULE}.httpx.AsyncClient", return_value=client
    ):
        response = await subject._call_minimax(EXPECTED, "req-1")
    assert response.status_code == 200 and response.body == body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "upstream", [httpx.ReadTimeout("slow"), httpx.Response(502, text="<html>bad</html>")]
)
async def test_transport_and_non_json_failures_become_502(upstream) -> None:
    """Gateway-side failures are rewritten without leaking the server key."""
    client, _ = _client(upstream)
    with patch.dict(os.environ, {"MINIMAX_API_KEY": "server-secret"}), patch(
        f"{MODULE}.httpx.AsyncClient", return_value=client
    ), pytest.raises(HTTPException) as error:
        await subject._call_minimax(EXPECTED, "req-1")
    assert error.value.status_code == 502
    assert "server-secret" not in str(error.value.detail)


@pytest.mark.asyncio
async def test_handler_checks_alias_before_upstream() -> None:
    """A key without minimax-music never triggers a paid generation."""
    denied = AsyncMock(side_effect=HTTPException(status_code=401, detail="denied"))
    upstream = AsyncMock()
    with patch.object(common, "require_model_access", denied), patch(
        f"{MODULE}._call_minimax", upstream
    ), pytest.raises(HTTPException) as error:
        await subject.generate_music(_request(json.dumps(BASE).encode()), object())  # type: ignore[arg-type]
    assert error.value.status_code == 401
    denied.assert_awaited_once()
    assert denied.await_args.args[0] == "minimax-music"
    upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_forwards_validated_payload_and_request_id() -> None:
    """The handler sends only the narrowed body and the caller trace id."""
    upstream = AsyncMock(return_value="relayed")
    with patch.object(common, "require_model_access", AsyncMock()), patch(
        f"{MODULE}._call_minimax", upstream
    ):
        result = await subject.generate_music(
            _request(json.dumps(BASE).encode(), "trace_9"), object()  # type: ignore[arg-type]
        )
    assert result == "relayed"
    upstream.assert_awaited_once_with(EXPECTED, "trace_9")


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "code"), [(b"not json", 400), (b"[1]", 422)])
async def test_handler_rejects_non_object_bodies(body: bytes, code: int) -> None:
    """Malformed bodies fail before any provider work."""
    upstream = AsyncMock()
    with patch.object(common, "require_model_access", AsyncMock()), patch(
        f"{MODULE}._call_minimax", upstream
    ), pytest.raises(HTTPException) as error:
        await subject.generate_music(_request(body), object())  # type: ignore[arg-type]
    assert error.value.status_code == code
    upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_rejects_oversized_body_before_paid_call() -> None:
    """A caller cannot make the gateway buffer an unbounded JSON payload."""
    upstream = AsyncMock()
    with patch.object(common, "require_model_access", AsyncMock()), patch(
        f"{MODULE}._call_minimax", upstream
    ), pytest.raises(HTTPException) as error:
        await subject.generate_music(_request(b"{" + b" " * common.MAX_JSON_BYTES + b"}"), object())  # type: ignore[arg-type]
    assert error.value.status_code == 413
    upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_oversized_chunk_is_rejected_before_copy() -> None:
    """The JSON guard checks an ASGI chunk's size before allocating a copy."""

    class OversizedChunk:
        def __len__(self) -> int:
            return common.MAX_JSON_BYTES + 1

        def __iter__(self):
            raise AssertionError("oversized chunk must not be copied")

    with pytest.raises(HTTPException) as error:
        await common.read_json_object(_request(OversizedChunk()))  # type: ignore[arg-type]
    assert error.value.status_code == 413


def test_music_route_is_registered_for_virtual_keys() -> None:
    """Unlisted custom routes are proxy-admin-only, which would 401 every virtual key."""
    import ast

    tree = ast.parse((source_root / "proxy" / "_types.py").read_text())
    routes: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LiteLLMRoutes":
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and getattr(stmt.targets[0], "id", None) == "openai_routes":
                    routes = [elt.value for elt in stmt.value.elts]  # type: ignore[attr-defined]
    assert "/v1/music/generations" in routes
