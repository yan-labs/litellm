"""OpenAI-compatible Wokey images behind the existing generations/edits contract.

LiteLLM's shared sync/async HTTP handlers do the I/O. This layer maps Kollab's
size/quality/aspect controls and exposes inline bytes plus settled billing.
"""


from math import gcd

from litellm.llms.openai.image_edit.transformation import OpenAIImageEditConfig
from litellm.llms.openai.image_generation.gpt_transformation import (
    GPTImageGenerationConfig,
)
from litellm.types.utils import ImageResponse, WokeyImageUsage

from .common import API_BASE, auth, error, image_file, payload, usage


def image_body(model, prompt, params):
    """Map current Kollab controls; unsupported operations fail before paid I/O.

    n>1 stays on the existing Kie fallback: Wokey documents n=1 only. Explicit
    dimensions keep their aspect while quality selects the requested longest side.
    """
    p = dict(params)
    p.update(p.pop("extra_body", {}) or {})
    if not isinstance(prompt, str) or not prompt.strip():
        raise error(400, "Image prompt is required")
    if model not in ("gpt-image-2.5", "gpt-image-2", "grok-imagine-image-2.0"):
        raise error(400, "Unknown Wokey image model")
    n = p.pop("n", 1)
    if isinstance(n, bool) or n not in (1, "1"):
        raise error(400, "Wokey images support n=1; use Kie fallback for batches")
    size = p.pop("size", None) or "auto"
    quality = p.pop("quality", None) or "auto"
    ratio = p.pop("aspect_ratio", None)
    resolution = p.pop("resolution", None)
    background = p.pop("background", None)
    response_format = p.pop("response_format", "b64_json")
    for field in ("model", "prompt", "extra_headers", "user"):
        p.pop(field, None)
    if p or response_format not in ("b64_json", None):
        raise error(400, "Unsupported Wokey image control")
    if quality not in ("auto", "low", "medium", "high"):
        raise error(400, "Unsupported image quality")
    if background not in (None, "auto", "opaque"):
        # Transparency is not documented; preserve it through Kie instead of dropping it.
        raise error(400, "Wokey background control is unverified; use Kie fallback")
    try:
        w, h = (1024, 1024) if size == "auto" else map(int, size.lower().split("x"))
        if ratio and ratio != "auto":
            w, h = map(int, ratio.split(":"))
        if w <= 0 or h <= 0:
            raise ValueError()
        aspect_w, aspect_h = w, h
        edge = {"low": 1024, "medium": 2048, "high": 4096}.get(quality)
        if resolution:
            edge = {"1K": 1024, "2K": 2048, "4K": 4096}.get(str(resolution).upper())
            if edge is None:
                raise ValueError()
        if edge or (ratio and ratio != "auto"):
            scale = (edge or 1024) / max(w, h)
            w, h = round(w * scale), round(h * scale)
        dimensions = (w, h)
        if model in ("gpt-image-2.5", "gpt-image-2") and size != "auto":
            dimensions += tuple(map(int, size.lower().split("x")))
        if not all(128 <= side <= 4096 for side in dimensions):
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise error(400, "Image dimensions must be 128-4096 per side") from None
    if model == "grok-imagine-image-2.0":
        # Wokey's Grok catalog accepts fixed sizes, unlike GPT's free dimensions.
        sizes = [(1024, 1024), (2048, 2048), (1536, 1024), (1024, 1536), (2048, 1152), (1152, 2048)]
        matches = [(a, b) for a, b in sizes if a * aspect_h == b * aspect_w and max(a, b) >= max(w, h)]
        if not matches:
            raise error(400, "Unsupported Wokey Grok image size; use Kie fallback")
        w, h = min(matches, key=lambda size: max(size))
    elif size != "auto" or (ratio and ratio != "auto"):
        # Guide upstream framing without changing returned pixels.
        divisor = gcd(aspect_w, aspect_h)
        prompt += (
            f"\nRequested output canvas: {w}x{h} pixels, "
            f"aspect ratio {aspect_w // divisor}:{aspect_h // divisor}. "
            "Compose the image within this canvas."
        )
    return {"model": model, "prompt": prompt, "n": 1, "size": f"{w}x{h}"}


