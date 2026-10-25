"""Serve Doubao TTS and ASR through LiteLLM's authenticated media layer.

Kollab sends only its virtual key. Provider credentials and wire details stay
here; the terminal responses expose character count or audio duration so the
existing Kollab job layer can settle usage without changing artifact handling.
ASR access depends on account entitlement (45000030 means it is not enabled).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from litellm.proxy import kollab_media_common as common
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

router = APIRouter()
_BASE = "https://openspeech.bytedance.com"
_DONE = 20000000
# The response is JSON/base64, so cap decoded audio before it is copied again.
_MAX_AUDIO_BYTES = 32 * 1024 * 1024
# 80,000 Chinese characters take 480 KB when JSON escaped; leave room for metadata.
_MAX_TTS_JSON_BYTES = 512 * 1024
_PUBLIC_ASR_ID = re.compile(r"([A-Za-z0-9_-]{1,128})~([A-Za-z0-9_-]{22})")
_ERRORS = {
    45000006: "Audio URL is not reachable by the ASR provider.",
    45000030: "ASR service is not enabled on this account.",
    45000131: "Audio exceeds the 30-minute length limit.",
    45000132: "Audio exceeds the 512 MB size limit.",
    45000151: "Unsupported audio format for transcription.",
}


def _credentials() -> tuple[str, str]:
    """Read gateway-local provider credentials for each paid request."""
    app_id = os.environ.get("VOLCANO_AUDIO_APP_ID", "").strip()
    access_key = os.environ.get("VOLCANO_AUDIO_ACCESS_KEY", "").strip()
    if not app_id or not access_key:
        raise HTTPException(503, "Doubao audio is not configured")
    return app_id, access_key


def _headers(kind: str, request_id: str, *, submit: bool = False) -> dict[str, str]:
    """Use the provider's distinct TTS and ASR app header names."""
    app_id, access_key = _credentials()
    headers = {
        "X-Api-App-Id" if kind == "tts" else "X-Api-App-Key": app_id,
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": os.environ.get(
            "VOLCANO_TTS_RESOURCE_ID" if kind == "tts" else "VOLCANO_ASR_RESOURCE_ID",
            "seed-tts-2.0" if kind == "tts" else "volc.bigasr.auc",
        ),
        "X-Api-Request-Id": request_id,
        "Content-Type": "application/json",
    }
    if submit:
        headers["X-Api-Sequence"] = "-1"
    return headers


def _token(auth: UserAPIKeyAuth) -> str:
    """Bind each ASR task to LiteLLM's hashed virtual-key identity."""
    token = getattr(auth, "token", None)
    if not isinstance(token, str) or not token:
        raise HTTPException(403, "Media key is not allowed")
    return token


def _signing_key() -> bytes:
    key = os.getenv("LITELLM_SALT_KEY") or os.getenv("LITELLM_MASTER_KEY") or ""
    if not key:
        raise HTTPException(503, "Doubao audio is not configured")
    return key.encode()


def _sign(provider_id: str, token: str) -> str:
    """Domain-separate ASR ids from other gateway task signatures."""
    digest = hmac.new(_signing_key(), f"doubao-asr:{token}:{provider_id}".encode(), hashlib.sha256).digest()
    return f"{provider_id}~{base64.urlsafe_b64encode(digest).decode()[:22]}"


def _provider_id(public_id: Any, token: str) -> str:
    """Reject another key's task before querying the shared provider account."""
    match = _PUBLIC_ASR_ID.fullmatch(public_id) if isinstance(public_id, str) else None
    if not match or not hmac.compare_digest(_sign(match.group(1), token), public_id):
        raise HTTPException(404, "ASR task not found")
    return match.group(1)


def _provider_status(response: httpx.Response) -> int:
    """Turn an ASR header status into a stable gateway status."""
    try:
        code = int(response.headers.get("x-api-status-code", ""))
    except ValueError:
        code = -1
    if code in _ERRORS:
        raise HTTPException(400, _ERRORS[code])
    if response.status_code >= 400:
        raise HTTPException(502, f"Doubao ASR HTTP {response.status_code}")
    return code


