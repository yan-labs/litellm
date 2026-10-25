"""Validate OpenRouter audio models against its public capability catalog.

The Tokyo gateway uses this module before forwarding OpenAI-compatible speech
requests. It keeps provider credentials and endpoints server-owned while still
allowing newly published OpenRouter audio model IDs without a code release.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Literal


AudioModality = Literal["speech", "transcription"]
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
_CACHE_TTL_SECONDS = 300
_MISS_REFRESH_THROTTLE_SECONDS = 2
_NEGATIVE_CACHE_TTL_SECONDS = 10
_catalog_cache: dict[AudioModality, tuple[float, frozenset[str]]] = {}
_negative_cache: dict[tuple[AudioModality, str], float] = {}
_last_miss_refresh: dict[AudioModality, float] = {}
_catalog_lock = threading.Lock()
_FORBIDDEN_CALLER_FIELDS = frozenset(
    {"api_key", "api_base", "base_url", "custom_llm_provider", "provider"}
)


class OpenRouterAudioModelError(ValueError):
    """The caller selected an invalid or capability-incompatible audio model."""


class OpenRouterAudioCatalogError(RuntimeError):
    """The public OpenRouter model catalog could not be verified safely."""


def is_openrouter_api_base(api_base: str | None) -> bool:
    """Return true only for the fixed managed OpenRouter API root."""
    return bool(api_base and api_base.rstrip("/") == OPENROUTER_API_BASE)


def reject_caller_routing_fields(data: dict) -> None:
    """Reject caller-controlled credentials or routes for managed audio aliases."""
    forbidden = sorted(_FORBIDDEN_CALLER_FIELDS.intersection(data))
    if forbidden:
        raise ValueError(
            f"Managed OpenRouter audio routes do not accept {', '.join(forbidden)}"
        )


def resolve_managed_audio_alias(
    model: object,
) -> tuple[str, AudioModality] | None:
    """Resolve a managed public alias to its exact OpenRouter model and modality."""
    if not isinstance(model, str):
        return None
    if model == "grok-stt":
        return "x-ai/grok-stt-1.0", "transcription"
    if model.startswith("openrouter-tts/"):
        return model.removeprefix("openrouter-tts/"), "speech"
    if model.startswith("openrouter-stt/"):
        return model.removeprefix("openrouter-stt/"), "transcription"
    return None


def is_managed_audio_alias(model: object) -> bool:
    """Identify only the TEST aliases whose provider route is gateway-owned."""
    return resolve_managed_audio_alias(model) is not None


def _fetch_catalog(modality: AudioModality) -> frozenset[str]:
    """Fetch exact model IDs whose declared output modality matches the route."""
    query = urllib.parse.urlencode({"output_modalities": modality})
    request = urllib.request.Request(
        f"{OPENROUTER_API_BASE}/models?{query}",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except Exception as exc:
        raise OpenRouterAudioCatalogError(
            "OpenRouter audio catalog is temporarily unavailable"
        ) from exc
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise OpenRouterAudioCatalogError(
            "OpenRouter audio catalog returned an invalid response"
        )
    models = {
        row["id"]
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and modality
        in ((row.get("architecture") or {}).get("output_modalities") or [])
    }
    if not models:
        raise OpenRouterAudioCatalogError(
            "OpenRouter audio catalog returned no matching models"
        )
    return frozenset(models)


def _get_catalog(modality: AudioModality) -> frozenset[str]:
    """Return a short-lived catalog; callers refresh their first miss without a startup delay."""
    now = time.monotonic()
    cached = _catalog_cache.get(modality)
    if cached and cached[0] > now:
        return cached[1]
    with _catalog_lock:
        cached = _catalog_cache.get(modality)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        models = _fetch_catalog(modality)
        _catalog_cache[modality] = (time.monotonic() + _CACHE_TTL_SECONDS, models)
        return models


def validate_audio_model(model: str, modality: AudioModality) -> None:
    """Fail closed unless the exact model currently declares the required modality."""
    if not model or "://" in model or any(ord(char) < 32 for char in model):
        raise OpenRouterAudioModelError("Invalid OpenRouter audio model ID")
    negative_key = (modality, model)
    if _negative_cache.get(negative_key, 0) > time.monotonic():
        raise OpenRouterAudioModelError(
            f"OpenRouter model {model!r} does not support {modality} output"
        )
    models = _get_catalog(modality)
    if model not in models:
        with _catalog_lock:
            now = time.monotonic()
            last_miss_refresh = _last_miss_refresh.get(modality)
            # A missing stamp means this is the first miss for the modality. It must refresh even
            # when the process has been alive for less than the throttle window; otherwise a
            # newly published model is falsely rejected during process startup and cached as bad.
            if (
                last_miss_refresh is None
                or now - last_miss_refresh >= _MISS_REFRESH_THROTTLE_SECONDS
            ):
                models = _fetch_catalog(modality)
                _catalog_cache[modality] = (now + _CACHE_TTL_SECONDS, models)
                _last_miss_refresh[modality] = now
            else:
                models = _catalog_cache.get(modality, (0, frozenset()))[1]
    if model not in models:
        _negative_cache[negative_key] = time.monotonic() + _NEGATIVE_CACHE_TTL_SECONDS
        raise OpenRouterAudioModelError(
            f"OpenRouter model {model!r} does not support {modality} output"
        )


async def validate_audio_model_async(model: str, modality: AudioModality) -> None:
    """Run catalog validation without blocking LiteLLM's request event loop."""
    await asyncio.to_thread(validate_audio_model, model, modality)


async def validate_managed_audio_request(data: dict) -> None:
    """Validate one managed proxy request before routing or invoking hooks."""
    resolved = resolve_managed_audio_alias(data.get("model"))
    if resolved is None:
        return
    try:
        reject_caller_routing_fields(data)
    except ValueError as exc:
        raise OpenRouterAudioModelError(str(exc)) from exc
    await validate_audio_model_async(*resolved)


def resolve_audio_media_type(
    upstream_content_type: str | None, response_format: object
) -> str:
    """Prefer the upstream MIME type and safely map known OpenRouter formats."""
    if upstream_content_type:
        return upstream_content_type
    return {
        "mp3": "audio/mpeg",
        "pcm": "audio/pcm",
        "wav": "audio/wav",
    }.get(response_format, "application/octet-stream")
