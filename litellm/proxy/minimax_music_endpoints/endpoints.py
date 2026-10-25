"""Expose MiniMax Music generation to Kollab through LiteLLM.

This module sits in the LiteLLM proxy's custom-endpoint layer and is mounted by
``proxy_server.py``. Kollab's skills-server (and any virtual-key holder granted
the managed ``minimax-music`` alias) calls it instead of MiniMax directly, so the
MiniMax credential only lives in the gateway process.

Surface:

- ``POST /v1/music/generations`` -> MiniMax ``POST /v1/music_generation``
  (synchronous: one call returns the finished song).

Contract choices mirror Kollab's direct adapter (``minimax_music.py``):
``model`` is pinned to ``music-3.0``, ``stream`` is always false, ``lyrics`` is
required (MiniMax answers HTTP 200 + ``base_resp.status_code=2013`` without it),
and ``audio_setting`` defaults to 44.1 kHz / 256 kbps MP3. Only those fields are
accepted so a caller cannot switch models or options that change price or
delivery shape. MiniMax bills per song (<= 5 minutes), so there is no per-second
field to cap. The upstream JSON (``base_resp``, ``data.audio``, ``extra_info``,
``trace_id``) is returned byte-for-byte; callers must still treat
``base_resp.status_code != 0`` as failure even when HTTP is 200, exactly as the
direct adapter does. There are no URL inputs, so no SSRF surface.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from litellm.proxy import kollab_media_common as common
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

router = APIRouter()
logger = logging.getLogger(__name__)

MODEL_ALIAS = "minimax-music"
UPSTREAM_MODEL = "music-3.0"
MINIMAX_API_BASE = "https://api.minimax.io"
# A 5-minute song at 256 kbps comes back as ~20 MB of hex; MiniMax takes
# minutes to render it, so the upstream read budget is generous.
_UPSTREAM_TIMEOUT = httpx.Timeout(360.0, connect=15.0)
_PROMPT_MAX = 2000
_LYRICS_MAX = 3500
_ALLOWED_FIELDS = {
    "model",
    "prompt",
    "lyrics",
    "is_instrumental",
    "lyrics_optimizer",
    "output_format",
    "audio_setting",
}
_OUTPUT_FORMATS = {"hex", "url"}
_AUDIO_SETTING_VALUES = {
    "sample_rate": {16000, 24000, 32000, 44100},
    "bitrate": {32000, 64000, 128000, 256000},
    "format": {"mp3", "wav", "pcm"},
}
_DEFAULT_AUDIO_SETTING = {"sample_rate": 44100, "bitrate": 256000, "format": "mp3"}


def _bounded_text(body: dict[str, Any], field: str, limit: int) -> str:
    """Return a stripped non-empty string field or raise 422."""
    value = body.get(field)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise common.unprocessable(f"{field} must contain 1 to {limit} characters")
    return value.strip()


def _audio_setting(value: Any) -> dict[str, Any]:
    """Validate the optional audio_setting object against MiniMax's documented enums."""
    if value is None:
        return dict(_DEFAULT_AUDIO_SETTING)
    if not isinstance(value, dict):
        raise common.unprocessable("audio_setting must be an object")
    common.reject_unknown_fields(value, _AUDIO_SETTING_VALUES)
    merged = {**_DEFAULT_AUDIO_SETTING, **value}
    for key, allowed in _AUDIO_SETTING_VALUES.items():
        item = merged[key]
        if isinstance(item, bool) or item not in allowed:
            raise common.unprocessable(
                f"audio_setting.{key} must be one of {sorted(map(str, allowed))}"
            )
    return merged


def _music_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Validate the public contract and build the MiniMax request body.

    Called by ``generate_music`` before any upstream work, so an invalid or
    credential-carrying request never costs a paid generation.
    """
    common.reject_caller_credentials(body, extra=("minimax_api_key", "group_id"))
    common.reject_unknown_fields(body, _ALLOWED_FIELDS)
    if body.get("model") != MODEL_ALIAS:
        raise common.unprocessable(f"model must be {MODEL_ALIAS}")
    payload: dict[str, Any] = {
        "model": UPSTREAM_MODEL,
        "prompt": _bounded_text(body, "prompt", _PROMPT_MAX),
        "lyrics": _bounded_text(body, "lyrics", _LYRICS_MAX),
        "stream": False,
        "output_format": body.get("output_format", "hex"),
        "audio_setting": _audio_setting(body.get("audio_setting")),
    }
    if payload["output_format"] not in _OUTPUT_FORMATS:
        raise common.unprocessable("output_format must be hex or url")
    for flag in ("is_instrumental", "lyrics_optimizer"):
        value = body.get(flag)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise common.unprocessable(f"{flag} must be a boolean")
        if value:
            # Only true flags are sent, keeping the default body identical to
            # MiniMax's minimal documented request (same as the direct adapter).
            payload[flag] = True
    return payload


def _minimax_key() -> str:
    """Read the gateway-owned MiniMax key per request; 503 fail closed if absent.

    Known risk: ``MINIMAX_API_KEY`` is also the env var LiteLLM's native
    ``minimax/*`` provider falls back to. No ``minimax/*`` route exists today;
    adding one without an explicit ``api_key`` would silently reuse this key
    outside the whitelist above, so give such a route its own credential.
    """
    api_key = (os.environ.get("MINIMAX_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MiniMax music is not configured",
        )
    return api_key


async def _call_minimax(payload: dict[str, Any], request_id: str) -> Response:
    """POST one generation and relay MiniMax's JSON body byte-for-byte.

    The body is parsed once only to prove it is JSON (a proxy error page must not
    reach callers as audio); the original bytes are returned so a ~20 MB hex
    payload is not re-serialized. Caller headers are never forwarded.
    """
    headers = {
        "Authorization": f"Bearer {_minimax_key()}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT) as client:
            upstream = await client.post(
                f"{MINIMAX_API_BASE}/v1/music_generation", headers=headers, json=payload
            )
    except httpx.HTTPError as exc:
        logger.warning("MiniMax music request failed request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="MiniMax music is temporarily unavailable",
        ) from exc
    try:
        json.loads(upstream.content)
    except ValueError as exc:
        logger.warning("MiniMax music returned invalid JSON request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="MiniMax music returned an invalid response",
        ) from exc
    response_headers = {"X-Request-Id": request_id}
    trace_id = upstream.headers.get("Trace-Id")
    if trace_id:
        response_headers["X-MiniMax-Trace-Id"] = trace_id
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type="application/json",
        headers=response_headers,
    )


@router.post("/v1/music/generations", tags=["music"])
async def generate_music(
    request: Request,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),  # noqa: B008
) -> Response:
    """Generate one song through the managed ``minimax-music`` alias."""
    await common.require_model_access(MODEL_ALIAS, user_api_key_dict)
    body = await common.read_json_object(request)
    payload = _music_payload(body)
    return await _call_minimax(payload, common.request_id(request))