def image_response(model, response, params=None):
    """Return inline images and truthful usage in the same envelope as Kie."""
    data = payload(response)
    images = data.get("data")
    if not isinstance(images, list) or not images or any(not item.get("b64_json") for item in images):
        raise error(502, "Wokey image response has no inline image")
    billing = usage(data, response)
    result = ImageResponse(data=images, usage=WokeyImageUsage(**billing))
    result._hidden_params.update(model=model, custom_llm_provider="wokey")
    if billing["cost_authoritative"]:
        result._hidden_params["additional_headers"] = {"llm_provider-x-litellm-response-cost": billing["cost_usd"]}
    return result


class WokeyImageGenerationConfig(GPTImageGenerationConfig):
    """Reuse OpenAI generation transport with Wokey request/usage transforms."""

    def get_supported_openai_params(self, model):
        """Expose the controls already sent by Kollab's media callers."""
        return ["n", "quality", "response_format", "size", "background", "extra_body", "extra_headers", "aspect_ratio", "resolution"]

    def validate_environment(self, headers, model, messages=None, optional_params=None, litellm_params=None, api_key=None, api_base=None):
        """Use only the server-selected credential."""
        return auth(headers, api_key or (litellm_params or {}).get("api_key"))

    def get_complete_url(self, api_base=None, **kwargs):
        """Normalize a deployment base once, avoiding doubled /v1."""
        return (api_base or API_BASE).rstrip("/") + "/images/generations"

    def transform_image_generation_request(self, model, prompt, optional_params, litellm_params, headers):
        """Map parameters before the shared handler's JSON POST."""
        return image_body(model, prompt, optional_params)

    def transform_image_generation_response(self, model, raw_response, **kwargs):
        """Preserve b64_json and provider cost for downstream artifact settlement."""
        return image_response(model, raw_response, kwargs.get("optional_params"))

    def get_error_class(self, error_message, status_code, headers):
        """Return a sanitized error usable by LiteLLM's existing fallback logic."""
        return error(status_code, "Wokey image request failed")


class WokeyImageEditConfig(OpenAIImageEditConfig):
    """Reuse multipart edit transport but submit to Wokey generations."""

    def get_supported_openai_params(self, model):
        """Share generation controls; mask editing is not documented by Wokey."""
        return WokeyImageGenerationConfig().get_supported_openai_params(model)

    def validate_environment(self, headers, model, api_key=None):
        """Authenticate the selected named credential only."""
        return auth(headers, api_key)

    def get_complete_url(self, model, api_base, litellm_params):
        """Use Grok's catalogued edits endpoint; GPT keeps its documented multipart path."""
        if model == "grok-imagine-image-2.0":
            return (api_base or API_BASE).rstrip("/") + "/images/edits"
        return WokeyImageGenerationConfig().get_complete_url(api_base)

    def transform_image_edit_request(self, model, prompt, image, image_edit_optional_request_params, litellm_params, headers):
        """Forward one file using image, never OpenAI's image[] or a temporary URL."""
        images = image if isinstance(image, list) else [image]
        if len(images) != 1 or images[0] is None:
            raise error(400, "Wokey multi-image edits are unverified; use Kie fallback")
        body = image_body(model, prompt, image_edit_optional_request_params)
        for field in list(headers):
            if field.lower() == "content-type":
                del headers[field]
        return body, [("image", image_file(images[0]))]

    def transform_image_edit_response(self, model, raw_response, logging_obj):
        """Return the shared inline-image response after multipart editing."""
        return image_response(model, raw_response, logging_obj.optional_params)

    def get_error_class(self, error_message, status_code, headers):
        """Sanitize provider error bodies before router fallback."""
        return error(status_code, "Wokey image edit request failed")
