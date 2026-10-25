"""Managed TEST-only TypeSafe judgment passthrough for Kollab's Claude guard.

Generic authenticated passthrough requires a premium license and accepts only
env headers; this small route reuses gateway auth and named DB credentials.
No supplier credential is forwarded to Kollab or written in request logs.
"""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from litellm.litellm_core_utils.credential_accessor import CredentialAccessor
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy._types import LiteLLMRoutes

# Existing virtual-key/team auth treats the judgment path as a managed API route.
if "/jev/v1/systemone" not in LiteLLMRoutes.openai_routes.value:
    LiteLLMRoutes.openai_routes.value.append("/jev/v1/systemone")

router = APIRouter()


@router.post("/jev/v1/systemone", dependencies=[Depends(user_api_key_auth)])
async def judge(request: Request) -> Response:
    """Forward judgment JSON once using typesafe-jev; preserve upstream status/body.

    The mounted config explicitly opts in. PROD defaults to 404. Fixed upstream
    URL, no forwarded client headers, redirects, retries or model fallback.
    """
    from litellm.proxy.proxy_server import proxy_config

    config = proxy_config.get_config_state() or {}
    if not config.get("jev_passthrough", {}).get("enabled", False):
        raise HTTPException(status_code=404, detail="JEV passthrough is inactive")
    key = CredentialAccessor.get_credential_values("typesafe-jev").get("api_key")
    if not key:
        raise HTTPException(status_code=503, detail="JEV credential unavailable")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid judgment JSON") from None
    if not isinstance(body, dict) or not isinstance(body.get("state"), (str, dict)) or not isinstance(body.get("questions"), dict):
        raise HTTPException(status_code=400, detail="state and questions are required")
    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
        try:
            upstream = await client.post("https://api.typesafe.ai/v1/systemone", json=body,
                                         headers={"Authorization": "Bearer " + key})
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="JEV upstream unavailable") from None
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type="application/json")
