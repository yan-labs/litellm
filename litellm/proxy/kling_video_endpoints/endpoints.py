"""Expose Kling V3 Pro video generation (via OpenRouter) to Kollab through LiteLLM.

This module sits in the LiteLLM proxy's custom-endpoint layer and is mounted by
``proxy_server.py``. Kollab's skills-server calls it instead of OpenRouter's
``/api/v1/videos`` so the OpenRouter key stays inside the gateway: the handler
reuses the gateway's existing ``openrouter-toyo`` LiteLLM credential (the same
one the ``seedance-2`` route uses) and never accepts a caller key.

Surface (all behind virtual-key auth + the ``kling-v3`` alias):

- ``POST /v1/kling/videos``                  -> OpenRouter ``POST /videos``
- ``GET  /v1/kling/videos/{video_id}``       -> OpenRouter ``GET /videos/{id}``
- ``GET  /v1/kling/videos/{video_id}/content`` -> OpenRouter ``GET /videos/{id}/content``

Why a custom endpoint instead of a ``kling-v3`` row for LiteLLM's native
``/v1/videos``: the native OpenRouter video adapter (built for seedance-2)
drops ``aspect_ratio``/``resolution``/``generate_audio``/``seed`` and rebuilds
the status object without OpenRouter's ``usage`` block, which Kollab needs
for billing. A native route would also let any key bypass the 15-second cap
here, so the alias is deliberately *not* a router model.

Guards: fixed upstream model ``kwaivgi/kling-v3.0-pro``; ``duration`` 4-15 s
(OpenRouter bills Kling per generated second, so the cap bounds spend);
field whitelist; public-HTTPS-only image URLs (shared SSRF guard); caller
credential fields rejected. Task ids returned to callers are HMAC-signed
(``<provider id>~<sig>``) so a ``kling-v3`` key can only poll or download
tasks it created here, never other jobs on the shared OpenRouter account.
Status JSON is relayed unchanged except that signed ``id`` (``usage``,
``unsigned_urls``, ``error`` intact); ``unsigned_urls`` point at openrouter.ai
and need the gateway key, so callers must download via the ``/content`` route.
Task ids are bound to the creating LiteLLM key. Rotating that key invalidates
outstanding ids; Kollab must finish or fail those jobs before retiring a key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from litellm.proxy import kollab_media_common as common
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

router = APIRouter()
logger = logging.getLogger(__name__)

MODEL_ALIAS = "kling-v3"
UPSTREAM_MODEL = "kwaivgi/kling-v3.0-pro"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
# Name of the LiteLLM credential holding the OpenRouter key. Defaults to the
# credential the seedance-2 route already uses in both TEST and PROD configs.
CREDENTIAL_ENV = "KLING_OPENROUTER_CREDENTIAL"
DEFAULT_CREDENTIAL = "openrouter-toyo"
# Mirrors Kollab's video policy for Kling (4-15 s, default 6). OpenRouter bills
# Kling per generated second, so the upper bound is also the spend bound.
MIN_SECONDS, MAX_SECONDS, DEFAULT_SECONDS = 4, 15, 6
_PROMPT_MAX = 2500
# Same ceiling as Kollab's shared media validation (IMAGE_MAX_COUNT).
_MAX_REFERENCES = 9
_ALLOWED_FIELDS = {
    "model",
    "prompt",
    "duration",
    "aspect_ratio",
    "resolution",
    "size",
    "generate_audio",
    "seed",
    "watermark",
    "first_frame_image_url",
    "last_frame_image_url",
    "reference_image_urls",
}
_ASPECT_RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
_RESOLUTION = re.compile(r"(480p|540p|720p|1080p|[1-4]K)")
_SIZE = re.compile(r"[1-9]\d{2,3}x[1-9]\d{2,3}")
# Public ids are ``<provider id>~<signature>``. The provider part is opaque and
# excludes ".." so it can never walk the upstream path; the signature proves
# the task was created through this endpoint (see ``_sign``).
_PROVIDER_ID = r"(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}"
_PUBLIC_ID = re.compile(rf"({_PROVIDER_ID})~([A-Za-z0-9_-]{{22}})")
# Secrets the gateway already has; the salt key never rotates (it encrypts DB
# credentials), so ids stay valid across master-key rotations.
_SIGNING_ENVS = ("LITELLM_SALT_KEY", "LITELLM_MASTER_KEY")
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_CONTENT_TIMEOUT = httpx.Timeout(300.0, connect=15.0)


def _video_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Validate the public contract and build the OpenRouter submit body.

    Shapes mirror Kollab's direct adapter (``_submit_openrouter_video_task``):
    ``duration`` is always sent, ``generate_audio`` defaults to false (audio
    moderation can reject otherwise valid clips), frames become
    ``frame_images`` with ``frame_type`` and references become ``image_url``
    ``input_references``. Video/audio references are not exposed because Kling
    ignores them upstream while still billing the generation.
    """
    common.reject_caller_credentials(body, extra=("openrouter_api_key",))
    common.reject_unknown_fields(body, _ALLOWED_FIELDS)
    if body.get("model") != MODEL_ALIAS:
        raise common.unprocessable(f"model must be {MODEL_ALIAS}")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.strip()) > _PROMPT_MAX:
        raise common.unprocessable(f"prompt must contain 1 to {_PROMPT_MAX} characters")
    duration = body.get("duration", DEFAULT_SECONDS)
    if isinstance(duration, bool) or not isinstance(duration, int) or not (
        MIN_SECONDS <= duration <= MAX_SECONDS
    ):
        raise common.unprocessable(
            f"duration must be an integer from {MIN_SECONDS} to {MAX_SECONDS} seconds"
        )
    generate_audio = body.get("generate_audio", False)
    if not isinstance(generate_audio, bool):
        raise common.unprocessable("generate_audio must be a boolean")
    payload: dict[str, Any] = {
        "model": UPSTREAM_MODEL,
        "prompt": prompt.strip(),
        "duration": duration,
        "generate_audio": generate_audio,
    }
    for field, pattern_ok in (
        ("aspect_ratio", lambda v: v in _ASPECT_RATIOS),
        ("resolution", lambda v: bool(_RESOLUTION.fullmatch(v))),
        ("size", lambda v: bool(_SIZE.fullmatch(v))),
    ):
        value = body.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not pattern_ok(value):
            raise common.unprocessable(f"Unsupported {field}")
        payload[field] = value
    seed = body.get("seed")
    if seed is not None:
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
            raise common.unprocessable("seed must be an integer from 0 to 4294967295")
        payload["seed"] = seed
    watermark = body.get("watermark")
    if watermark is not None:
        if not isinstance(watermark, bool):
            raise common.unprocessable("watermark must be a boolean")
        payload["provider"] = {"watermark": watermark}
    frames = []
    for field, frame_type in (
        ("first_frame_image_url", "first_frame"),
        ("last_frame_image_url", "last_frame"),
    ):
        if body.get(field) is not None:
            url = common.validate_public_https_url(body[field], field)
            frames.append(
                {"type": "image_url", "image_url": {"url": url}, "frame_type": frame_type}
            )
    if frames:
        payload["frame_images"] = frames
    references = body.get("reference_image_urls")
    if references is not None:
        if not isinstance(references, list) or not 1 <= len(references) <= _MAX_REFERENCES:
            raise common.unprocessable(
                f"reference_image_urls must be a list of 1 to {_MAX_REFERENCES} URLs"
            )
        payload["input_references"] = [
            {
                "type": "image_url",
                "image_url": {
                    "url": common.validate_public_https_url(url, "reference_image_urls")
                },
            }
            for url in references
        ]
    return payload


