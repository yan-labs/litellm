"""Verify the Kling V3 gateway contract, duration cap, SSRF guard, and relays."""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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

MODULE = "litellm.proxy.kling_video_endpoints.endpoints"
subject = importlib.import_module(MODULE)
common = importlib.import_module("litellm.proxy.kollab_media_common")

BASE = {"model": "kling-v3", "prompt": " a red kite "}
SIGNING_ENV = {"LITELLM_SALT_KEY": "salt-for-tests"}
TOKEN = "hashed-server-key-a"


def _auth() -> SimpleNamespace:
    """Represent the authenticated managed key supplied by LiteLLM."""
    return SimpleNamespace(key_alias="kollab-test-server-ai-models-20260529141754", token=TOKEN)


def _request(body: bytes = b"{}", request_id: str = "req-1") -> Request:
    """Build a minimal Starlette request for direct handler calls."""
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/kling/videos",
            "headers": [
                (b"content-type", b"application/json"),
                (b"authorization", b"Bearer caller-virtual-key"),
                (b"x-request-id", request_id.encode()),
            ],
        },
        receive,
    )


def _credentials(values: dict) -> dict:
    """Install a fake LiteLLM credential accessor returning ``values``."""
    fake = ModuleType("litellm.litellm_core_utils.credential_accessor")
    accessor = MagicMock()
    accessor.get_credential_values.return_value = values
    fake.CredentialAccessor = accessor  # type: ignore[attr-defined]
    return {"litellm.litellm_core_utils.credential_accessor": fake}


def test_payload_defaults_mirror_direct_adapter() -> None:
    """Fixed model, trimmed prompt, 6 s default (Kollab policy), audio off by default."""
    assert subject._video_payload(dict(BASE)) == {
        "model": "kwaivgi/kling-v3.0-pro",
        "prompt": "a red kite",
        "duration": 6,
        "generate_audio": False,
    }


def test_payload_maps_frames_references_and_provider_options() -> None:
    """Frames, references, sizing, seed and watermark use OpenRouter's shapes."""
    payload = subject._video_payload(
        {
            **BASE,
            "duration": 15,
            "aspect_ratio": "9:16",
            "resolution": "720p",
            "generate_audio": True,
            "seed": 7,
            "watermark": False,
            "first_frame_image_url": "https://cdn.example/a.png",
            "last_frame_image_url": "https://cdn.example/b",
            "reference_image_urls": ["https://cdn.example/r1.jpg?sig=x"] + ["https://cdn.example/r.png"] * 8,
        }
    )
    assert payload["duration"] == 15
    assert payload["aspect_ratio"] == "9:16" and payload["resolution"] == "720p"
    assert payload["generate_audio"] is True and payload["seed"] == 7
    assert payload["provider"] == {"watermark": False}
    assert payload["frame_images"] == [
        {"type": "image_url", "image_url": {"url": "https://cdn.example/a.png"}, "frame_type": "first_frame"},
        {"type": "image_url", "image_url": {"url": "https://cdn.example/b"}, "frame_type": "last_frame"},
    ]
    assert len(payload["input_references"]) == 9
    assert payload["input_references"][0] == {
        "type": "image_url",
        "image_url": {"url": "https://cdn.example/r1.jpg?sig=x"},
    }


@pytest.mark.parametrize(
    "override",
    [
        {"duration": 16},
        {"duration": 60},
        {"duration": 3},
        {"duration": "5"},
        {"duration": True},
        {"duration": 5.0},
        {"model": "kwaivgi/kling-v3.0-pro"},
        {"prompt": "  "},
        {"prompt": "x" * 2501},
        {"aspect_ratio": "2:1"},
        {"resolution": "8K"},
        {"size": "10x10"},
        {"seed": -1},
        {"seed": 2**32},
        {"watermark": "no"},
        {"generate_audio": "yes"},
        {"reference_image_urls": []},
        {"reference_image_urls": ["https://a.example/1.png"] * 10},
        {"reference_image_urls": "https://a.example/1.png"},
        {"video_urls": ["https://a.example/v.mp4"]},
        {"input_references": [{"type": "video_url"}]},
        {"frame_images": []},
        {"provider": {"order": ["x"]}},
    ],
)
def test_payload_rejects_values_outside_contract(override: dict) -> None:
    """Anything beyond the 15 s cap or the whitelist fails before a paid submit."""
    with pytest.raises(HTTPException) as error:
        subject._video_payload({**BASE, **override})
    assert error.value.status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example/a.png",
        "https://127.0.0.1/a.png",
        "https://169.254.169.254/latest",
        "https://[::127.0.0.1]/a.png",
        "https://[64:ff9b::a9fe:a9fe]/a.png",
        "https://metadata.internal/a.png",
        "https://user:pw@cdn.example/a.png",
        "file:///etc/passwd",
    ],
)
@pytest.mark.parametrize("field", ["first_frame_image_url", "last_frame_image_url", "reference_image_urls"])
def test_payload_rejects_unsafe_image_urls(field: str, url: str) -> None:
    """Every image input goes through the shared SSRF guard."""
    value = [url] if field == "reference_image_urls" else url
    with pytest.raises(HTTPException):
        subject._video_payload({**BASE, field: value})


