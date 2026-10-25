"""Verify the Tripo V3 contract, input narrowing, tracing, and server-owned credentials."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException, Response
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

MODULE = "litellm.proxy.tripo_3d_endpoints.endpoints"
subject = importlib.import_module(MODULE)
common = importlib.import_module("litellm.proxy.kollab_media_common")
_call_tripo = subject._call_tripo
_require_model_access = subject._require_model_access
_submission = subject._submission
get_3d_task = subject.get_3d_task
submit_3d_task = subject.submit_3d_task
upload_3d_image = subject.upload_3d_image

TEXT = {"model": "tripo-3d", "type": "text_to_model", "prompt": " small cat "}
V3_DEFAULTS = {
    "model": "v3.0-20250812",
    "texture": True,
    "pbr": True,
    "texture_quality": "standard",
    "geometry_quality": "standard",
}


def _request(
    body: bytes = b"{}",
    request_id: str = "request-1",
    content_type: bytes = b"application/json",
) -> Request:
    """Build the smallest Starlette request needed by direct endpoint checks."""
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
            "path": "/v1/3d/tasks",
            "headers": [
                (b"x-request-id", request_id.encode()),
                (b"content-type", content_type),
                (b"authorization", b"Bearer caller-virtual-key"),
            ],
        },
        receive,
    )


def _multipart(fields: dict[str, tuple[str | None, bytes]]) -> Request:
    """Encode a multipart/form-data request; a None filename makes a text field."""
    boundary = "tripo-test-boundary"
    parts = []
    for name, (filename, data) in fields.items():
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        parts.append(
            f"--{boundary}\r\nContent-Disposition: {disposition}\r\n\r\n".encode()
            + data
            + b"\r\n"
        )
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    return _request(body, content_type=f"multipart/form-data; boundary={boundary}".encode())


def _fake_client(upstream: httpx.Response) -> tuple[AsyncMock, AsyncMock]:
    """Return (AsyncClient factory result, request mock) for upstream stubbing."""
    request = AsyncMock(return_value=upstream)
    client = AsyncMock()
    client.__aenter__.return_value.request = request
    return client, request


def test_text_submission_builds_v3_body_with_stable_defaults() -> None:
    """Omitted controls map to the pinned V3 build and Kollab's standard tier."""
    path, payload = _submission(dict(TEXT))
    assert path == "/generation/text-to-model"
    assert payload == {**V3_DEFAULTS, "prompt": "small cat"}


def test_hd_controls_and_dated_build_override_are_forwarded() -> None:
    """Kollab's hd tier and explicit V3 builds must reach Tripo unchanged."""
    _, payload = _submission(
        {
            **TEXT,
            "model_version": "v3.1-20260101",
            "texture_quality": "detailed",
            "geometry_quality": "detailed",
            "texture": False,
            "pbr": False,
        }
    )
    assert payload["model"] == "v3.1-20260101"
    assert payload["texture_quality"] == payload["geometry_quality"] == "detailed"
    assert payload["texture"] is False and payload["pbr"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_version", "v2.5-20250123"),
        ("model_version", "v3."),
        ("model_version", "latest"),
        ("texture_quality", "ultra"),
        ("geometry_quality", "hd"),
        ("texture", "yes"),
        ("pbr", 1),
    ],
)
def test_submission_rejects_values_outside_the_priced_contract(field: str, value: object) -> None:
    """Unpriced tiers, V2 builds, or loose types must fail before any upstream call."""
    with pytest.raises(HTTPException) as error:
        _submission({**TEXT, field: value})
    assert error.value.status_code == 422


