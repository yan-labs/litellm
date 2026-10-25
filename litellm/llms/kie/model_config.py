"""Execute data-driven Kie capabilities from LiteLLM's model catalog."""

import json
import math
from pathlib import Path

import litellm
from litellm.utils import get_model_info

from .common_utils import failure

TRANSPORT_PARAMS = {"extra_headers", "response_format", "n", "user", "model", "prompt"}


def config(model: str) -> dict:
    """Resolve packaged Kie capabilities after Router pricing registration.

    Router can register an entry with pricing/mode but no provider capabilities;
    reload that incomplete entry before mapping a Wokey submission fallback.
    """
    name = model.removeprefix("kie/")
    if "kie_config" not in litellm.model_cost.get("kie/" + name, {}):
        # Package wheels use the bundled cost-map backup, which can lag the root
        # catalog. Register only our packaged provider records before lookup.
        records = json.loads(Path(__file__).with_name("model_catalog.json").read_text())
        if "kie/" + name not in records:
            raise failure(400, "Unknown Kie model")
        litellm.register_model(records)
    return get_model_info(model=name, custom_llm_provider="kie")["kie_config"]


def media_url(value):
    if isinstance(value, str):
        return value
    for kind in ("image", "video", "audio"):
        item = value.get(kind + "_url")
        if item:
            return item.get("url") if isinstance(item, dict) else item
    raise failure(400, "Reference must contain a media URL")


def build_input(model: str, prompt: str, params: dict, edit: bool = False) -> dict:
    rule = config(model)
    p = {**params}
    p.update(p.pop("extra_body", {}) or {})
    inputs = dict(rule["defaults"])
    inputs["prompt"] = prompt
    if len(prompt) > rule.get("prompt_limit", 20000):
        raise failure(400, "Prompt exceeds model limit")
    size = p.pop("size", None)
    if size and size != "auto":
        try:
            w, h = map(int, size.lower().split("x"))
            divisor = math.gcd(w, h)
            p.setdefault("aspect_ratio", f"{w // divisor}:{h // divisor}")
            if rule.get("size_resolution"):
                p.setdefault(
                    "resolution",
                    rule["size_resolution"].get(str(min(w, h)), "unsupported"),
                )
        except (ValueError, AttributeError):
            raise failure(400, "Size must be WIDTHxHEIGHT") from None
    quality = p.pop("quality", None)
    if quality and quality != "auto":
        resolution = rule.get("quality", {}).get(quality)
        if resolution is None:
            raise failure(400, "Quality is unsupported for this Kie model")
        p.setdefault("resolution", resolution)
    seconds = p.pop("seconds", None)
    if seconds is not None:
        p["duration"] = seconds
    frames = p.pop("frame_images", None) or []
    refs = p.pop("input_references", None) or []
    frame_fields = rule.get("frames", {})
    if (
        frame_fields.get("last_frame") == "image_urls"
        and any(frame.get("frame_type") == "last_frame" for frame in frames)
        and not any(
            frame.get("frame_type", "first_frame") == "first_frame" for frame in frames
        )
    ):
        raise failure(400, "A last frame requires a first frame for this model")
    frames = sorted(frames, key=lambda frame: frame.get("frame_type") == "last_frame")
    for frame in frames:
        frame_type = frame.get("frame_type", "first_frame")
        field = frame_fields.get(frame_type)
        if not field:
            raise failure(400, "Frame control is unsupported for this model")
        url = media_url(frame)
        if field == "image_urls":
            inputs.setdefault(field, []).append(url)
        else:
            inputs[field] = url
    for ref in refs:
        kind = str(ref.get("type", "image_url")).removesuffix("_url")
        field = rule.get("references", {}).get(kind)
        if not field:
            raise failure(400, "Reference type is unsupported for this model")
        inputs.setdefault(field, []).append(media_url(ref))
    for key, value in p.items():
        if value is None or key in TRANSPORT_PARAMS:
            continue
        target = rule.get("mapping", {}).get(key, key)
        if target not in rule["allowed"]:
            raise failure(400, f"Unsupported Kie parameter: {key}")
        if key == "resolution" and rule.get("resolution_modes"):
            value = rule["resolution_modes"].get(value, "unsupported")
        inputs[target] = value
    if "duration" in inputs:
        raw = inputs["duration"]
        try:
            duration = int(raw)
            if isinstance(raw, bool) or float(raw) != duration:
                raise ValueError()
        except (TypeError, ValueError):
            raise failure(400, "Duration must be an integer") from None
        low, high = rule["duration_range"]
        if not low <= duration <= high and not (
            duration == -1 and rule.get("auto_duration")
        ):
            raise failure(400, "Duration is outside the model range")
        inputs["duration"] = str(duration) if rule.get("string_duration") else duration
    enums = {**rule.get("enums", {}), **(rule.get("edit_enums", {}) if edit else {})}
    for field, enum in enums.items():
        if field in inputs and inputs[field] not in enum:
            raise failure(400, f"Unsupported {field}")
    for field, limit in rule.get("limits", {}).items():
        if len(inputs.get(field, [])) > limit:
            raise failure(400, f"Too many {field}")
    if (
        rule.get("exclusive_frames")
        and any(inputs.get(k) for k in frame_fields.values())
        and any(inputs.get(k) for k in rule.get("references", {}).values())
    ):
        raise failure(400, "Frames and references are mutually exclusive")
    if (
        rule.get("string_duration")
        and inputs.get("multi_shots")
        and len(inputs.get("image_urls", [])) > 1
    ):
        raise failure(400, "Multi-shot Kling only supports a first frame")
    if (
        rule.get("one_image_1080p")
        and inputs.get("resolution") == "1080p"
        and len(inputs.get("image_urls", [])) > 1
    ):
        raise failure(400, "1080p supports one reference image")
    if (
        rule.get("restricted_ratios")
        and inputs.get("resolution") != "1K"
        and inputs.get("aspect_ratio") in rule["restricted_ratios"]
    ):
        raise failure(400, "This aspect ratio only supports 1K")
    if edit and not inputs.get(rule["edit_field"]):
        raise failure(400, "An edit input image is required")
    return {
        "model": rule.get("edit_model") if edit else rule["create_model"],
        "input": inputs,
    }