@pytest.mark.parametrize("field", ["api_key", "Authorization", "openrouter_api_key", "token"])
def test_payload_rejects_caller_credentials(field: str) -> None:
    """Callers can never bring their own OpenRouter key through the body."""
    with pytest.raises(HTTPException) as error:
        subject._video_payload({**BASE, field: "caller"})
    assert "credentials" in error.value.detail


def test_key_comes_from_named_litellm_credential() -> None:
    """The gateway reuses its stored OpenRouter credential (default openrouter-toyo)."""
    modules = _credentials({"api_key": " sk-or-server "})
    with patch.dict(sys.modules, modules), patch.dict(os.environ, {}, clear=True):
        assert subject._openrouter_key() == "sk-or-server"
    accessor = modules["litellm.litellm_core_utils.credential_accessor"].CredentialAccessor
    accessor.get_credential_values.assert_called_once_with("openrouter-toyo")


@pytest.mark.asyncio
async def test_missing_credential_fails_closed_before_upstream() -> None:
    """No stored credential means 503 and no OpenRouter request."""
    client = AsyncMock()
    with patch.dict(sys.modules, _credentials({})), patch(
        f"{MODULE}.httpx.AsyncClient", return_value=client
    ), pytest.raises(HTTPException) as error:
        await subject._request("POST", "/videos", "req-1", {"x": 1})
    assert error.value.status_code == 503
    client.__aenter__.return_value.request.assert_not_called()


@pytest.mark.asyncio
async def test_request_sends_only_server_key_to_openrouter() -> None:
    """Caller headers are dropped; only the gateway credential reaches OpenRouter."""
    client = AsyncMock()
    client.__aenter__.return_value.request = AsyncMock(return_value=httpx.Response(200, json={}))
    with patch.dict(sys.modules, _credentials({"api_key": "sk-or-server"})), patch(
        f"{MODULE}.httpx.AsyncClient", return_value=client
    ):
        await subject._request("POST", "/videos", "req-1", {"model": "m"})
    args = client.__aenter__.return_value.request.await_args
    assert args.args == ("POST", "https://openrouter.ai/api/v1/videos")
    assert args.kwargs["headers"] == {"Authorization": "Bearer sk-or-server", "X-Request-Id": "req-1"}


def test_relay_signs_id_and_keeps_billing_fields() -> None:
    """Only ``id`` is replaced by the signed id; usage and status stay intact."""
    body = {"id": "v1", "status": "completed", "usage": {"cost": 0.84}, "unsigned_urls": ["u"]}
    with patch.dict(os.environ, SIGNING_ENV, clear=True):
        response = subject._relay_json(httpx.Response(200, json=body), "req-1", TOKEN, "v1")
        relayed = json.loads(response.body)
        assert relayed == {**body, "id": subject._sign("v1", TOKEN)}
        assert subject._verified_provider_id(relayed["id"], TOKEN) == "v1"
    assert response.headers["X-Request-Id"] == "req-1"
    error = json.dumps({"error": {"message": "bad"}}).encode()
    failed = subject._relay_json(httpx.Response(400, content=error), "r", TOKEN)
    assert failed.status_code == 400 and failed.body == error
    with pytest.raises(HTTPException) as failure:
        subject._relay_json(httpx.Response(502, text="<html>"), "r", TOKEN)
    assert failure.value.status_code == 502


def test_signed_ids_reject_foreign_or_forged_tasks() -> None:
    """Raw provider ids (e.g. a seedance job) and tampered signatures are 404."""
    with patch.dict(os.environ, SIGNING_ENV, clear=True):
        good = subject._sign("gen-abc123", TOKEN)
        assert subject._verified_provider_id(good, TOKEN) == "gen-abc123"
        with pytest.raises(HTTPException) as other_key:
            subject._verified_provider_id(good, "hashed-server-key-b")
        assert other_key.value.status_code == 404
        for forged in ("gen-abc123", "gen-other~" + good.split("~")[1], good[:-1] + "x", good + "a"):
            with pytest.raises(HTTPException) as error:
                subject._verified_provider_id(forged, TOKEN)
            assert error.value.status_code == 404
    with patch.dict(os.environ, {"LITELLM_SALT_KEY": "rotated"}, clear=True), pytest.raises(HTTPException):
        subject._verified_provider_id(good, TOKEN)


