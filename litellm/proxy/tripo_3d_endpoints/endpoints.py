"""Expose the small Tripo 3D V3 surface used by Kollab through LiteLLM.

This module sits in the LiteLLM proxy's custom-endpoint layer and is mounted by
``proxy_server.py``. Kollab's skills-server, using the managed server key,
calls it instead of reaching Tripo
directly, so the Tripo credential only ever lives in the gateway process.

Surface (all behind LiteLLM virtual-key auth + the ``tripo-3d`` model allowlist):

- ``POST /v1/3d/tasks``            -> Tripo V3 ``/generation/text-to-model`` or
                                      ``/generation/image-to-model``
- ``POST /v1/3d/files``            -> Tripo V3 ``/files`` (JPEG/PNG, <= 20 MB)
- ``GET  /v1/3d/tasks/{task_id}``  -> Tripo V3 ``/tasks/{task_id}``

Why a narrow whitelist instead of a generic pass-through: Kollab bills 3D jobs
from the terminal ``data.credits_consumed`` and prices standard/hd tiers from
``texture_quality`` / ``geometry_quality`` / ``model``. Letting callers send
arbitrary provider fields (``quad``, ``face_limit`` ...) or V2 fields would let
them change output format or cost outside that contract, so unknown fields are
rejected before any upstream call. Upstream JSON envelopes (``code``, ``data``,
``message``, ``suggestion``) are returned verbatim so billing fields such as
``credits_consumed`` reach the caller unchanged.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from litellm.proxy import kollab_media_common as common
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

router = APIRouter()
logger = logging.getLogger(__name__)

MODEL_ALIAS = "tripo-3d"
TRIPO_API_BASE = "https://openapi.tripo3d.ai/v3"
# Official stable V3 build. Pinning it (instead of "latest") keeps output
# quality and per-task credit cost stable for callers that omit model_version.
DEFAULT_MODEL_VERSION = "v3.0-20250812"
_MODEL_VERSION = re.compile(r"v3\.\d+-\d{8}")
_QUALITIES = {"standard", "detailed"}
_TASK_TYPES = {"text_to_model", "image_to_model"}
_ALLOWED_FIELDS = {
    "model",
    "type",
    "prompt",
    "file_token",
    "image_url",
    "model_version",
    "texture_quality",
    "geometry_quality",
    "texture",
    "pbr",
}
_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# Tripo file tokens are opaque; restrict to a conservative charset so the value
# can never be a URL or path that V3's unified ``input`` would dereference.
_FILE_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_UPLOAD_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_PROMPT_MAX = 1024


_unprocessable = common.unprocessable


async def _require_model_access(user_api_key_dict: UserAPIKeyAuth) -> None:
    """Require the managed server key and its ``tripo-3d`` grant.

    Every route calls this before touching Tripo. Kollab owns job billing, so
    keys outside its server account cannot create or read 3D tasks here.
    """
    await common.require_model_access(MODEL_ALIAS, user_api_key_dict)


def _validate_image_url(image_url: Any) -> str:
    """Accept only public HTTPS URLs (shared SSRF guard) for image-to-3D input.

    The path suffix is deliberately not checked: V3 infers the image type and
    Kollab forwards public image URLs that often lack an extension (CDN query
    URLs); Tripo rejects non-image content itself.
    """
    return common.validate_public_https_url(image_url, "image_url")


def _submission(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Validate the public contract and build ``(v3_path, v3_body)``.

    Mirrors Kollab's direct V3 adapter: ``model`` is always sent (V3 requires
    it), texture/pbr default on, quality controls default to ``standard`` and
    only ``standard``/``detailed`` are accepted because those are the two tiers
    Kollab prices. ``quad`` is intentionally not exposed: it forces FBX while
    the Kollab delivery contract is GLB. Prompts over 1024 characters are
    rejected rather than silently truncated so callers see exactly what was
    generated; Kollab's direct adapter truncates, so callers migrating to the
    gateway must truncate before sending.
    """
    common.reject_caller_credentials(body, extra=("tripo_api_key",))
    common.reject_unknown_fields(body, _ALLOWED_FIELDS)
    if body.get("model") != MODEL_ALIAS:
        raise _unprocessable(f"model must be {MODEL_ALIAS}")
    task_type = body.get("type")
    if task_type not in _TASK_TYPES:
        raise _unprocessable("type must be text_to_model or image_to_model")

    model_version = body.get("model_version", DEFAULT_MODEL_VERSION)
    if not isinstance(model_version, str) or not _MODEL_VERSION.fullmatch(
        model_version.strip()
    ):
        raise _unprocessable("model_version must be a dated V3 build like v3.0-20250812")
    payload: dict[str, Any] = {"model": model_version.strip()}
    for flag in ("texture", "pbr"):
        value = body.get(flag, True)
        if not isinstance(value, bool):
            raise _unprocessable(f"{flag} must be a boolean")
        payload[flag] = value
    for field in ("texture_quality", "geometry_quality"):
        value = body.get(field, "standard")
        if value not in _QUALITIES:
            raise _unprocessable(f"{field} must be standard or detailed")
        payload[field] = value

    if task_type == "text_to_model":
        if body.get("file_token") is not None or body.get("image_url") is not None:
            raise _unprocessable("file_token/image_url are only valid for image_to_model")
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > _PROMPT_MAX:
            raise _unprocessable(f"prompt must contain 1 to {_PROMPT_MAX} characters")
        payload["prompt"] = prompt.strip()
        return "/generation/text-to-model", payload

    if body.get("prompt") is not None:
        raise _unprocessable("prompt is only valid for text_to_model")
    file_token = body.get("file_token")
    image_url = body.get("image_url")
    if (file_token is None) == (image_url is None):
        raise _unprocessable("image_to_model requires exactly one of file_token or image_url")
    if file_token is not None:
        if not isinstance(file_token, str) or not _FILE_TOKEN.fullmatch(file_token):
            raise _unprocessable("Invalid file_token")
        payload["input"] = file_token
    else:
        payload["input"] = _validate_image_url(image_url)
    return "/generation/image-to-model", payload


