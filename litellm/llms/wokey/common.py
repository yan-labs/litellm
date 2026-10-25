"""Shared Wokey media auth, response billing and submission uncertainty.

The image/video configurations use these helpers inside LiteLLM's HTTP layer;
credentials come only from the deployment's named credential, never client defaults.
"""

import math

import litellm

API_BASE = "https://api.wokey.ai/v1"


def auth(headers, api_key):
    """Authenticate one managed deployment without borrowing another provider's key."""
    if not api_key:
        raise error(401, "Wokey named credential is required")
    return {**headers, "Authorization": f"Bearer {api_key}"}


def error(status, message):
    """Use the shared HTTP error contract; raw upstream bodies may contain secrets."""
    options = {"message": message, "model": "", "llm_provider": "wokey"}
    if status in (400, 422):
        exc = litellm.BadRequestError(**options)
        exc.status_code = status
        return exc
    if status == 401:
        return litellm.AuthenticationError(**options)
    if status == 429:
        return litellm.RateLimitError(**options)
    if status == 408:
        return litellm.Timeout(**options)
    return litellm.APIError(status_code=status, **options)


def payload(response):
    """Parse a successful object, failing safely without echoing upstream content."""
    if response.status_code >= 400:
        raise error(response.status_code, "Wokey media request rejected")
    try:
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError()
        return value
    except (ValueError, TypeError):
        raise error(502, "Wokey returned invalid media JSON") from None


def usage(data, response=None):
    """Expose actual settled USD only; model-list rates are never a billing receipt."""
    raw_usage = data.get("usage") or {}
    candidates = [data.get("price_usd"), raw_usage.get("cost_usd"), raw_usage.get("cost")]
    if response is not None:
        candidates.append(response.headers.get("x-litellm-response-cost"))
    result = {"provider": "wokey", "cost_authoritative": False, "billing_pending": True}
    for raw in candidates:
        if raw is None or isinstance(raw, bool):
            continue
        try:
            cost = float(raw)
        except (ValueError, TypeError):
            continue
        if math.isfinite(cost) and cost >= 0:
            result.update(cost=cost, cost_usd=cost, cost_authoritative=True, billing_pending=False)
            break
    return result


def uncertain_submission():
    """Block retries/fallback when a video POST may already have reserved balance."""
    exc = litellm.Timeout(message="Wokey video submission outcome unknown; reconcile with the original Idempotency-Key before resubmitting", model="", llm_provider="wokey")
    exc.media_no_resubmit = True
    exc.num_retries = 0
    return exc


def is_wokey_job(video_id):
    """Recognize accepted jobs so router fallback cannot move their reads to Kie."""
    from litellm.types.videos.utils import decode_video_id_with_provider
    if not isinstance(video_id, str):
        return False
    return decode_video_id_with_provider(video_id).get("custom_llm_provider") == "wokey"


def image_file(image):
    """Reuse OpenAI file inputs, fetching reference URLs without provider auth.

    Only one reference is forwarded until live Wokey multi-image limits are
    verified. Callers reject larger sets before fetching or submitting.
    """
    import base64

    import httpx

    from litellm.images.utils import ImageEditRequestUtils

    if isinstance(image, str) and image.startswith(("https://", "http://")):
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            result = client.get(image)
            result.raise_for_status()
            image = result.content
    elif isinstance(image, str) and image.startswith("data:"):
        try:
            image = base64.b64decode(image.split(",", 1)[1], validate=True)
        except (ValueError, IndexError):
            raise error(400, "Invalid reference image data") from None
    if isinstance(image, tuple):
        return image
    if hasattr(image, "read"):
        position = image.tell()
        image.seek(0)
        value = image.read()
        image.seek(position)
        image = value
    if not isinstance(image, bytes) or not image:
        raise error(400, "Unsupported reference image input")
    return ("reference.png", image, ImageEditRequestUtils.get_image_content_type(image))