@pytest.mark.asyncio
async def test_status_rejects_different_key_before_upstream() -> None:
    """A leaked signed id cannot be polled by a second managed key."""
    upstream = AsyncMock()
    with patch.object(common, "require_model_access", AsyncMock()), patch(
        f"{MODULE}._kling_status", upstream
    ), patch.dict(os.environ, SIGNING_ENV, clear=True), pytest.raises(HTTPException) as error:
        await subject.get_kling_video(subject._sign("v1", TOKEN), _request(), SimpleNamespace(token="other-key"))
    assert error.value.status_code == 404
    upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_content_rejects_different_key_before_upstream() -> None:
    """A second key cannot download bytes using the creator's signed id."""
    upstream = AsyncMock()
    with patch.object(common, "require_model_access", AsyncMock()), patch(
        f"{MODULE}._kling_status", upstream
    ), patch.dict(os.environ, SIGNING_ENV, clear=True), pytest.raises(HTTPException) as error:
        await subject.get_kling_video_content(
            subject._sign("v1", TOKEN), _request(), 0, SimpleNamespace(token="other-key")
        )
    assert error.value.status_code == 404
    upstream.assert_not_awaited()


def test_signing_fails_closed_without_gateway_secret() -> None:
    """Without a signing secret the endpoints refuse (503) instead of issuing unsigned ids."""
    with patch.dict(os.environ, {}, clear=True), pytest.raises(HTTPException) as error:
        subject._sign("v1", TOKEN)
    assert error.value.status_code == 503


@pytest.mark.parametrize(
    "provider_id", ["..", "a/../b", "a/b", "", "-x", "a" * 300, "a..b"]
)
def test_path_like_provider_ids_never_verify(provider_id: str) -> None:
    """Even a correctly signed path-like id is rejected by the id grammar."""
    with patch.dict(os.environ, SIGNING_ENV, clear=True):
        digest = subject._sign("x", TOKEN).split("~")[1]
        with pytest.raises(HTTPException):
            subject._verified_provider_id(f"{provider_id}~{digest}", TOKEN)


@pytest.mark.asyncio
async def test_status_hides_jobs_of_other_models() -> None:
    """Defense in depth: a status that names another model is hidden."""
    other = httpx.Response(200, json={"id": "v", "model": "bytedance/seedance-2.0-fast"})
    with patch(f"{MODULE}._request", AsyncMock(return_value=other)), pytest.raises(
        HTTPException
    ) as error:
        await subject._kling_status("v", "req-1")
    assert error.value.status_code == 404
    mine = httpx.Response(200, json={"id": "v", "model": "kwaivgi/kling-v3.0-pro"})
    with patch(f"{MODULE}._request", AsyncMock(return_value=mine)):
        assert (await subject._kling_status("v", "req-1")) is mine


@pytest.mark.asyncio
async def test_submit_checks_alias_before_any_upstream_work() -> None:
    """A key without kling-v3 never triggers a paid submit."""
    denied = AsyncMock(side_effect=HTTPException(status_code=401, detail="denied"))
    upstream = AsyncMock()
    with patch.object(common, "require_model_access", denied), patch(
        f"{MODULE}._request", upstream
    ), pytest.raises(HTTPException):
        await subject.submit_kling_video(_request(json.dumps(BASE).encode()), _auth())
    assert denied.await_args.args[0] == "kling-v3"
    upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_posts_narrowed_payload() -> None:
    """The handler forwards only the validated OpenRouter body."""
    upstream = AsyncMock(return_value=httpx.Response(200, json={"id": "v1", "status": "pending"}))
    with patch.object(common, "require_model_access", AsyncMock()), patch(
        f"{MODULE}._request", upstream
    ), patch.dict(os.environ, SIGNING_ENV, clear=True):
        response = await subject.submit_kling_video(
            _request(json.dumps({**BASE, "duration": 4}).encode()), _auth()
        )
        assert json.loads(response.body) == {"id": subject._sign("v1", TOKEN), "status": "pending"}
    upstream.assert_awaited_once_with(
        "POST",
        "/videos",
        "req-1",
        {"model": "kwaivgi/kling-v3.0-pro", "prompt": "a red kite", "duration": 4, "generate_audio": False},
    )