async def _post(path: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> httpx.Response:
    """Send one provider request; transport failures do not expose credentials."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(f"{_BASE}{path}", headers=headers, json=body)
    except httpx.RequestError as exc:
        raise HTTPException(502, "Doubao audio request failed") from exc


@router.post("/v1/audio/doubao/tts", tags=["audio"])
async def tts(request: Request, key: UserAPIKeyAuth = Depends(user_api_key_auth)) -> dict[str, Any]:
    """Assemble provider JSON lines into one audio payload and billable text count."""
    await common.require_model_access("doubao-tts", key)
    body = await common.read_json_object(request, max_bytes=_MAX_TTS_JSON_BYTES)
    common.reject_caller_credentials(body)
    common.reject_unknown_fields(body, {"model", "text", "voice", "format", "sample_rate", "speed_rate", "loudness_rate", "emotion", "emotion_scale", "language", "conversation_id"})
    if body.get("model") != "doubao-tts" or not isinstance(body.get("text"), str) or not body["text"].strip():
        raise common.unprocessable("model must be doubao-tts and text is required")
    params: dict[str, Any] = {"format": body.get("format") or "mp3", "sample_rate": body.get("sample_rate") or 24000}
    for field in ("speed_rate", "loudness_rate", "emotion", "emotion_scale"):
        if body.get(field) is not None:
            params[field] = body[field]
    req_params: dict[str, Any] = {"text": body["text"], "speaker": body.get("voice") or "zh_female_vv_uranus_bigtts", "audio_params": params}
    if body.get("language"):
        req_params["additions"] = json.dumps({"explicit_language": body["language"]})
    payload = {"user": {"uid": f"kollab-{body.get('conversation_id', '')}"}, "namespace": "BidirectionalTTS", "req_params": req_params}
    request_id = common.request_id(request)
    chunks: list[bytes] = []
    pending = b""
    audio_bytes = 0
    terminated = False
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", f"{_BASE}/api/v3/tts/unidirectional", headers=_headers("tts", request_id), json=payload) as response:
                if response.status_code >= 400:
                    raise HTTPException(502, f"Doubao TTS HTTP {response.status_code}")
                async for part in response.aiter_bytes():
                    if len(pending) + len(part) > 2 * _MAX_AUDIO_BYTES:
                        raise HTTPException(413, "Doubao TTS audio exceeds 32 MB limit")
                    pending += part
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        code = event.get("code")
                        if code == _DONE:
                            terminated = True
                            break
                        if code != 0:
                            raise HTTPException(502, str(event.get("message") or f"Doubao TTS error {code}"))
                        if event.get("data"):
                            chunk = base64.b64decode(event["data"])
                            audio_bytes += len(chunk)
                            if audio_bytes > _MAX_AUDIO_BYTES:
                                raise HTTPException(413, "Doubao TTS audio exceeds 32 MB limit")
                            chunks.append(chunk)
                    if terminated:
                        break
    except httpx.RequestError as exc:
        raise HTTPException(502, "Doubao TTS request failed") from exc
    if not chunks or not terminated:
        raise HTTPException(502, "Doubao TTS produced no audio data (stream did not terminate cleanly)")
    return {"audio_base64": base64.b64encode(b"".join(chunks)).decode(), "usage": {"text_chars": len(body["text"]), "model": "doubao-tts"}, "request_id": request_id}


@router.post("/v1/audio/doubao/asr/submit", tags=["audio"])
async def asr_submit(request: Request, key: UserAPIKeyAuth = Depends(user_api_key_auth)) -> dict[str, Any]:
    """Start one idempotent ASR task and return a key-bound public id."""
    await common.require_model_access("doubao-asr", key)
    token = _token(key)
    body = await common.read_json_object(request)
    common.reject_caller_credentials(body)
    common.reject_unknown_fields(body, {"model", "audio_url", "format", "language", "enable_speaker_info", "show_utterances", "enable_itn", "enable_punc", "conversation_id", "request_id"})
    if body.get("model") != "doubao-asr" or not body.get("audio_url"):
        raise common.unprocessable("model must be doubao-asr and audio_url is required")
    audio_url = common.validate_public_https_url(body["audio_url"], "audio_url")
    request_id = body.get("request_id") or str(uuid.uuid4())
    if not isinstance(request_id, str) or not common.REQUEST_ID.fullmatch(request_id):
        raise common.unprocessable("Invalid request_id")
    _signing_key()  # Fail before a paid submit if its id could not be signed.
    options = {"model_name": "bigmodel", "enable_itn": body.get("enable_itn", True), "enable_punc": body.get("enable_punc", True), "show_utterances": body.get("show_utterances", False)}
    for field in ("language", "enable_speaker_info"):
        if body.get(field):
            options[field] = body[field]
    payload = {"user": {"uid": f"kollab-{body.get('conversation_id', '')}"}, "audio": {"url": audio_url, "format": body.get("format") or "mp3", "codec": "raw"}, "request": options}
    response = await _post("/api/v3/auc/bigmodel/submit", _headers("asr", request_id, submit=True), payload, 30.0)
    if _provider_status(response) != _DONE:
        raise HTTPException(502, "Doubao ASR submit returned unexpected status")
    return {"request_id": _sign(request_id, token), "status": "queued"}


@router.post("/v1/audio/doubao/asr/query", tags=["audio"])
async def asr_query(request: Request, key: UserAPIKeyAuth = Depends(user_api_key_auth)) -> dict[str, Any]:
    """Map provider polling states and retain terminal transcript and duration."""
    await common.require_model_access("doubao-asr", key)
    token = _token(key)
    body = await common.read_json_object(request)
    common.reject_caller_credentials(body)
    common.reject_unknown_fields(body, {"model", "request_id"})
    request_id = body.get("request_id")
    if body.get("model") != "doubao-asr":
        raise common.unprocessable("model must be doubao-asr")
    request_id = _provider_id(request_id, token)
    response = await _post("/api/v3/auc/bigmodel/query", _headers("asr", request_id), {}, 30.0)
    code = _provider_status(response)
    if code in (20000001, 55000031):
        return {"status": "processing", "result": None, "error": None}
    if code == 20000002:
        return {"status": "queued", "result": None, "error": None}
    if code != _DONE:
        return {"status": "processing", "result": None, "error": f"Unexpected ASR status code {code}"}
    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(502, "Doubao ASR returned invalid JSON") from exc
    audio_info = result.get("audio_info") or {}
    return {"status": "succeeded", "result": result, "error": None, "usage": {"model": "doubao-asr", "audio_duration_ms": audio_info.get("duration")}}