def _openrouter_key() -> str:
    """Resolve the gateway-owned OpenRouter key from the LiteLLM credential store.

    Read per request so a credential rotated through the admin API applies
    without a restart; 503 fail closed when it is missing.
    """
    from litellm.litellm_core_utils.credential_accessor import CredentialAccessor

    name = (os.environ.get(CREDENTIAL_ENV) or DEFAULT_CREDENTIAL).strip()
    api_key = str(CredentialAccessor.get_credential_values(name).get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Kling video is not configured",
        )
    return api_key


def _headers(request_id: str) -> dict[str, str]:
    """Build upstream headers; caller headers are never forwarded."""
    return {"Authorization": f"Bearer {_openrouter_key()}", "X-Request-Id": request_id}


def _signing_key() -> bytes:
    """Return the gateway secret used to sign public video ids; 503 if absent."""
    for name in _SIGNING_ENVS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value.encode()
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Kling video is not configured",
    )


def _sign(provider_id: str, token: str) -> str:
    """Bind a provider task id to the submitting virtual key's hashed token.

    The gateway's OpenRouter account also runs other jobs (seedance-2 via the
    native video route). The HMAC binds both the model and authenticated key,
    since possession of a task id must not grant another key access to a video.
    """
    digest = hmac.new(_signing_key(), f"{MODEL_ALIAS}:{token}:{provider_id}".encode(), hashlib.sha256)
    return f"{provider_id}~{base64.urlsafe_b64encode(digest.digest()).decode()[:22]}"


