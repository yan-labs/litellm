"""Translate managed LiteLLM video calls into OpenRouter's asynchronous API.

This adapter sits in the Tokyo LiteLLM provider layer. Kollab submits only a
stable public alias to the gateway; the gateway owns OpenRouter credentials,
the provider model id, job polling, and content streaming.

Coupling: keep this adapter synchronized with ``config/litellm.{test,prod}.yaml``
and Kollab's managed video clients. The configured alias owns the upstream id;
the conversion retains request controls, terminal usage, and authenticated media.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple, Union
from urllib.parse import quote

import httpx
from httpx._types import RequestFiles

import litellm
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.base_llm.videos.transformation import BaseVideoConfig
from litellm.llms.openrouter.common_utils import OpenRouterException
from litellm.secret_managers.main import get_secret_str
from litellm.types.router import GenericLiteLLMParams
from litellm.types.videos.main import VideoCreateOptionalRequestParams, VideoObject
from litellm.types.videos.utils import (
    encode_video_id_with_provider,
    extract_original_video_id,
)

if TYPE_CHECKING:
    from litellm.litellm_core_utils.litellm_logging import Logging as _LiteLLMLoggingObj

    LiteLLMLoggingObj = _LiteLLMLoggingObj
else:
    LiteLLMLoggingObj = Any

# Only these routes accept Kollab's existing OpenRouter-specific video controls.
_EXTENDED_VIDEO_MODELS = {
    "bytedance/seedance-2.5",
    "minimax/hailuo-3",
    "x-ai/grok-imagine-video",
}


class OpenRouterVideoConfig(BaseVideoConfig):
    """Implement OpenRouter video tasks behind LiteLLM's OpenAI-style endpoints.

    The config is selected by LiteLLM's provider registry when a managed route
    uses ``custom_llm_provider: openrouter`` and ``mode: video_generation``.
    It intentionally accepts a small request allowlist so callers cannot use
    video payload fields to override gateway-controlled upstream routing.
    """

    def get_supported_openai_params(self, model: str) -> list:
        """Declare the controls accepted by the configured managed video route."""

        controls = [
            "model",
            "prompt",
            "seconds",
            "size",
            "frame_images",
            "input_references",
        ]
        if model in _EXTENDED_VIDEO_MODELS:
            controls.extend(("aspect_ratio", "resolution", "generate_audio", "seed", "provider"))
        return controls

    def map_openai_params(
        self,
        video_create_optional_params: VideoCreateOptionalRequestParams,
        model: str,
        drop_params: bool,
    ) -> Dict:
        """Keep only the controls this adapter forwards to OpenRouter.

        The shared Kollab adapter supplies ``frame_images`` and
        ``input_references`` alongside standard OpenAI-style duration and size
        fields. Keeping the filtering here prevents unknown request data from
        becoming a caller-controlled provider option.
        """

        allowed = {
            "seconds",
            "size",
            "frame_images",
            "input_references",
        }
        # Seedance 2 keeps its existing narrow wire contract; the three new
        # aliases need their existing product controls for edit and render.
        if model in _EXTENDED_VIDEO_MODELS:
            allowed.update({"aspect_ratio", "resolution", "generate_audio", "seed", "provider"})
        return {
            key: value
            for key, value in video_create_optional_params.items()
            if key in allowed and value is not None
        }

    def validate_environment(
        self,
        headers: dict,
        model: str,
        api_key: Optional[str] = None,
        litellm_params: Optional[GenericLiteLLMParams] = None,
    ) -> dict:
        """Attach the gateway-owned OpenRouter credential to one upstream call."""

        configured_key = api_key
        if not configured_key and litellm_params is not None:
            configured_key = litellm_params.get("api_key")
        configured_key = (
            configured_key
            or litellm.api_key
            or get_secret_str("OPENROUTER_API_KEY")
            or get_secret_str("OR_API_KEY")
        )
        if not configured_key:
            raise ValueError("OpenRouter API key is required for video generation")
        headers.update(
            {
                "Authorization": f"Bearer {configured_key}",
                "Content-Type": "application/json",
            }
        )
        return headers

    def get_complete_url(
        self,
        model: str,
        api_base: Optional[str],
        litellm_params: dict,
    ) -> str:
        """Return the OpenRouter video collection endpoint for create requests."""

        base_url = (
            api_base
            or litellm.api_base
            or get_secret_str("OPENROUTER_API_BASE")
            or "https://openrouter.ai/api/v1"
        )
        return self._videos_api_base(base_url)

    def transform_video_create_request(
        self,
        model: str,
        prompt: str,
        api_base: str,
        video_create_optional_request_params: Dict,
        litellm_params: GenericLiteLLMParams,
        headers: dict,
    ) -> Tuple[Dict, RequestFiles, str]:
        """Build an OpenRouter async-submit payload from the managed request.

        OpenRouter names the clip length ``duration`` instead of ``seconds``.
        Frame and reference arrays are already shaped by Kollab's signed-media
        resolver. The additional controls are admitted only for configured new
        aliases; the upstream model id always comes from the route, not payload.
        """

        request_data: Dict[str, Any] = {"model": model, "prompt": prompt}
        duration = video_create_optional_request_params.get("seconds")
        if duration is not None:
            if isinstance(duration, bool):
                raise ValueError("OpenRouter video duration must be an integer")
            try:
                request_data["duration"] = int(duration)
            except (TypeError, ValueError) as exc:
                raise ValueError("OpenRouter video duration must be an integer") from exc
        controls = ["size", "frame_images", "input_references"]
        if model in _EXTENDED_VIDEO_MODELS:
            controls.extend(("aspect_ratio", "resolution", "generate_audio", "seed", "provider"))
        for key in controls:
            value = video_create_optional_request_params.get(key)
            if value is not None:
                request_data[key] = value
        return request_data, [], self._videos_api_base(api_base)

    def transform_video_create_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
        custom_llm_provider: Optional[str] = None,
        request_data: Optional[Dict] = None,
    ) -> VideoObject:
        """Normalize an accepted OpenRouter task and retain route context in its id."""

        payload = self._parse_response(raw_response)
        return self._video_object(
            payload=payload,
            model=model,
            custom_llm_provider=custom_llm_provider,
            request_data=request_data,
        )

    def transform_video_content_request(
        self,
        video_id: str,
        api_base: str,
        litellm_params: GenericLiteLLMParams,
        headers: dict,
        variant: Optional[str] = None,
    ) -> Tuple[str, Dict]:
        """Target OpenRouter's authenticated content endpoint for raw video bytes.

        Kollab's worker calls the LiteLLM ``/content`` endpoint only after a
        completed status poll. Keeping this request on OpenRouter's equivalent
        content endpoint avoids trusting a provider-returned download URL and
        lets LiteLLM retain the authenticated media boundary.
        """

        original_video_id = extract_original_video_id(video_id)
        return (
            f"{self._videos_api_base(api_base)}/{quote(original_video_id, safe='')}/content",
            {},
        )

    def transform_video_content_response(
        self,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
    ) -> bytes:
        """Return the raw bytes streamed by OpenRouter's content endpoint."""

        self._raise_for_content_error(raw_response)
        return raw_response.content

    async def async_transform_video_content_response(
        self,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
    ) -> bytes:
        """Return raw content without a second provider fetch in async requests."""

        self._raise_for_content_error(raw_response)
        return raw_response.content

    def transform_video_remix_request(
        self,
        video_id: str,
        prompt: str,
        api_base: str,
        litellm_params: GenericLiteLLMParams,
        headers: dict,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, Dict]:
        """Reject remix because the public managed Seedance contract is generate-only."""

        raise NotImplementedError("OpenRouter video remix is not supported")

    def transform_video_remix_response(
        self,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
        custom_llm_provider: Optional[str] = None,
    ) -> VideoObject:
        """Reject remix responses because no managed remix request can be issued."""

        raise NotImplementedError("OpenRouter video remix is not supported")

    def transform_video_list_request(
        self,
        api_base: str,
        litellm_params: GenericLiteLLMParams,
        headers: dict,
        after: Optional[str] = None,
        limit: Optional[int] = None,
        order: Optional[str] = None,
        extra_query: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, Dict]:
        """Reject global listing because Kollab uses its durable per-job history instead."""

        raise NotImplementedError("OpenRouter video listing is not supported")

    def transform_video_list_response(
        self,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
        custom_llm_provider: Optional[str] = None,
    ) -> Dict[str, str]:
        """Reject list responses because the upstream list endpoint is not exposed."""

        raise NotImplementedError("OpenRouter video listing is not supported")

    def transform_video_delete_request(
        self,
        video_id: str,
        api_base: str,
        litellm_params: GenericLiteLLMParams,
        headers: dict,
    ) -> Tuple[str, Dict]:
        """Reject cancellation because the managed public route does not expose it."""

        raise NotImplementedError("OpenRouter video cancellation is not supported")

    def transform_video_delete_response(
        self,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
    ) -> VideoObject:
        """Reject delete responses because the gateway never sends cancellation requests."""

        raise NotImplementedError("OpenRouter video cancellation is not supported")

    def transform_video_status_retrieve_request(
        self,
        video_id: str,
        api_base: str,
        litellm_params: GenericLiteLLMParams,
        headers: dict,
    ) -> Tuple[str, Dict]:
        """Build one provider poll request from LiteLLM's encoded video id."""

        original_video_id = extract_original_video_id(video_id)
        return (
            f"{self._videos_api_base(api_base)}/{quote(original_video_id, safe='')}",
            {},
        )

    def transform_video_status_retrieve_response(
        self,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
        custom_llm_provider: Optional[str] = None,
    ) -> VideoObject:
        """Normalize a provider poll object without losing its terminal error state."""

        payload = self._parse_response(raw_response)
        return self._video_object(
            payload=payload,
            model=self._string_or_none(payload.get("model")),
            custom_llm_provider=custom_llm_provider,
            request_data=None,
        )

    def _parse_response(self, raw_response: httpx.Response) -> Dict[str, Any]:
        """Decode a successful provider object and preserve upstream HTTP status on failure."""

        try:
            raw_response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OpenRouterException(
                message=f"OpenRouter video request failed with HTTP {raw_response.status_code}",
                status_code=raw_response.status_code,
                headers=raw_response.headers,
            ) from exc
        try:
            payload = raw_response.json()
        except ValueError as exc:
            raise OpenRouterException(
                message="OpenRouter video request returned invalid JSON",
                status_code=raw_response.status_code,
                headers=raw_response.headers,
            ) from exc
        if not isinstance(payload, dict):
            raise OpenRouterException(
                message="OpenRouter video request returned a non-object response",
                status_code=raw_response.status_code,
                headers=raw_response.headers,
            )
        return payload

    def _video_object(
        self,
        *,
        payload: Dict[str, Any],
        model: Optional[str],
        custom_llm_provider: Optional[str],
        request_data: Optional[Dict],
    ) -> VideoObject:
        """Convert one provider task without dropping terminal usage and cost.

        The product's usage ledger consumes provider-reported cost when present;
        reconstructing an object without it would silently fall back to estimates.
        """

        provider_video_id = self._string_or_none(payload.get("id"))
        if not provider_video_id:
            raise OpenRouterException(
                message="OpenRouter video response did not include a task id",
                status_code=502,
                headers={},
            )
        encoded_id = provider_video_id
        if custom_llm_provider:
            encoded_id = encode_video_id_with_provider(
                provider_video_id,
                custom_llm_provider,
                model,
            )
        response_data: Dict[str, Any] = {
            "id": encoded_id,
            "object": "video",
            "status": self._normalize_status(payload.get("status")),
        }
        for source_key, target_key in (
            ("created_at", "created_at"),
            ("createdAt", "created_at"),
            ("completed_at", "completed_at"),
            ("completedAt", "completed_at"),
            ("progress", "progress"),
            ("error", "error"),
            ("size", "size"),
            ("usage", "usage"),
        ):
            value = payload.get(source_key)
            if value is not None and target_key not in response_data:
                response_data[target_key] = value
        if model:
            response_data["model"] = model
        # OpenRouter can report cost at task level while LiteLLM's VideoObject
        # exposes usage only. Place it there for the product's existing ledger.
        if payload.get("cost") is not None or payload.get("cost_usd") is not None:
            usage = dict(response_data.get("usage") or {})
            usage.setdefault("cost", payload.get("cost", payload.get("cost_usd")))
            response_data["usage"] = usage
        if request_data:
            if "duration" in request_data:
                response_data["seconds"] = str(request_data["duration"])
            if "size" in request_data and "size" not in response_data:
                response_data["size"] = request_data["size"]
        return VideoObject(**response_data)

    def _raise_for_content_error(self, raw_response: httpx.Response) -> None:
        """Preserve content-endpoint failures before returning media bytes.

        This keeps a not-yet-ready or unauthorized content request distinguishable
        from a successful but empty media stream in the shared proxy error path.
        """

        try:
            raw_response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OpenRouterException(
                message=(
                    "OpenRouter video content request failed with HTTP "
                    f"{raw_response.status_code}"
                ),
                status_code=raw_response.status_code,
                headers=raw_response.headers,
            ) from exc

    @staticmethod
    def _normalize_status(value: Any) -> str:
        """Map OpenRouter task terms onto the stable LiteLLM video status vocabulary."""

        normalized = str(value or "queued").strip().lower()
        return {
            "pending": "queued",
            "in_progress": "processing",
            "running": "processing",
            "success": "completed",
            "succeeded": "completed",
            "done": "completed",
            "fail": "failed",
            "error": "failed",
        }.get(normalized, normalized)

    @staticmethod
    def _string_or_none(value: Any) -> Optional[str]:
        """Normalize optional response strings before they enter an encoded task id."""

        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _videos_api_base(api_base: str) -> str:
        """Normalize either an API root or endpoint into one OpenRouter videos URL."""

        normalized = api_base.rstrip("/")
        return normalized if normalized.endswith("/videos") else f"{normalized}/videos"

    def get_error_class(
        self,
        error_message: str,
        status_code: int,
        headers: Union[dict, httpx.Headers],
    ) -> BaseLLMException:
        """Preserve OpenRouter failure status when LiteLLM's HTTP handler catches an error."""

        raise OpenRouterException(
            status_code=status_code,
            message=error_message,
            headers=headers,
        )
