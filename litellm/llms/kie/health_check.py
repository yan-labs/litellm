"""Free credential probe; never creates or uploads media."""

from contextvars import ContextVar
import httpx
from litellm.types.utils import ImageResponse
from litellm.types.videos.main import VideoObject
from .common_utils import API_BASE, auth, envelope, failure

# ahealth_check -> get_mode_handlers forwards max_tokens unchanged; image prompt
# may be None and video defaults to "test video generation". Public parameters
# cannot uniquely identify probes. Only ahealth_check sets this internal context,
# propagated through async calls / copied worker contexts and reset in finally.
# Ordinary requests with max_tokens retain strict validation.
health_check_context: ContextVar[bool] = ContextVar("kie_health_check", default=False)


def probe_response(model, video=False):
    if video:
        return VideoObject(
            id="kie-health-check", object="video", model=model, status="completed"
        )
    response = ImageResponse(data=[{}])
    response.usage = None  # ImageResponse otherwise supplies default token usage.
    return response


def check_credit(api_key, model, client=None, video=False):
    if client is None:
        with httpx.Client() as owned:
            return check_credit(api_key, model, owned, video)
    try:
        envelope(
            client.get(
                url=f"{API_BASE}/chat/credit", headers=auth({}, api_key), timeout=30
            )
        )
    except httpx.TimeoutException:
        raise failure(408, "Kie credit probe timed out") from None
    except httpx.RequestError:
        raise failure(502, "Kie credit probe network error") from None
    return probe_response(model, video)


async def acheck_credit(api_key, model, client=None, video=False):
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await acheck_credit(api_key, model, owned, video)
    try:
        envelope(
            await client.get(
                url=f"{API_BASE}/chat/credit", headers=auth({}, api_key), timeout=30
            )
        )
    except httpx.TimeoutException:
        raise failure(408, "Kie credit probe timed out") from None
    except httpx.RequestError:
        raise failure(502, "Kie credit probe network error") from None
    return probe_response(model, video)