_request_id = common.request_id


def _tripo_key() -> str:
    """Read the gateway-owned Tripo key at call time and fail closed if absent.

    Read per request (not at import) so a missing secret only disables the 3D
    routes with 503 instead of breaking proxy startup.
    """
    api_key = (os.environ.get("TRIPO_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tripo 3D is not configured",
        )
    return api_key


async def _call_tripo(
    method: str,
    path: str,
    request_id: str,
    *,
    json_body: dict[str, Any] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> tuple[int, Any, dict[str, str]]:
    """Call Tripo V3 with the server-side key and return its envelope verbatim.

    Caller headers are never forwarded, so a caller cannot inject its own
    ``Authorization``. Status code and JSON body are passed through unchanged
    so provider error ``code``/``message``/``suggestion`` and terminal billing
    fields survive; only transport failures and non-JSON bodies become 502.
    """
    headers = {"Authorization": f"Bearer {_tripo_key()}", "X-Request-Id": request_id}
    if method == "GET":
        # Tripo result URLs expire quickly; status refreshes must bypass any
        # intermediary cache so Kollab can recover a fresh signed artifact URL.
        headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache"})
    try:
        # 3D traffic is low-volume; a per-request client keeps lifecycle simple.
        async with httpx.AsyncClient(timeout=60) as client:
            upstream = await client.request(
                method,
                f"{TRIPO_API_BASE}{path}",
                headers=headers,
                json=json_body,
                files=files,
            )
    except httpx.HTTPError as exc:
        logger.warning("Tripo request failed request_id=%s path=%s", request_id, path)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Tripo 3D is temporarily unavailable",
        ) from exc
    try:
        response_body: Any = upstream.json()
    except ValueError as exc:
        logger.warning("Tripo returned invalid JSON request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Tripo 3D returned an invalid response",
        ) from exc
    response_headers = {"X-Request-Id": request_id}
    trace_id = upstream.headers.get("X-Tripo-Trace-ID")
    if trace_id:
        response_headers["X-Tripo-Trace-ID"] = trace_id
    return upstream.status_code, response_body, response_headers


@router.post("/v1/3d/tasks", tags=["3d"])
async def submit_3d_task(
    request: Request,
    response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),  # noqa: B008
) -> Any:
    """Submit one text-to-3D or image-to-3D V3 task through the managed alias."""
    await _require_model_access(user_api_key_dict)
    body = await common.read_json_object(request)
    path, payload = _submission(body)
    request_id = _request_id(request)
    status_code, result, headers = await _call_tripo(
        "POST", path, request_id, json_body=payload
    )
    response.status_code = status_code
    response.headers.update(headers)
    return result


@router.post("/v1/3d/files", tags=["3d"])
async def upload_3d_image(
    request: Request,
    response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),  # noqa: B008
) -> Any:
    """Upload one JPEG/PNG (multipart field ``file``) and return Tripo's file_token.

    Used by image-to-3D callers holding local bytes instead of a public URL.
    Type and 20 MB size are enforced here so invalid input never leaves the
    gateway; the returned ``data.file_token`` feeds ``POST /v1/3d/tasks``.
    """
    await _require_model_access(user_api_key_dict)
    try:
        form = await request.form()
    except Exception as exc:  # starlette raises assorted parser errors
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be multipart/form-data",
        ) from exc
    upload = form.get("file")
    if upload is None or isinstance(upload, str) or not hasattr(upload, "read"):
        raise _unprocessable("multipart field 'file' is required")
    extra = set(form.keys()) - {"file"}
    if extra:
        raise _unprocessable(f"Unsupported fields: {', '.join(sorted(extra))}")
    suffix = (upload.filename or "").rsplit(".", 1)[-1].lower()
    mime = _UPLOAD_TYPES.get(suffix)
    if mime is None:
        raise _unprocessable("file must be a JPG or PNG image")
    # LiteLLM's auth dependency parses the form first; rewind defensively in
    # case any middleware consumed part of the spooled file.
    await upload.seek(0)
    data = await upload.read(_MAX_UPLOAD_BYTES + 1)
    if not data:
        raise _unprocessable("file must not be empty")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="file exceeds the 20 MB limit",
        )
    request_id = _request_id(request)
    status_code, result, headers = await _call_tripo(
        "POST", "/files", request_id, files={"file": (f"image.{suffix}", data, mime)}
    )
    response.status_code = status_code
    response.headers.update(headers)
    return result


@router.get("/v1/3d/tasks/{task_id}", tags=["3d"])
async def get_3d_task(
    task_id: str,
    request: Request,
    response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),  # noqa: B008
) -> Any:
    """Poll a Tripo V3 task; the upstream envelope (incl. credits_consumed) is verbatim."""
    await _require_model_access(user_api_key_dict)
    if not _TASK_ID.fullmatch(task_id):
        raise _unprocessable("Invalid task_id")
    request_id = _request_id(request)
    status_code, result, headers = await _call_tripo("GET", f"/tasks/{task_id}", request_id)
    response.status_code = status_code
    response.headers.update(headers)
    return result