def test_image_submission_uses_unified_v3_input() -> None:
    """Both file tokens and public URLs travel in V3's single ``input`` field."""
    base = {"model": "tripo-3d", "type": "image_to_model"}
    path, payload = _submission({**base, "file_token": "tok_abc-123"})
    assert path == "/generation/image-to-model"
    assert payload == {**V3_DEFAULTS, "input": "tok_abc-123"}
    _, payload = _submission({**base, "image_url": "https://assets.example/cat.jpg"})
    assert payload["input"] == "https://assets.example/cat.jpg"
    # Extensionless CDN URLs are accepted; V3 infers the type from content.
    _, payload = _submission({**base, "image_url": "https://cdn.example/photo-1?w=800"})
    assert payload["input"] == "https://cdn.example/photo-1?w=800"


@pytest.mark.parametrize(
    "body",
    [
        {"type": "image_to_model"},
        {"type": "image_to_model", "file_token": "t1", "image_url": "https://a.example/c.png"},
        {"type": "image_to_model", "file_token": "https://evil.example/x.png"},
        {"type": "image_to_model", "file_token": "../etc/passwd"},
        {"type": "image_to_model", "file_token": "t1", "prompt": "cat"},
        {"type": "text_to_model", "prompt": "cat", "file_token": "t1"},
        {"type": "text_to_model", "prompt": "   "},
        {"type": "text_to_model", "prompt": "x" * 1025},
        {"type": "text_to_model", "prompt": "cat", "negative_prompt": "dog"},
        {"type": "text_to_model", "prompt": "cat", "quad": True},
        {"type": "text_to_model", "prompt": "cat", "face_limit": 1000},
        {"type": "refine_model", "prompt": "cat"},
    ],
)
def test_submission_rejects_malformed_or_unmanaged_requests(body: dict) -> None:
    """The adapter must not become an arbitrary authenticated pass-through."""
    with pytest.raises(HTTPException) as error:
        _submission({"model": "tripo-3d", **body})
    assert error.value.status_code == 422


@pytest.mark.parametrize("field", ["api_key", "tripo_api_key", "Authorization", "token"])
def test_submission_rejects_caller_supplied_credentials(field: str) -> None:
    """Callers can never bring their own provider key through the body."""
    with pytest.raises(HTTPException) as error:
        _submission({**TEXT, field: "caller-controlled"})
    assert error.value.status_code == 422
    assert "credentials" in error.value.detail


def test_submission_requires_managed_alias() -> None:
    """Only the fixed alias is accepted so key allowlists stay meaningful."""
    with pytest.raises(HTTPException):
        _submission({**TEXT, "model": "tripo-v3"})


@pytest.mark.parametrize(
    "image_url",
    [
        "https://127.0.0.1/cat.png",
        "https://169.254.169.254/cat.png",
        "https://[::1]/cat.png",
        "https://localhost/cat.png",
        "https://localhost./cat.png",
        "https://foo.localhost./cat.png",
        "https://assets.internal/cat.png",
        "https://127.1/cat.png",
        "https://2130706433/cat.png",
        "https://0x7f000001/cat.png",
        "https://user:password@assets.example/cat.png",
        "https://[bad/cat.png",
        "http://assets.example/cat.png",
        "https://[::127.0.0.1]/cat.png",
        "https://[64:ff9b::a9fe:a9fe]/cat.png",
        "https://[::ffff:10.0.0.1]/cat.png",
        "https://[2002:7f00:1::]/cat.png",
    ],
)
def test_submission_rejects_unsafe_or_non_image_urls(image_url: str) -> None:
    """Provider-side fetching must not receive local targets or non-image paths."""
    with pytest.raises(HTTPException):
        _submission({"model": "tripo-3d", "type": "image_to_model", "image_url": image_url})


