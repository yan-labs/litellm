"""Trusted provider refusal classification shared by HTTP mapping and Router.

Only structured upstream protocol fields are read, never user text or fuzzy
error messages. Genuine non-2xx usage is retained without inventing a bill.
"""
import json
from typing import Any


def refusal_body(error: Exception) -> dict:
    """Read SDK body, HTTP JSON or a provider exception's complete JSON envelope."""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        return body
    response = getattr(error, "response", None)
    try:
        body = response.json() if response is not None else None
        if isinstance(body, dict):
            return body
    except (ValueError, RuntimeError):
        pass
    # BaseLLMException keeps the upstream HTTP body as message; parse JSON only.
    try:
        body = json.loads(getattr(error, "message", ""))
        return body if isinstance(body, dict) else {}
    except (ValueError, TypeError):
        return {}


def upstream_status(error: Exception) -> int:
    """Read both SDK and httpx status locations without inspecting response prose."""
    return getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", 400)


def refusal_code(error: Exception) -> str | None:
    """Classify exact upstream error fields; generic 403 remains an auth failure."""
    body = refusal_body(error)
    detail = body.get("error", body)
    if not isinstance(detail, dict):
        return None
    codes = (detail.get("code"), detail.get("type"))
    if "upstream_model_refusal" in codes:
        return "upstream_model_refusal"
    if upstream_status(error) == 403 and "model_denylisted" in codes:
        return "model_denylisted"
    return None


def is_refusal_response(response: Any) -> bool:
    """Prevent content-policy fallback for successful protocol refusal responses."""
    if isinstance(response, dict):
        return response.get("stop_reason") == "refusal"
    if getattr(response, "_hidden_params", {}).get("upstream_stop_reason") == "refusal":
        return True
    for choice in getattr(response, "choices", []):
        if getattr(choice, "finish_reason", None) == "refusal":
            return True
        message = getattr(choice, "message", None) or getattr(choice, "delta", None)
        if getattr(message, "refusal", None):
            return True
    return False


def mapped_refusal(error: Exception, model: str, provider: str):
    """Normalize trusted terminal errors before generic mappings discard their body.

    Preserve original HTTP status and real token counts in provider_specific_fields;
    no usage is synthesized. The proxy's existing exception serializer carries it.
    """
    code = refusal_code(error)
    if code and (getattr(error, "provider_specific_fields", None) or {}).get("retryable") is False:
        return error
    if not code:
        return None
    from litellm.exceptions import BadRequestError

    body = refusal_body(error)
    result = BadRequestError(message=code, model=model, llm_provider=provider,
                             body={"error": {"code": code, "type": code}})
    result.status_code = upstream_status(error)
    result.code = code
    result.type = code
    fields = {"retryable": False}
    usage = body.get("usage")
    if not isinstance(usage, dict) and isinstance(body.get("error"), dict):
        usage = body["error"].get("usage")
    if isinstance(usage, dict):
        real = {k: v for k, v in usage.items() if k in {
            "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
            "total_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"
        } and type(v) is int and v >= 0}
        if real:
            fields["usage"] = real
    result.provider_specific_fields = fields
    return result