def _content_client(stream: httpx.AsyncByteStream | httpx.ByteStream) -> MagicMock:
    """Fake AsyncClient whose streamed response yields ``stream``."""
    content = httpx.Response(
        200, stream=stream, headers={"content-type": "video/mp4", "content-length": "12"}
    )
    client = MagicMock()
    client.build_request = MagicMock(return_value="req")
    client.send = AsyncMock(return_value=content)
    client.aclose = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_content_streams_upstream_bytes_and_closes() -> None:
    """Finished MP4 bytes stream through and the upstream client is closed afterwards."""
    status_ok = httpx.Response(200, json={"id": "v1", "status": "completed"})
    client = _content_client(httpx.ByteStream(b"\x00\x00\x00\x18ftypmp42"))
    with patch.object(common, "require_model_access", AsyncMock()), patch(
        f"{MODULE}._kling_status", AsyncMock(return_value=status_ok)
    ), patch(f"{MODULE}.httpx.AsyncClient", return_value=client), patch.dict(
        sys.modules, _credentials({"api_key": "sk-or-server"})
    ), patch.dict(os.environ, SIGNING_ENV, clear=True):
        response = await subject.get_kling_video_content(
            subject._sign("v1", TOKEN), _request(), 0, _auth()
        )
        chunks = [chunk async for chunk in response.body_iterator]
    assert b"".join(chunks) == b"\x00\x00\x00\x18ftypmp42"
    assert response.media_type == "video/mp4"
    assert response.headers["X-Request-Id"] == "req-1"
    build = client.build_request.call_args
    assert build.args == ("GET", "https://openrouter.ai/api/v1/videos/v1/content")
    assert build.kwargs["params"] == {"index": 0}
    client.aclose.assert_awaited_once()


class _BrokenStream(httpx.AsyncByteStream):
    """Upstream stream that dies mid-body, like a reset connection."""

    async def __aiter__(self):
        yield b"partial"
        raise httpx.ReadError("reset")


@pytest.mark.asyncio
async def test_content_releases_connection_when_stream_breaks() -> None:
    """A mid-stream failure still closes the upstream client (no leaked sockets)."""
    status_ok = httpx.Response(200, json={"id": "v1", "status": "completed"})
    client = _content_client(_BrokenStream())
    with patch.object(common, "require_model_access", AsyncMock()), patch(
        f"{MODULE}._kling_status", AsyncMock(return_value=status_ok)
    ), patch(f"{MODULE}.httpx.AsyncClient", return_value=client), patch.dict(
        sys.modules, _credentials({"api_key": "sk-or-server"})
    ), patch.dict(os.environ, SIGNING_ENV, clear=True):
        response = await subject.get_kling_video_content(
            subject._sign("v1", TOKEN), _request(), 0, _auth()
        )
        with pytest.raises(httpx.ReadError):
            async for _ in response.body_iterator:
                pass
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_content_rejects_unsigned_ids_before_any_upstream_call() -> None:
    """A raw OpenRouter id (for example a seedance job) cannot be downloaded."""
    status_call = AsyncMock()
    with patch.object(common, "require_model_access", AsyncMock()), patch(
        f"{MODULE}._kling_status", status_call
    ), patch.dict(os.environ, SIGNING_ENV, clear=True), pytest.raises(HTTPException) as error:
        await subject.get_kling_video_content("v1", _request(), 0, _auth())
    assert error.value.status_code == 404
    status_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_content_relays_status_errors_without_fetching_bytes() -> None:
    """Unknown tasks never reach the content fetch."""
    missing = httpx.Response(404, json={"error": {"message": "not found"}})
    with patch.object(common, "require_model_access", AsyncMock()), patch(
        f"{MODULE}._kling_status", AsyncMock(return_value=missing)
    ), patch(f"{MODULE}.httpx.AsyncClient") as factory, patch.dict(
        os.environ, SIGNING_ENV, clear=True
    ):
        response = await subject.get_kling_video_content(
            subject._sign("v1", TOKEN), _request(), 0, _auth()
        )
    assert response.status_code == 404
    factory.assert_not_called()


def test_kling_routes_are_registered_for_virtual_keys() -> None:
    """Unlisted custom routes are proxy-admin-only, which would 401 every virtual key."""
    import ast

    tree = ast.parse((source_root / "proxy" / "_types.py").read_text())
    routes: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LiteLLMRoutes":
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and getattr(stmt.targets[0], "id", None) == "openai_routes":
                    routes = [elt.value for elt in stmt.value.elts]  # type: ignore[attr-defined]
    assert {
        "/v1/kling/videos",
        "/v1/kling/videos/{video_id}",
        "/v1/kling/videos/{video_id}/content",
    } <= set(routes)


@pytest.mark.asyncio
async def test_submit_without_signing_secret_fails_before_paid_call() -> None:
    """No signing secret means 503 before OpenRouter is asked to render anything."""
    upstream = AsyncMock()
    with patch.object(common, "require_model_access", AsyncMock()), patch(
        f"{MODULE}._request", upstream
    ), patch.dict(os.environ, {}, clear=True), pytest.raises(HTTPException) as error:
        await subject.submit_kling_video(_request(json.dumps(BASE).encode()), _auth())
    assert error.value.status_code == 503
    upstream.assert_not_awaited()
