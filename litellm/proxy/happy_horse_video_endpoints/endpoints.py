"""Expose Happy Horse async video jobs through the authenticated LiteLLM proxy.

Kollab submits, polls and downloads through this route; only the gateway reads
DashScope credentials. The public task id is bound to the submitting virtual key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from litellm.proxy import kollab_media_common as common
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

router = APIRouter()
ALIAS = "happy-horse"
DASHSCOPE_BASE = "https://dashscope-intl.aliyuncs.com/api/v1"
_MODELS = {
    "t2v": "happyhorse-1.1-t2v",
    "i2v": "happyhorse-1.1-i2v",
    "r2v": "happyhorse-1.1-r2v",
    "edit": "happyhorse-1.0-video-edit",
}
_PUBLIC_ID = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.:-]{0,255})~([A-Za-z0-9_-]{22})")
_ALLOWED = {
    "model", "operation", "prompt", "duration", "ratio", "resolution", "size",
    "image_url", "first_frame_image_url", "last_frame_image_url",
    "reference_image_urls", "video_urls", "audio_urls", "seed", "watermark", "audio_setting",
}


def _key() -> str:
    """Resolve the gateway-owned DashScope key for one upstream request."""
    key = (os.getenv("ALIYUN_VIDEO_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or "").strip()
    if not key:
        raise HTTPException(503, "Happy Horse is not configured")
    return key


def _token(auth: UserAPIKeyAuth) -> str:
    """Require the hashed virtual-key identity used to bind public task ids."""
    token = getattr(auth, "token", None)
    if not isinstance(token, str) or not token:
        raise HTTPException(403, "Media key is not allowed")
    return token


def _signing_key() -> bytes:
    """Use the gateway's stable secret so a task id cannot cross virtual keys."""
    key = os.getenv("LITELLM_SALT_KEY") or os.getenv("LITELLM_MASTER_KEY") or ""
    if not key:
        raise HTTPException(503, "Happy Horse is not configured")
    return key.encode()


def _sign(provider_id: str, token: str) -> str:
    """Encode a provider task id for the creating virtual key only."""
    digest = hmac.new(_signing_key(), f"{ALIAS}:{token}:{provider_id}".encode(), hashlib.sha256).digest()
    return f"{provider_id}~{base64.urlsafe_b64encode(digest).decode()[:22]}"


def _provider_id(public_id: str, token: str) -> str:
    """Verify one gateway-issued id before reading a shared provider account."""
    match = _PUBLIC_ID.fullmatch(public_id)
    if not match or ".." in match.group(1) or not hmac.compare_digest(_sign(match.group(1), token), public_id):
        raise HTTPException(404, "Video not found")
    return match.group(1)


def _submit_body(body: dict[str, Any]) -> dict[str, Any]:
    """Select the DashScope model from operation and media shape at the gateway."""
    common.reject_caller_credentials(body)
    common.reject_unknown_fields(body, _ALLOWED)
    if body.get("model") != ALIAS:
        raise common.unprocessable(f"model must be {ALIAS}")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise common.unprocessable("prompt is required")
    if body.get("last_frame_image_url") or body.get("audio_urls"):
        raise common.unprocessable("Unsupported Happy Horse media")
    first = body.get("first_frame_image_url") or body.get("image_url")
    references = body.get("reference_image_urls") or []
    videos = body.get("video_urls") or []
    if not isinstance(references, list) or not isinstance(videos, list):
        raise common.unprocessable("Media URLs must be lists")
    operation = body.get("operation") or "generate"
    if operation == "edit":
        if len(videos) != 1 or not isinstance(videos[0], str):
            raise common.unprocessable("Happy Horse edit requires one source video")
        images = [value for value in [body.get("image_url"), body.get("first_frame_image_url"), *references] if value]
        if len(images) > 5:
            raise common.unprocessable("Happy Horse edit supports at most 5 reference images")
        media = [{"type": "video", "url": common.validate_public_https_url(videos[0], "video_urls")}]
        media.extend({"type": "reference_image", "url": common.validate_public_https_url(url, "reference_image_urls")} for url in images)
        input_body = {"prompt": prompt.strip(), "media": media}
        mode = "edit"
    else:
        if videos or operation != "generate":
            raise common.unprocessable("Unsupported Happy Horse operation")
        images = [value for value in [first, *references] if value]
        mode = "r2v" if references else "i2v" if first else "t2v"
        input_body = {"prompt": prompt.strip()}
        if mode == "r2v":
            input_body["media"] = [{"type": "reference_image", "url": common.validate_public_https_url(url, "reference_image_urls")} for url in images]
        elif mode == "i2v":
            input_body["img_url"] = common.validate_public_https_url(first, "first_frame_image_url")
    parameters: dict[str, Any] = {"watermark": bool(body.get("watermark", False))}
    for field in (("resolution", "audio_setting", "seed") if mode == "edit" else ("duration", "ratio", "resolution", "seed")):
        value = body.get(field)
        if value is not None:
            parameters[field] = str(value).upper() if field == "resolution" else value
    if mode == "edit" and parameters.get("audio_setting") in {"copy", "original", "keep", "keep_original", "preserve"}:
        parameters["audio_setting"] = "origin"
    return {"model": _MODELS[mode], "input": input_body, "parameters": parameters}