@pytest.mark.asyncio
async def test_call_tripo_uses_server_key_v3_base_and_preserves_request_id() -> None:
    """Only the process key reaches Tripo V3 and the caller trace survives the hop."""
    upstream = httpx.Response(
        200,
        json={"code": 0, "data": {"task_id": "task-1"}},
        headers={"X-Tripo-Trace-ID": "trace-1"},
    )
    client, request = _fake_client(upstream)
    with patch.dict(os.environ, {"TRIPO_API_KEY": "server-secret"}, clear=False), patch(
        f"{MODULE}.httpx.AsyncClient", return_value=client
    ):
        status_code, body, headers = await _call_tripo(
            "POST", "/generation/text-to-model", "request-1", json_body={"prompt": "cat"}
        )

    assert status_code == 200
    assert body["data"]["task_id"] == "task-1"
    assert headers == {"X-Request-Id": "request-1", "X-Tripo-Trace-ID": "trace-1"}
    args = request.await_args
    assert args.args == (
        "POST",
        "https://openapi.tripo3d.ai/v3/generation/text-to-model",
    )
    assert args.kwargs["headers"] == {
        "Authorization": "Bearer server-secret",
        "X-Request-Id": "request-1",
    }
    assert args.kwargs["json"] == {"prompt": "cat"}


@pytest.mark.asyncio
@pytest.mark.parametrize("env", [{}, {"TRIPO_API_KEY": ""}, {"TRIPO_API_KEY": "   "}])
async def test_call_tripo_fails_closed_without_server_key(env: dict) -> None:
    """A missing LiteLLM-owned key must never fall back to caller credentials."""
    client, request = _fake_client(httpx.Response(200, json={}))
    with patch.dict(os.environ, env, clear=True), patch(
        f"{MODULE}.httpx.AsyncClient", return_value=client
    ), pytest.raises(HTTPException) as error:
        await _call_tripo("GET", "/tasks/task-1", "request-1")
    assert error.value.status_code == 503
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_call_tripo_passes_provider_errors_through_verbatim() -> None:
    """Provider status/code/suggestion must survive so callers can classify failures."""
    envelope = {"code": 2010, "message": "insufficient credit", "suggestion": "top up"}
    client, _ = _fake_client(httpx.Response(403, json=envelope))
    with patch.dict(os.environ, {"TRIPO_API_KEY": "server-secret"}), patch(
        f"{MODULE}.httpx.AsyncClient", return_value=client
    ):
        status_code, body, _ = await _call_tripo("POST", "/generation/text-to-model", "r")
    assert status_code == 403
    assert body == envelope


@pytest.mark.asyncio
async def test_call_tripo_maps_transport_and_non_json_failures_to_502() -> None:
    """Only gateway-side failures are rewritten, and never with provider secrets."""
    broken = AsyncMock()
    broken.__aenter__.return_value.request = AsyncMock(side_effect=httpx.ConnectError("x"))
    html, _ = _fake_client(httpx.Response(502, text="<html>bad gateway</html>"))
    for client in (broken, html):
        with patch.dict(os.environ, {"TRIPO_API_KEY": "server-secret"}), patch(
            f"{MODULE}.httpx.AsyncClient", return_value=client
        ), pytest.raises(HTTPException) as error:
            await _call_tripo("GET", "/tasks/t", "r")
        assert error.value.status_code == 502
        assert "server-secret" not in str(error.value.detail)


@pytest.mark.asyncio
async def test_get_tripo_bypasses_stale_signed_url_caches() -> None:
    """Task refreshes ask every intermediary for current artifact URLs."""
    client, request = _fake_client(httpx.Response(200, json={"code": 0, "data": {}}))
    with patch.dict(os.environ, {"TRIPO_API_KEY": "server-secret"}), patch(
        f"{MODULE}.httpx.AsyncClient", return_value=client
    ):
        await _call_tripo("GET", "/tasks/task-1", "request-1")
    assert request.await_args.kwargs["headers"]["Cache-Control"] == "no-cache"
    assert request.await_args.kwargs["headers"]["Pragma"] == "no-cache"


