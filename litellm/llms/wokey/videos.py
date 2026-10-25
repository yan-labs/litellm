"""Wokey asynchronous videos using LiteLLM's existing OpenAI video transport.

Submission returns a provider-encoded ID immediately. Subsequent status/content
requests use that identity and never submit another job or change provider.
"""

import math
import re
from urllib.parse import quote
from uuid import uuid4

from litellm.llms.openai.videos.transformation import OpenAIVideoConfig
from litellm.types.videos.main import VideoObject
from litellm.types.videos.utils import (
    decode_video_id_with_provider,
    encode_video_id_with_provider,
)

from .common import (
    API_BASE,
    auth,
    error,
    image_file,
    payload,
    uncertain_submission,
    usage,
)


class WokeyVideoConfig(OpenAIVideoConfig):
    """Map Kollab's current Grok inputs, retaining native async task lifecycle."""

    def get_supported_openai_params(self, model):
        """Accept existing gateway controls; provider-specific values use extra_body."""
        return ["model", "prompt", "seconds", "size", "input_reference", "extra_headers", "extra_body", "frame_images", "input_references", "aspect_ratio", "resolution"]

    def validate_environment(self, headers, model, api_key=None, litellm_params=None):
        """Keep all job reads authenticated with the deployment's named key."""
        return auth(headers, api_key or (litellm_params or {}).get("api_key"))

    def get_complete_url(self, model, api_base, litellm_params):
        """Use the documented canonical authenticated video routes."""
        return (api_base or API_BASE).rstrip("/") + "/videos"

    def transform_video_create_request(self, model, prompt, api_base, video_create_optional_request_params, litellm_params, headers):
        """Map before submitting, retaining a caller's Idempotency-Key unchanged.

        Only one image is documented for image_to_video. Multiple references,
        last frames and unsupported ratios reject here so the original input
        can use Kie fallback. No reference is silently truncated.
        """
        p = dict(video_create_optional_request_params)
        p.update(p.pop("extra_body", {}) or {})
        if model not in ("grok-imagine-video-1.5", "grok-imagine-video-1.5-lite") or not prompt or not prompt.strip():
            raise error(400, "Unknown Wokey video model or empty prompt")
        raw_seconds = p.pop("seconds", p.pop("duration_seconds", p.pop("duration", 8)))
        ratio = p.pop("aspect_ratio", p.pop("ratio", None))
        resolution = p.pop("resolution", p.pop("video_resolution", "480p"))
        size = p.pop("size", None)
        try:
            seconds = int(raw_seconds)
            if isinstance(raw_seconds, bool) or float(raw_seconds) != seconds or not 1 <= seconds <= 15:
                raise ValueError()
            if size:
                w, h = map(int, size.lower().split("x"))
                if w <= 0 or h <= 0:
                    raise ValueError()
                divisor = math.gcd(w, h)
                ratio = ratio or f"{w // divisor}:{h // divisor}"
                # Kollab sends explicit resolution alongside rounded audit dimensions.
                if "resolution" not in video_create_optional_request_params and not (video_create_optional_request_params.get("extra_body") or {}).get("resolution"):
                    resolution = {480: "480p", 720: "720p", 1080: "1080p"}.get(min(w, h))
        except (ValueError, TypeError, AttributeError):
            raise error(400, "Unsupported Wokey video duration or size") from None
        # auto has no Wokey equivalent: keep it on Kie rather than changing framing.
        ratio = ratio or "16:9"
        if ratio not in ("1:1", "3:4", "4:3", "9:16", "16:9", "21:9") or resolution not in ("480p", "720p", "1080p"):
            raise error(400, "Unsupported Wokey video ratio or resolution; use Kie fallback")
        images = []
        reference = p.pop("input_reference", None)
        if reference is not None:
            images.append(reference)
        for frame in p.pop("frame_images", None) or []:
            if frame.get("frame_type", "first_frame") != "first_frame":
                raise error(400, "Wokey Grok last frames are unsupported")
            images.append((frame.get("image_url") or {}).get("url"))
        for reference in p.pop("input_references", None) or []:
            if reference.get("type") != "image_url":
                raise error(400, "Wokey Grok accepts image references only")
            image = reference.get("image_url")
            images.append(image.get("url") if isinstance(image, dict) else image)
        images.extend(p.pop("image_urls", None) or [])
        for key in ("model", "prompt", "extra_headers", "user"):
            p.pop(key, None)
        if p or len(images) > 1:
            raise error(400, "Unsupported Wokey Grok control or reference count; use Kie fallback")
        body = {"model": model, "prompt": prompt, "duration_seconds": seconds, "ratio": ratio, "video_resolution": resolution, "mode": "image_to_video" if images else "text_to_video"}
        key_name = next((key for key in headers if key.lower() == "idempotency-key"), "Idempotency-Key")
        headers.setdefault(key_name, "wokey-media-" + uuid4().hex)
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,160}", headers[key_name]):
            raise error(400, "Invalid video Idempotency-Key")
        files = [("image", image_file(images[0]))] if images else []
        if files:
            for key in list(headers):
                if key.lower() == "content-type":
                    del headers[key]
        return body, files, api_base

    def transform_video_create_response(self, model, raw_response, logging_obj, custom_llm_provider=None, request_data=None):
        """Return accepted jobs without billing their reserved amount.

        A success envelope without an ID is ambiguous, so it must not fallback.
        HTTP 4xx/5xx rejection remains the existing submission fallback boundary.
        """
        if raw_response.status_code >= 400:
            payload(raw_response)
        try:
            data = payload(raw_response)
            return self._video(data, model, settled=False)
        except Exception:  # noqa: BLE001 - accepted paid POSTs must never trigger fallback
            # Even a known ID with an unknown status represents a paid accepted POST.
            raise uncertain_submission() from None

    def _video(self, data, model, settled):
        """Normalize native status and preserve usage only for completed jobs."""
        task_id = data.get("id")
        if not task_id:
            raise error(502, "Wokey video response has no ID")
        status = {"in_progress": "processing", "queued": "queued", "completed": "completed", "failed": "failed", "cancelled": "failed"}.get(data.get("status"))
        if status is None:
            raise error(502, "Unknown Wokey video status")
        result = VideoObject(id=encode_video_id_with_provider(task_id, "wokey", model), object="video", model=model, status=status, error=data.get("error"), usage=usage(data) if settled and status == "completed" else None,
                             seconds=str(data["duration_seconds"]) if data.get("duration_seconds") is not None else None)
        # Submit reserves balance; GET is a read. Kollab settles completed usage once.
        result._hidden_params = {"additional_headers": {"llm_provider-x-litellm-response-cost": 0.0}}
        return result

    def transform_video_status_retrieve_request(self, video_id, api_base, litellm_params, headers):
        """Bind the encoded model/ID for this request's response transformation."""
        decoded = decode_video_id_with_provider(video_id)
        self._model = decoded.get("model_id")
        self._task_id = decoded["video_id"]
        return f"{api_base}/{quote(self._task_id, safe='')}", {}

    def transform_video_status_retrieve_response(self, raw_response, logging_obj, custom_llm_provider=None):
        """Expose completed price_usd, keeping failed/cancelled jobs unbilled."""
        return self._video(payload(raw_response), self._model, settled=True)

    def transform_video_content_request(self, video_id, api_base, litellm_params, headers, variant=None):
        """Download the canonical authenticated content_url route immediately.

        This is the documented /videos/{id}/content route. It avoids returning
        provider URLs and does not forward a key to a response-supplied host.
        """
        if variant:
            raise error(400, "Wokey content variants are unsupported")
        task_id = decode_video_id_with_provider(video_id)["video_id"]
        return f"{api_base}/{quote(task_id, safe='')}/content", {}

    def transform_video_content_response(self, raw_response, logging_obj):
        """Return finished MP4 bytes to the existing artifact upload pipeline."""
        if raw_response.status_code >= 400:
            raise error(raw_response.status_code, "Wokey video download failed")
        if not raw_response.content or "video/mp4" not in raw_response.headers.get("content-type", ""):
            raise error(502, "Wokey video download is not MP4")
        return raw_response.content

    def get_error_class(self, error_message, status_code, headers):
        """Sanitize transport errors on job reads; router retains their provider."""
        return error(status_code, "Wokey video request failed")

    def unsupported(self, *args, **kwargs):
        """Do not inherit OpenAI operations with unverified Wokey semantics."""
        raise error(400, "Wokey supports create, status and content only")

    transform_video_remix_request = unsupported
    transform_video_list_request = unsupported
    transform_video_delete_request = unsupported
