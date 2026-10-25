"""Shared guards for Kollab's managed media gateway endpoints.

These helpers sit in the LiteLLM proxy's custom-endpoint layer and are used by
``tripo_3d_endpoints``, ``minimax_music_endpoints`` and ``kling_video_endpoints``.
Each of those endpoints keeps its provider credential inside the gateway and
exposes a narrow, whitelisted request contract behind LiteLLM virtual-key auth.
Centralising the security-relevant pieces (alias permission check, request-id
handling, caller-credential rejection, SSRF-safe public URL validation) keeps
the three endpoints from drifting apart on exactly the checks that matter.
"""

from __future__ import annotations

import json
import re
from ipaddress import IPv4Address, IPv6Address, IPv6Network, ip_address
from typing import Any, Iterable
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import HTTPException, Request, status

from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.auth_checks import can_key_call_model

REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# These custom media routes bypass LiteLLM's normal spend callback. Only the
# Kollab server and skills-server keys may call them: Kollab settles each
# finished job to its original payer using the provider's actual usage.
MANAGED_KEY_ALIASES = frozenset(
    {
        "kollab-test-server-ai-models-20260529141754",
        "kollab-server-ai-models-20260529",
        "skills-server-test",
        "skills-server-alpha",
        "skills-server-prod",
    }
)
MAX_JSON_BYTES = 64 * 1024
# Body field names that would smuggle a caller-owned provider credential. Every
# endpoint rejects them with a stable message before its own whitelist check.
CREDENTIAL_FIELDS = frozenset(
    {"api_key", "apikey", "key", "authorization", "token", "access_token", "secret"}
)
# IPv6 forms that embed an IPv4 target. ``ipaddress`` reports several of these
# as global even when the embedded IPv4 is loopback/link-local/private, so the
# embedded address is re-checked explicitly.
_IPV4_COMPAT = IPv6Network("::/96")
_NAT64 = IPv6Network("64:ff9b::/96")


def unprocessable(detail: str) -> HTTPException:
    """Build the gateway's own 422 error (upstream errors are passed through)."""
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def require_model_access(alias: str, user_api_key_dict: UserAPIKeyAuth) -> None:
    """Require a Kollab server key and its fixed media model grant.

    Called by all custom media routes before provider work. These routes do not
    enter LiteLLM's spend accounting, so third-party keys must be rejected even
    when they have a broad model allowlist. Kollab bills the original job owner.
    """
    if getattr(user_api_key_dict, "key_alias", None) not in MANAGED_KEY_ALIASES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Media key is not allowed")
    from litellm.proxy.proxy_server import llm_router

    await can_key_call_model(
        model=alias,
        llm_model_list=None,
        valid_token=user_api_key_dict,
        llm_router=llm_router,
    )


def reject_caller_credentials(fields: Iterable[Any], extra: Iterable[str] = ()) -> None:
    """Fail with 422 when the caller tries to bring its own provider key."""
    names = {str(name).lower() for name in fields}
    if names & (CREDENTIAL_FIELDS | {name.lower() for name in extra}):
        raise unprocessable(
            "Caller-supplied provider credentials are not accepted; "
            "the gateway owns the provider key"
        )


def reject_unknown_fields(body: dict[str, Any], allowed: Iterable[str]) -> None:
    """Fail with 422 on any field outside an endpoint's public whitelist."""
    unknown = set(body) - set(allowed)
    if unknown:
        raise unprocessable(f"Unsupported fields: {', '.join(sorted(map(str, unknown)))}")


def request_id(request: Request) -> str:
    """Preserve a safe caller trace id or create one when it is absent."""
    value = request.headers.get("X-Request-Id")
    if value is None:
        return str(uuid4())
    if not REQUEST_ID.fullmatch(value):
        raise unprocessable("Invalid X-Request-Id")
    return value


async def read_json_object(request: Request, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    """Read a bounded JSON body before parsing so oversized calls cannot exhaust memory."""
    body_bytes = bytearray()
    async for chunk in request.stream():
        # Reject before copying a single oversized ASGI chunk into our buffer.
        if len(body_bytes) + len(chunk) > max_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Request body too large")
        body_bytes.extend(chunk)
    try:
        body = json.loads(body_bytes)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Request body must be JSON"
        ) from exc
    if not isinstance(body, dict):
        raise unprocessable("Request body must be an object")
    return body


def is_public_ip(address: IPv4Address | IPv6Address) -> bool:
    """Return True only when the address and any embedded IPv4 are globally routable."""
    if not address.is_global:
        return False
    if isinstance(address, IPv6Address):
        embedded = [address.ipv4_mapped, address.sixtofour]
        if address.teredo:
            embedded.append(address.teredo[1])
        if address in _IPV4_COMPAT or address in _NAT64:
            embedded.append(IPv4Address(int(address) & 0xFFFFFFFF))
        return all(ip is None or ip.is_global for ip in embedded)
    return True


def validate_public_https_url(value: Any, field: str) -> str:
    """Accept only public HTTPS URLs so a provider is never pointed inward.

    Providers fetch these URLs themselves, but the value comes from an
    untrusted caller; blocking private/loopback/metadata hosts keeps the
    gateway from being used to probe internal addresses through provider
    error messages. DNS names are not resolved here (the provider resolves
    them on its own network), so only literal addresses and reserved names
    are checked.
    """
    try:
        parsed = urlparse(value) if isinstance(value, str) else None
        hostname = (parsed.hostname or "").lower().rstrip(".") if parsed else ""
    except ValueError as exc:
        raise unprocessable(f"{field} must be an absolute HTTPS URL") from exc
    if parsed is None or parsed.scheme != "https" or not parsed.netloc or len(value) > 4096:
        raise unprocessable(f"{field} must be an absolute HTTPS URL")
    if (
        parsed.username is not None
        or parsed.password is not None
        or not hostname
        or hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".internal"))
    ):
        raise unprocessable(f"{field} host is not allowed")
    try:
        if not is_public_ip(ip_address(hostname)):
            raise unprocessable(f"{field} host is not allowed")
    except ValueError:
        if all(part.isdigit() for part in hostname.split(".")) or re.fullmatch(
            r"0x[0-9a-f]+", hostname
        ):
            raise unprocessable(f"{field} host is not allowed")
    return value