def _verified_provider_id(public_id: str, token: str) -> str:
    """Return the provider id only for the key that submitted the task."""
    match = _PUBLIC_ID.fullmatch(public_id)
    if not match or not hmac.compare_digest(_sign(match.group(1), token), public_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    return match.group(1)


def _auth_token(auth: UserAPIKeyAuth) -> str:
    """Read LiteLLM's hashed key identifier; reject missing identity before spend."""
    token = getattr(auth, "token", None)
    if not isinstance(token, str) or not token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Media key is not allowed")
    return token


def _relay_json(
    upstream: httpx.Response, request_id: str, token: str, provider_id: str | None = None
) -> Response:
    """Relay an OpenRouter JSON body with its status code.

    Only the task ``id`` is rewritten to the signed public id (on success, when
    it matches the task); every other field — ``usage`` for billing, ``status``,
    ``unsigned_urls``, error envelopes — is passed through unchanged.
    """
    try:
        body = json.loads(upstream.content)
    except ValueError as exc:
        logger.warning("OpenRouter video returned invalid JSON request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Kling video returned an invalid response",
        ) from exc
    content = upstream.content
    if upstream.is_success and isinstance(body, dict) and isinstance(body.get("id"), str):
        if provider_id is None or body["id"] == provider_id:
            content = json.dumps({**body, "id": _sign(body["id"], token)}).encode()
    return Response(
        content=content,
        status_code=upstream.status_code,
        media_type="application/json",
        headers={"X-Request-Id": request_id},
    )


async def _request(method: str, path: str, request_id: str, payload: Any = None) -> httpx.Response:
    """Send one bounded JSON request to OpenRouter; transport failures become 502."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            return await client.request(
                method, f"{OPENROUTER_API_BASE}{path}", headers=_headers(request_id), json=payload
            )
    except httpx.HTTPError as exc:
        logger.warning("OpenRouter video request failed request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Kling video is temporarily unavailable",
        ) from exc


async def _kling_status(provider_id: str, request_id: str) -> httpx.Response:
    """Fetch one verified Kling task; a status naming another model is also hidden.

    The model check is defense in depth on top of the signed id: it only fires
    when OpenRouter names the model, which is not guaranteed.
    """
    upstream = await _request("GET", f"/videos/{provider_id}", request_id)
    if upstream.status_code == 200:
        try:
            model = json.loads(upstream.content).get("model")
        except (ValueError, AttributeError):
            model = None
        if isinstance(model, str) and model and not model.startswith("kwaivgi/kling"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    return upstream


@router.post("/v1/kling/videos", tags=["video"])
async def submit_kling_video(
    request: Request,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),  # noqa: B008
) -> Response:
    """Submit one Kling V3 Pro task; returns OpenRouter's body with a signed ``id``."""
    await common.require_model_access(MODEL_ALIAS, user_api_key_dict)
    token = _auth_token(user_api_key_dict)
    payload = _video_payload(await common.read_json_object(request))
    request_id = common.request_id(request)
    _signing_key()  # fail closed before a paid submit whose id could not be signed
    return _relay_json(await _request("POST", "/videos", request_id, payload), request_id, token)


@router.get("/v1/kling/videos/{video_id}", tags=["video"])
async def get_kling_video(
    video_id: str,
    request: Request,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),  # noqa: B008
) -> Response:
    """Poll one task; status, ``usage`` and ``unsigned_urls`` are relayed unchanged."""
    await common.require_model_access(MODEL_ALIAS, user_api_key_dict)
    token = _auth_token(user_api_key_dict)
    request_id = common.request_id(request)
    provider_id = _verified_provider_id(video_id, token)
    upstream = await _kling_status(provider_id, request_id)
    return _relay_json(upstream, request_id, token, provider_id)


@router.get("/v1/kling/videos/{video_id}/content", tags=["video"])
async def get_kling_video_content(
    video_id: str,
    request: Request,
    index: int = Query(0, ge=0, le=9),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),  # noqa: B008
) -> Response:
    """Stream the finished MP4 through the gateway's authenticated content fetch.

    The signed id is verified and the task status fetched first (one extra
    call) so only finished Kling tasks created here are streamed. Upstream errors are relayed as JSON
    when possible; redirects are followed by httpx, which drops the
    Authorization header on cross-origin hops.
    """
    await common.require_model_access(MODEL_ALIAS, user_api_key_dict)
    token = _auth_token(user_api_key_dict)
    request_id = common.request_id(request)
    provider_id = _verified_provider_id(video_id, token)
    status_response = await _kling_status(provider_id, request_id)
    if status_response.status_code != 200:
        return _relay_json(status_response, request_id, token, provider_id)
    headers = _headers(request_id)  # resolve the key before opening a client
    client = httpx.AsyncClient(timeout=_CONTENT_TIMEOUT, follow_redirects=True)
    try:
        upstream = await client.send(
            client.build_request(
                "GET",
                f"{OPENROUTER_API_BASE}/videos/{provider_id}/content",
                params={"index": index},
                headers=headers,
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("OpenRouter video content failed request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Kling video is temporarily unavailable",
        ) from exc
    if upstream.status_code != 200:
        try:
            await upstream.aread()
        finally:
            await upstream.aclose()
            await client.aclose()
        return _relay_json(upstream, request_id, token, provider_id)

    async def _body():
        # try/finally (not a background task) so the upstream connection is
        # released even when the caller disconnects mid-stream.
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    # Raw bytes are relayed, so length and encoding travel together unchanged.
    response_headers = {"X-Request-Id": request_id}
    for name in ("content-length", "content-encoding"):
        if upstream.headers.get(name):
            response_headers[name] = upstream.headers[name]
    return StreamingResponse(
        _body(),
        status_code=200,
        media_type=upstream.headers.get("content-type", "video/mp4"),
        headers=response_headers,
    )