@pytest.mark.asyncio
@pytest.mark.parametrize("key_alias", ["kollab-test-server-ai-models-20260529141754", "skills-server-test", "skills-server-alpha"])
async def test_require_model_access_checks_fixed_alias(key_alias: str) -> None:
    """Managed product keys still need LiteLLM's model permission check."""
    fake_proxy_server = ModuleType("litellm.proxy.proxy_server")
    fake_proxy_server.llm_router = object()  # type: ignore[attr-defined]
    token = SimpleNamespace(key_alias=key_alias)
    can_key_call_model = fake_auth_checks.can_key_call_model
    can_key_call_model.reset_mock()  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"litellm.proxy.proxy_server": fake_proxy_server}):
        await _require_model_access(token)  # type: ignore[arg-type]
    can_key_call_model.assert_awaited_once_with(  # type: ignore[attr-defined]
        model="tripo-3d",
        llm_model_list=None,
        valid_token=token,
        llm_router=fake_proxy_server.llm_router,  # type: ignore[attr-defined]
    )


@pytest.mark.asyncio
async def test_custom_media_rejects_non_server_key_before_model_check() -> None:
    """A broad virtual-key grant cannot bypass Kollab's job billing ledger."""
    can_key_call_model = fake_auth_checks.can_key_call_model
    can_key_call_model.reset_mock()  # type: ignore[attr-defined]
    with pytest.raises(HTTPException) as error:
        await _require_model_access(SimpleNamespace(key_alias="external-key"))  # type: ignore[arg-type]
    assert error.value.status_code == 403
    can_key_call_model.assert_not_awaited()  # type: ignore[attr-defined]


def test_managed_media_aliases_are_exact_product_keys() -> None:
    """A similarly named customer key must not enter unmetered media routes."""
    assert common.MANAGED_KEY_ALIASES == {
        "kollab-test-server-ai-models-20260529141754",
        "kollab-server-ai-models-20260529",
        "skills-server-test",
        "skills-server-alpha",
        "skills-server-prod",
    }


@pytest.mark.asyncio
async def test_submit_routes_validated_payload_to_v3_endpoint() -> None:
    """POST /v1/3d/tasks forwards only the narrowed body and keeps upstream status."""
    upstream = AsyncMock(return_value=(200, {"code": 0, "data": {"task_id": "t"}}, {}))
    response = Response()
    with patch(f"{MODULE}._require_model_access", AsyncMock()), patch(
        f"{MODULE}._call_tripo", upstream
    ):
        result = await submit_3d_task(
            _request(b'{"model":"tripo-3d","type":"text_to_model","prompt":"cat"}'),
            response,
            object(),  # type: ignore[arg-type]
        )
    assert result == {"code": 0, "data": {"task_id": "t"}}
    upstream.assert_awaited_once_with(
        "POST",
        "/generation/text-to-model",
        "request-1",
        json_body={**V3_DEFAULTS, "prompt": "cat"},
    )


@pytest.mark.asyncio
async def test_submit_rejection_never_reaches_tripo() -> None:
    """A denied virtual key cannot trigger provider work through POST."""
    denied = AsyncMock(side_effect=HTTPException(status_code=401, detail="denied"))
    upstream = AsyncMock()
    with patch(f"{MODULE}._require_model_access", denied), patch(
        f"{MODULE}._call_tripo", upstream
    ), pytest.raises(HTTPException) as error:
        await submit_3d_task(_request(), Response(), object())  # type: ignore[arg-type]
    assert error.value.status_code == 401
    upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_returns_terminal_billing_fields_verbatim() -> None:
    """credits_consumed and model_url must reach Kollab exactly as Tripo sent them."""
    envelope = {
        "code": 0,
        "data": {
            "task_id": "task-1",
            "status": "success",
            "progress": 100,
            "credits_consumed": 20.5,
            "output": {"model_url": "https://cdn.example/m.glb"},
        },
    }
    allowed = AsyncMock()
    upstream = AsyncMock(return_value=(200, envelope, {}))
    with patch(f"{MODULE}._require_model_access", allowed), patch(
        f"{MODULE}._call_tripo", upstream
    ):
        result = await get_3d_task(
            "task-1", _request(), Response(), object()  # type: ignore[arg-type]
        )
    allowed.assert_awaited_once()
    upstream.assert_awaited_once_with("GET", "/tasks/task-1", "request-1")
    assert result == envelope