async def _request(method: str, path: str, *, payload: dict[str, Any] | None = None) -> httpx.Response:
    """Call DashScope once with the gateway credential and async submit header."""
    headers = {"Authorization": f"Bearer {_key()}"}
    if method == "POST":
        headers["X-DashScope-Async"] = "enable"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            return await client.request(method, f"{DASHSCOPE_BASE}{path}", headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Happy Horse is temporarily unavailable") from exc


def _json(upstream: httpx.Response) -> dict[str, Any]:
    """Read one provider envelope without exposing a malformed response."""
    try:
        body = upstream.json()
    except ValueError as exc:
        raise HTTPException(502, "Happy Horse returned an invalid response") from exc
    if not isinstance(body, dict):
        raise HTTPException(502, "Happy Horse returned an invalid response")
    return body


def _relay(upstream: httpx.Response, token: str) -> Response:
    """Return a signed task id and $5.6/M video-token usage for settlement.

    Kollab's existing Happy Horse price table uses this rate; retain the raw
    provider token count so its durable runner can settle the same way.
    """
    body = _json(upstream)
    output = body.get("output")
    if upstream.is_success and isinstance(output, dict) and isinstance(output.get("task_id"), str):
        body["output"] = {**output, "task_id": _sign(output["task_id"], token)}
        usage = body.get("usage")
        if output.get("task_status") == "SUCCEEDED" and isinstance(usage, dict):
            tokens = usage.get("video_tokens")
            if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and tokens >= 0:
                body["usage"] = {**usage, "output_tokens": tokens, "cost_usd": round(tokens * 5.6 / 1_000_000, 6)}
    return Response(content=json.dumps(body), status_code=upstream.status_code, media_type="application/json")


@router.post("/v1/happy-horse/videos", tags=["video"])
async def submit_happy_horse_video(request: Request, user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth)) -> Response:
    """Submit one async DashScope job after checking the managed model grant."""
    await common.require_model_access(ALIAS, user_api_key_dict)
    token = _token(user_api_key_dict)
    body = _submit_body(await common.read_json_object(request))
    _signing_key()
    upstream = await _request("POST", "/services/aigc/video-generation/video-synthesis", payload=body)
    return _relay(upstream, token)


@router.get("/v1/happy-horse/videos/{video_id}", tags=["video"])
async def get_happy_horse_video(video_id: str, user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth)) -> Response:
    """Poll a gateway-issued task and include usable terminal cost fields."""
    await common.require_model_access(ALIAS, user_api_key_dict)
    token = _token(user_api_key_dict)
    return _relay(await _request("GET", f"/tasks/{_provider_id(video_id, token)}"), token)


@router.get("/v1/happy-horse/videos/{video_id}/content", tags=["video"])
async def get_happy_horse_video_content(video_id: str, user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth)) -> Response:
    """Stream verified completed video without buffering its full body in the gateway."""
    await common.require_model_access(ALIAS, user_api_key_dict)
    token = _token(user_api_key_dict)
    upstream = await _request("GET", f"/tasks/{_provider_id(video_id, token)}")
    if upstream.status_code != 200:
        return _relay(upstream, token)
    output = _json(upstream).get("output")
    if not isinstance(output, dict) or output.get("task_status") != "SUCCEEDED":
        raise HTTPException(409, "Video is not ready")
    video_url = common.validate_public_https_url(output.get("video_url"), "video_url")
    client = httpx.AsyncClient(timeout=300, follow_redirects=True)
    try:
        video = await client.send(client.build_request("GET", video_url), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, "Happy Horse content is temporarily unavailable") from exc

    async def body():
        # Release both connections on completion or caller disconnect.
        try:
            async for chunk in video.aiter_raw():
                yield chunk
        finally:
            await video.aclose()
            await client.aclose()

    headers = {}
    for name in ("content-length", "content-encoding"):
        if video.headers.get(name):
            headers[name] = video.headers[name]
    return StreamingResponse(body(), status_code=video.status_code, media_type=video.headers.get("content-type", "video/mp4"), headers=headers)
