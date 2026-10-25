"""Kie task envelopes, errors and authoritative credit accounting."""

import json
from decimal import Decimal
from typing import Any

import httpx

import litellm

API_BASE = "https://api.kie.ai/api/v1"
UPLOAD_URL = "https://kieai.redpandaai.co/api/file-base64-upload"
CREDIT_USD = Decimal("0.005")


class KieException(Exception):
    """Marker shared by Kie errors and LiteLLM's public exception classes."""


class KieBadRequest(litellm.BadRequestError, KieException):
    pass


class KieContentPolicy(litellm.ContentPolicyViolationError, KieException):
    pass


class KieAuthentication(litellm.AuthenticationError, KieException):
    pass


class KieRateLimit(litellm.RateLimitError, KieException):
    pass


class KieTimeout(litellm.Timeout, KieException):
    pass


class KieAPIError(litellm.APIError, KieException):
    pass


def auth(headers: dict, api_key: str) -> dict:
    if not api_key:
        raise failure(401, "Kie credential is required")
    return {
        **headers,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def failure(code: Any, message: str) -> KieException:
    text = str(message or "Kie task failed")
    policy_text = (str(code) + " " + text).lower().replace("_", " ").replace("-", " ")
    policy = any(
        word in policy_text
        for word in (
            "content policy",
            "content violation",
            "nsfw",
            "moderation",
            "sensitive content",
        )
    )
    try:
        status = int(code)
    except (TypeError, ValueError):
        status = 500
    if (
        status not in (400, 401, 402, 403, 404, 408, 409, 422, 429)
        and not 500 <= status <= 599
    ):
        status = 500
    options = {
        "message": ("Content policy violation: " if policy else "") + text,
        "model": "",
        "llm_provider": "kie",
    }
    if policy:
        return KieContentPolicy(**options)
    if status in (400, 422):
        error = KieBadRequest(**options)
        error.status_code = status
        return error
    if status == 401:
        return KieAuthentication(**options)
    if status == 429:
        return KieRateLimit(**options)
    if status == 408:
        error = KieTimeout(**options)
        # Sync wrappers preserve this; async wrappers need the utils override too.
        error.num_retries = 0
        return error
    return KieAPIError(status_code=status, **options)


def envelope(response: httpx.Response, allow_pending: bool = False) -> dict:
    if response.status_code >= 400:
        raise failure(response.status_code, "Kie HTTP request failed")
    try:
        payload = response.json()
    except ValueError:
        raise failure(502, "Kie returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise failure(502, "Kie returned a non-object response")
    if (
        allow_pending
        and payload.get("code") in (422, "422")
        and payload.get("data") is None
    ):
        return {}
    if payload.get("code") not in (200, "200"):
        raise failure(payload.get("code"), payload.get("msg", "Kie business error"))
    return payload.get("data") or {}


def result_urls(task: dict) -> list:
    result = task.get("resultJson") or {}
    # recordInfo encodes resultJson as JSON text; tolerate a second string layer.
    try:
        for _ in range(2):
            if isinstance(result, str):
                result = json.loads(result)
    except (ValueError, TypeError):
        raise failure(502, "Kie returned invalid resultJson") from None
    if not isinstance(result, dict):
        raise failure(502, "Kie resultJson is not an object")
    urls = result.get("resultUrls") or []
    if not urls:
        raise failure(502, "Kie successful task has no result URLs")
    return urls


def usage(task: dict) -> dict:
    result = {"provider": "kie", "cost_authoritative": False, "billing_pending": True}
    if task.get("costTime") is not None:
        result["costTime"] = task["costTime"]
    credits = task.get("creditsConsumed")
    if credits is not None:
        cost = float(Decimal(str(credits)) * CREDIT_USD)
        result.update(
            kie_credits=credits,
            creditsConsumed=credits,
            cost=cost,
            cost_usd=cost,
            cost_authoritative=True,
            billing_pending=False,
        )
    return result
