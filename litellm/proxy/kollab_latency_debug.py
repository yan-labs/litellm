"""Kollab-safe latency tracing for LiteLLM Anthropic Messages requests.

This module lives in LiteLLM's proxy/runtime observability layer. It is used by
Kollab's Tokyo LiteLLM deployment to split one `/v1/messages` request into
small phases without logging prompt text, response text, credentials, raw file
payloads, or provider bodies. The fixed log prefix lets `debug-kollab` correlate
LiteLLM internals with Kollab Gateway and AgentCore runtime logs.
"""

import json
import os
import sys
import time
from typing import Any, Dict, Iterable, List, Optional

from fastapi import Request

from litellm._logging import verbose_proxy_logger

PREFIX = "KOLLAB_LITELLM_LATENCY"
TRACE_ENV = "KOLLAB_LITELLM_LATENCY_LOG"
MAX_VALUE_LENGTH = 240


def latency_now() -> float:
    """Return a monotonic timestamp for per-request phase timing."""

    return time.perf_counter()


def attach_trace_context(
    logging_obj: Any,
    *,
    data: Optional[dict] = None,
    request: Optional[Request] = None,
    route_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Attach safe correlation fields to LiteLLM's per-call logging object.

    Proxy processing has the FastAPI request and request body, while lower
    provider handlers only receive `litellm_logging_obj`. Keeping the compact
    trace context on `model_call_details` lets every layer emit the same
    request identifiers without passing raw request objects through the stack.
    """

    context = build_context(data=data, request=request, route_type=route_type)
    if logging_obj is not None:
        model_call_details = getattr(logging_obj, "model_call_details", None)
        if isinstance(model_call_details, dict):
            existing = model_call_details.get("_kollab_latency_trace")
            if isinstance(existing, dict):
                existing.update(
                    {k: v for k, v in context.items() if v not in ("", None)}
                )
                context = existing
            model_call_details["_kollab_latency_trace"] = context
    return context


def get_trace_context(
    logging_obj: Any = None, data: Optional[dict] = None
) -> Dict[str, Any]:
    """Read the previously attached trace context, falling back to request data."""

    if logging_obj is not None:
        model_call_details = getattr(logging_obj, "model_call_details", None)
        if isinstance(model_call_details, dict):
            context = model_call_details.get("_kollab_latency_trace")
            if isinstance(context, dict):
                return context
    return build_context(data=data)


def log_latency_stage(
    stage: str,
    *,
    started_at: Optional[float] = None,
    total_started_at: Optional[float] = None,
    data: Optional[dict] = None,
    request: Optional[Request] = None,
    logging_obj: Any = None,
    route_type: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Emit one structured latency stage line if tracing is enabled.

    The payload is intentionally small and structural. It is safe to search in
    production-like logs because content, response chunks, API keys, and raw
    headers are never copied into the line.
    """

    if not is_enabled():
        return

    now = latency_now()
    context = get_trace_context(logging_obj=logging_obj, data=data)
    if request is not None or route_type is not None:
        fresh_context = {
            k: v
            for k, v in build_context(
                data=data,
                request=request,
                route_type=route_type,
            ).items()
            if v not in ("", None)
        }
        if "started_at" in context:
            fresh_context.pop("started_at", None)
        context.update(fresh_context)

    payload: Dict[str, Any] = {
        "stage": stage,
        "elapsed_ms": _elapsed_ms(started_at, now),
        "total_elapsed_ms": _elapsed_ms(
            total_started_at or context.get("started_at"), now
        ),
        "route_type": route_type or context.get("route_type") or "",
        "model": _truncate(model or context.get("model") or _extract_model(data) or ""),
        "provider": _truncate(provider or context.get("provider") or ""),
        "litellm_call_id": _truncate(context.get("litellm_call_id") or ""),
        "gateway_request_id": _truncate(context.get("gateway_request_id") or ""),
        "request_id": _truncate(context.get("request_id") or ""),
        "conversation_id": _truncate(context.get("conversation_id") or ""),
        "agent_session_id": _truncate(context.get("agent_session_id") or ""),
        "space_id": _truncate(context.get("space_id") or ""),
        "env": _truncate(context.get("env") or ""),
        "model_tier": _truncate(context.get("model_tier") or ""),
        "queue_time_ms": _queue_time_ms(context.get("queue_time_seconds")),
        "shape": build_request_shape(data=data, request=request),
    }
    if details:
        payload["details"] = _sanitize_details(details)

    try:
        _emit_latency_payload(payload)
    except Exception:
        verbose_proxy_logger.debug(
            "failed to emit Kollab LiteLLM latency trace", exc_info=True
        )


def _emit_latency_payload(payload: Dict[str, Any]) -> None:
    """Write a trace line to container stderr so Docker and SSM can always see it.

    LiteLLM's proxy logger may be muted or redirected by deployment-specific
    logging configuration. The latency trace is controlled by
    `KOLLAB_LITELLM_LATENCY_LOG`, so writing directly to stderr gives on-call
    debugging a stable signal without changing business behavior.
    """

    print(
        f"{PREFIX} "
        f"{json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)}",
        file=sys.stderr,
        flush=True,
    )


def build_context(
    *,
    data: Optional[dict] = None,
    request: Optional[Request] = None,
    route_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Build correlation context from request headers, metadata, and tags."""

    headers = request.headers if request is not None else {}
    metadata = _extract_metadata(data)
    tags = _collect_tags(data=data, metadata=metadata, headers=headers)
    parsed_tags = _parse_tags(tags)

    context: Dict[str, Any] = {
        "started_at": latency_now(),
        "route_type": route_type or "",
        "model": _extract_model(data),
        "litellm_call_id": _safe_get(data, "litellm_call_id"),
        "gateway_request_id": _header_get(headers, "x-kollab-gateway-request-id"),
        "request_id": _header_get(headers, "x-kollab-request-id"),
        "conversation_id": parsed_tags.get("conversation")
        or parsed_tags.get("conversation_id"),
        "agent_session_id": parsed_tags.get("agent_session"),
        "space_id": parsed_tags.get("space") or parsed_tags.get("space_id"),
        "env": parsed_tags.get("env"),
        "model_tier": parsed_tags.get("model_tier"),
        "queue_time_seconds": metadata.get("queue_time_seconds")
        if isinstance(metadata, dict)
        else None,
    }
    if not context["request_id"]:
        context["request_id"] = parsed_tags.get("request") or parsed_tags.get(
            "request_id"
        )
    if not context["gateway_request_id"]:
        context["gateway_request_id"] = (
            metadata.get("gateway_request_id") if isinstance(metadata, dict) else ""
        )
    return context


def build_request_shape(
    data: Optional[dict], request: Optional[Request] = None
) -> Dict[str, Any]:
    """Return non-content structural request fields useful for latency diagnosis."""

    body = data if isinstance(data, dict) else {}
    metadata = _extract_metadata(body)
    headers = request.headers if request is not None else {}
    anthropic_beta = (
        _header_get(headers, "anthropic-beta")
        or _safe_get(body, "anthropic_beta")
        or _safe_get(body, "anthropic-beta")
        or ""
    )
    return {
        "stream": body.get("stream") if isinstance(body.get("stream"), bool) else None,
        "max_tokens": body.get("max_tokens")
        if isinstance(body.get("max_tokens"), int)
        else None,
        "message_count": len(body.get("messages") or [])
        if isinstance(body.get("messages"), list)
        else 0,
        "message_content_block_count": _count_message_content_blocks(
            body.get("messages")
        ),
        "system_block_count": _count_system_blocks(body.get("system")),
        "tool_count": len(body.get("tools") or []) if isinstance(body.get("tools"), list) else 0,
        "cache_control_count": _count_cache_control(body),
        "thinking_type": _extract_nested_string(body.get("thinking"), "type"),
        "thinking_budget_tokens": _extract_nested_int(
            body.get("thinking"), "budget_tokens"
        ),
        "metadata_key_count": len(metadata) if isinstance(metadata, dict) else 0,
        "anthropic_beta": _truncate(anthropic_beta),
    }


def is_enabled() -> bool:
    """Allow runtime rollback by setting KOLLAB_LITELLM_LATENCY_LOG=false."""

    return os.getenv(TRACE_ENV, "true").lower() not in {"0", "false", "off", "no"}


def _elapsed_ms(started_at: Any, now: float) -> Optional[int]:
    """Convert a monotonic start timestamp to an integer millisecond duration."""

    if not isinstance(started_at, (int, float)):
        return None
    return max(0, int((now - float(started_at)) * 1000))


def _queue_time_ms(value: Any) -> Optional[int]:
    """Convert LiteLLM queue time seconds to milliseconds when present."""

    if not isinstance(value, (int, float)):
        return None
    return max(0, int(float(value) * 1000))


def _sanitize_details(details: dict) -> dict:
    """Keep caller-supplied diagnostic details small and non-secret."""

    allowed: Dict[str, Any] = {}
    for key, value in details.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            allowed[key] = _truncate(str(value)) if isinstance(value, str) else value
        elif isinstance(value, (list, tuple, set)):
            allowed[key] = [_truncate(str(item)) for item in list(value)[:20]]
        elif isinstance(value, dict):
            allowed[key] = {
                _truncate(str(k)): _truncate(str(v))
                for k, v in list(value.items())[:20]
                if not _looks_sensitive(str(k))
            }
        else:
            allowed[key] = _truncate(type(value).__name__)
    return allowed


def _looks_sensitive(key: str) -> bool:
    """Return whether a detail key should be excluded from logs."""

    lowered = key.lower()
    return any(
        marker in lowered for marker in ("key", "token", "secret", "password", "auth")
    )


def _extract_metadata(data: Optional[dict]) -> dict:
    """Read LiteLLM metadata from either supported request-body key."""

    if not isinstance(data, dict):
        return {}
    for key in ("litellm_metadata", "metadata"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _collect_tags(*, data: Optional[dict], metadata: dict, headers: Any) -> List[str]:
    """Collect Kollab correlation tags from headers, metadata, and request data."""

    tags: List[str] = []
    for value in (
        _header_get(headers, "x-litellm-tags"),
        metadata.get("tags") if isinstance(metadata, dict) else None,
        data.get("tags") if isinstance(data, dict) else None,
    ):
        tags.extend(_normalize_tags(value))
    return tags


def _normalize_tags(value: Any) -> List[str]:
    """Normalize comma-separated or iterable tags into trimmed strings."""

    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _parse_tags(tags: List[str]) -> Dict[str, str]:
    """Parse `key:value` tags into a lookup table for trace correlation."""

    parsed: Dict[str, str] = {}
    for tag in tags:
        if ":" not in tag:
            continue
        key, value = tag.split(":", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _header_get(headers: Any, name: str) -> str:
    """Safely read one request header from FastAPI or httpx-style headers."""

    if not headers:
        return ""
    try:
        value = headers.get(name)
    except Exception:
        value = None
    return value if isinstance(value, str) else ""


def _safe_get(data: Optional[dict], key: str) -> str:
    """Read a string request-body field without coercing non-string values."""

    if not isinstance(data, dict):
        return ""
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _extract_model(data: Optional[dict]) -> str:
    """Read the model name from a request body when available."""

    return _safe_get(data, "model")


def _truncate(value: Any) -> str:
    """Bound logged values so one malformed request cannot bloat log lines."""

    text = str(value) if value is not None else ""
    return text if len(text) <= MAX_VALUE_LENGTH else f"{text[:MAX_VALUE_LENGTH]}..."


def _count_message_content_blocks(messages: Any) -> int:
    """Count message content blocks without inspecting their content."""

    if not isinstance(messages, list):
        return 0
    total = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            total += len(content)
        elif isinstance(content, str):
            total += 1
    return total


def _count_system_blocks(system: Any) -> int:
    """Count system prompt blocks without logging the prompt body."""

    if isinstance(system, list):
        return len(system)
    if isinstance(system, str) and system:
        return 1
    return 0


def _count_cache_control(value: Any, depth: int = 0) -> int:
    """Recursively count Anthropic cache-control markers with a depth bound."""

    if depth > 8 or value is None:
        return 0
    if isinstance(value, list):
        return sum(_count_cache_control(item, depth + 1) for item in value)
    if not isinstance(value, dict):
        return 0
    total = 0
    for key, item in value.items():
        total += 1 if key == "cache_control" else 0
        total += _count_cache_control(item, depth + 1)
    return total


def _extract_nested_string(value: Any, key: str) -> str:
    """Extract a nested string option from a request sub-object."""

    if isinstance(value, dict) and isinstance(value.get(key), str):
        return value[key]
    return ""


def _extract_nested_int(value: Any, key: str) -> Optional[int]:
    """Extract a nested integer option from a request sub-object."""

    if isinstance(value, dict) and isinstance(value.get(key), int):
        return value[key]
    return None