@pytest.mark.asyncio
async def test_get_rejects_path_like_task_ids() -> None:
    """Task ids are interpolated into the upstream path, so they stay opaque tokens."""
    upstream = AsyncMock()
    with patch(f"{MODULE}._require_model_access", AsyncMock()), patch(
        f"{MODULE}._call_tripo", upstream
    ), pytest.raises(HTTPException):
        await get_3d_task("../files", _request(), Response(), object())  # type: ignore[arg-type]
    upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_forwards_png_as_multipart_file() -> None:
    """Local image bytes become a V3 /files upload returning a file_token."""
    upstream = AsyncMock(return_value=(200, {"code": 0, "data": {"file_token": "tok"}}, {}))
    with patch(f"{MODULE}._require_model_access", AsyncMock()), patch(
        f"{MODULE}._call_tripo", upstream
    ):
        result = await upload_3d_image(
            _multipart({"file": ("cat.PNG", b"\x89PNG-bytes")}),
            Response(),
            object(),  # type: ignore[arg-type]
        )
    assert result["data"]["file_token"] == "tok"
    upstream.assert_awaited_once_with(
        "POST",
        "/files",
        "request-1",
        files={"file": ("image.png", b"\x89PNG-bytes", "image/png")},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"file": ("cat.gif", b"GIF89a")}, 422),
        ({"file": ("cat.png", b"")}, 422),
        ({"image": ("cat.png", b"x")}, 422),
        ({"file": (None, b"not-a-file")}, 422),
        ({"file": ("cat.png", b"x"), "api_key": (None, b"caller")}, 422),
    ],
)
async def test_upload_rejects_invalid_files_before_tripo(fields: dict, code: int) -> None:
    """Unsupported, empty, or credential-carrying uploads never leave the gateway."""
    upstream = AsyncMock()
    with patch(f"{MODULE}._require_model_access", AsyncMock()), patch(
        f"{MODULE}._call_tripo", upstream
    ), pytest.raises(HTTPException) as error:
        await upload_3d_image(_multipart(fields), Response(), object())  # type: ignore[arg-type]
    assert error.value.status_code == code
    upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_rejects_images_over_20mb() -> None:
    """The documented V3 size limit is enforced locally with 413."""
    upstream = AsyncMock()
    big = b"x" * (20 * 1024 * 1024 + 1)
    with patch(f"{MODULE}._require_model_access", AsyncMock()), patch(
        f"{MODULE}._call_tripo", upstream
    ), pytest.raises(HTTPException) as error:
        await upload_3d_image(
            _multipart({"file": ("big.jpg", big)}), Response(), object()  # type: ignore[arg-type]
        )
    assert error.value.status_code == 413
    upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_permission_denied_never_parses_or_forwards() -> None:
    """A key without the alias cannot push bytes to Tripo."""
    denied = AsyncMock(side_effect=HTTPException(status_code=401, detail="denied"))
    upstream = AsyncMock()
    with patch(f"{MODULE}._require_model_access", denied), patch(
        f"{MODULE}._call_tripo", upstream
    ), pytest.raises(HTTPException) as error:
        await upload_3d_image(
            _multipart({"file": ("cat.png", b"x")}), Response(), object()  # type: ignore[arg-type]
        )
    assert error.value.status_code == 401
    upstream.assert_not_awaited()


def test_3d_routes_are_registered_as_llm_api_routes() -> None:
    """Unlisted custom routes are proxy-admin-only, which would 401 every virtual key.

    Parsed with ``ast`` so this stays runnable without the proxy's heavy extras.
    """
    import ast

    tree = ast.parse((source_root / "proxy" / "_types.py").read_text())
    routes: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LiteLLMRoutes":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and getattr(stmt.targets[0], "id", None) == "openai_routes"
                ):
                    routes = [elt.value for elt in stmt.value.elts]  # type: ignore[attr-defined]
    assert {"/v1/3d/tasks", "/v1/3d/files", "/v1/3d/tasks/{task_id}"} <= set(routes)
