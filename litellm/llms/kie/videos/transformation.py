"""Kie asynchronous videos behind LiteLLM's existing video-id contract."""

from urllib.parse import urlencode

import httpx

from litellm.llms.base_llm.videos.transformation import BaseVideoConfig
from litellm.types.videos.main import VideoObject
from litellm.types.videos.utils import (
    decode_video_id_with_provider,
    encode_video_id_with_provider,
)

from ..common_utils import API_BASE, auth, envelope, failure, result_urls, usage
from ..model_config import build_input, config


class KieVideoConfig(BaseVideoConfig):
    def get_supported_openai_params(self, model):
        return [
            "model",
            "prompt",
            "seconds",
            "size",
            "frame_images",
            "input_references",
            "aspect_ratio",
            "resolution",
            "generate_audio",
            "extra_body",
        ] + config(model)["allowed"]

    def map_openai_params(self, video_create_optional_params, model, drop_params):
        # Leave unsupported controls visible to validation, even with drop_params.
        return dict(video_create_optional_params)

    def validate_environment(self, headers, model, api_key=None, litellm_params=None):
        return auth(headers, api_key or (litellm_params or {}).get("api_key"))

    def get_complete_url(self, model, api_base, litellm_params):
        return (api_base or API_BASE).rstrip("/")

    def transform_video_create_request(
        self,
        model,
        prompt,
        api_base,
        video_create_optional_request_params,
        litellm_params,
        headers,
    ):
        return (
            build_input(model, prompt, video_create_optional_request_params),
            [],
            f"{api_base}/jobs/createTask",
        )

    def transform_video_create_response(
        self,
        model,
        raw_response,
        logging_obj,
        custom_llm_provider=None,
        request_data=None,
    ):
        task = envelope(raw_response)
        task_id = task.get("taskId")
        if not task_id:
            raise failure(502, "Kie createTask returned no taskId")
        response = VideoObject(
            id=encode_video_id_with_provider(task_id, "kie", model),
            object="video",
            model=model,
            status="queued",
        )
        # Submission is not completed generation. Do not bill estimated clip costs.
        response._hidden_params = {
            "additional_headers": {"llm_provider-x-litellm-response-cost": 0.0}
        }
        return response

    def transform_video_status_retrieve_request(
        self, video_id, api_base, litellm_params, headers
    ):
        decoded = decode_video_id_with_provider(video_id)
        self._model = decoded.get("model_id")
        self._task_id = decoded["video_id"]
        return f"{api_base}/jobs/recordInfo?{urlencode({'taskId': self._task_id})}", {}

    def transform_video_status_retrieve_response(
        self, raw_response, logging_obj, custom_llm_provider=None
    ):
        task = envelope(raw_response, allow_pending=True)
        state = task.get("state")
        status = {
            "waiting": "queued",
            "queuing": "queued",
            "generating": "processing",
            "success": "completed",
            "fail": "failed",
        }.get(state, "queued")
        task_id = task.get("taskId") or getattr(self, "_task_id", None)
        if not task_id:
            raise failure(502, "Kie recordInfo returned no taskId")
        model = getattr(self, "_model", None) or task.get("model")
        error = None
        if status == "failed":
            exc = failure(task.get("failCode"), task.get("failMsg"))
            error = {
                "code": str(task.get("failCode") or exc.status_code),
                "message": exc.message,
            }
        response = VideoObject(
            id=encode_video_id_with_provider(task_id, "kie", model),
            object="video",
            status=status,
            model=model,
            error=error,
            usage=usage(task) if status == "completed" else None,
        )
        # Reads are not new generations. Terminal usage is consumed once by
        # Kollab's durable job ledger, rather than charged on every GET.
        response._hidden_params = {
            "additional_headers": {"llm_provider-x-litellm-response-cost": 0.0}
        }
        return response

    def transform_video_content_request(
        self, video_id, api_base, litellm_params, headers, variant=None
    ):
        if variant:
            raise failure(400, "Kie video content variants are unsupported")
        return self.transform_video_status_retrieve_request(
            video_id, api_base, litellm_params, headers
        )

    @staticmethod
    def content_url(raw_response):
        task = envelope(raw_response)
        if task.get("state") == "fail":
            raise failure(task.get("failCode"), task.get("failMsg"))
        if task.get("state") != "success":
            raise failure(409, "Kie video content is not ready")
        return result_urls(task)[0]

    def transform_video_content_response(self, raw_response, logging_obj):
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            response = client.get(self.content_url(raw_response))
            response.raise_for_status()
            return response.content

    async def async_transform_video_content_response(self, raw_response, logging_obj):
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(self.content_url(raw_response))
            response.raise_for_status()
            return response.content

    def get_error_class(self, error_message, status_code, headers):
        return failure(status_code, error_message)

    def unsupported(self, *args, **kwargs):
        raise NotImplementedError("Kie only supports create, retrieve and content")

    transform_video_remix_request = unsupported
    transform_video_remix_response = unsupported
    transform_video_list_request = unsupported
    transform_video_list_response = unsupported
    transform_video_delete_request = unsupported
    transform_video_delete_response = unsupported
