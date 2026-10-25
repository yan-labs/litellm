"""Free Wokey catalog probe for LiteLLM's internal media health checks.

Reuses Kie's health-check context marker so background probes never submit
billable image/video generation. No public request parameter enables this path.
"""

import httpx

from litellm.types.utils import ImageResponse
from litellm.types.videos.main import VideoObject

from .common import API_BASE, auth, error


def probe_response(model, video):
    """Report connectivity only, with no generated artifact or billable usage."""
    if video:
        result = VideoObject(id="wokey-health-check", object="video", model=model, status="completed")
    else:
        result = ImageResponse(data=[{}])
        result.usage = None
    result._hidden_params = {"additional_headers": {"llm_provider-x-litellm-response-cost": 0.0}}
    return result


def check_catalog(api_key, model, api_base=None, video=False):
    """Probe the selected deployment's free model catalog in synchronous health checks."""
    with httpx.Client(timeout=30) as client:
        response = client.get((api_base or API_BASE).rstrip("/") + "/models", params={"output_modalities": "video" if video else "image"}, headers=auth({}, api_key))
        if response.status_code >= 400:
            raise error(response.status_code, "Wokey catalog health check failed")
    return probe_response(model, video)


async def acheck_catalog(api_key, model, api_base=None, video=False):
    """Probe catalogs asynchronously without consuming media generation balance."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get((api_base or API_BASE).rstrip("/") + "/models", params={"output_modalities": "video" if video else "image"}, headers=auth({}, api_key))
        if response.status_code >= 400:
            raise error(response.status_code, "Wokey catalog health check failed")
    return probe_response(model, video)
