"""Contract tests for the managed OpenRouter video adapter.

The Tokyo gateway exposes a stable Kollab video alias while the adapter keeps
the OpenRouter request, polling, and raw-content details inside LiteLLM.
These tests lock that boundary without making paid upstream requests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import yaml

sys.path.insert(0, os.path.abspath("../../../../.."))

from litellm.llms.custom_httpx.http_handler import HTTPHandler
from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
from litellm.llms.openrouter.common_utils import OpenRouterException
from litellm.llms.openrouter.videos.transformation import OpenRouterVideoConfig
from litellm.types.router import GenericLiteLLMParams
from litellm.types.utils import LlmProviders
from litellm.types.videos.utils import decode_video_id_with_provider
from litellm.utils import ProviderConfigManager


MODEL = "bytedance/seedance-2.0-fast"


def _response(status_code: int, payload: dict) -> httpx.Response:
    """Create a response with a request so error conversion has HTTP context."""

    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("GET", "https://openrouter.ai/api/v1/videos"),
    )


def test_openrouter_video_config_is_registered() -> None:
    """The video entrypoint resolves the managed OpenRouter provider adapter."""

    config = ProviderConfigManager.get_provider_video_config(
        model=MODEL,
        provider=LlmProviders.OPENROUTER,
    )

    assert isinstance(config, OpenRouterVideoConfig)


@pytest.mark.parametrize("environment", ["test", "prod"])
def test_seedance_alias_is_a_replayable_managed_video_route(environment: str) -> None:
    """Both Tokyo configs map the public Seedance alias to the managed adapter."""

    repository_root = Path(__file__).resolve().parents[5]
    config_path = repository_root / "config" / f"litellm.{environment}.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    routes = {route["model_name"]: route for route in config["model_list"]}

    seedance = routes["seedance-2"]
    # PROD 图片视频别名待 Kollab 新版发布后再切 kie，届时把 PROD 期望并入 TEST 期望。
    provider = "kie" if environment == "test" else "openrouter"
    upstream = "bytedance/seedance-2-fast" if environment == "test" else "bytedance/seedance-2.0-fast"
    assert seedance["litellm_params"]["model"] == f"{provider}/{upstream}"
    assert seedance["litellm_params"]["custom_llm_provider"] == provider
    assert seedance["litellm_params"]["litellm_credential_name"] == ("kie-ai" if environment == "test" else "openrouter-toyo")
    assert seedance["model_info"]["mode"] == "video_generation"


def test_create_request_keeps_kollab_image_guidance_contract() -> None:
    """The stable gateway request becomes OpenRouter's documented video payload."""

    config = OpenRouterVideoConfig()
    frame_images = [
        {
            "type": "image_url",
            "image_url": {"url": "https://signed.example/first.png"},
            "frame_type": "first_frame",
        }
    ]
    input_references = [
        {
            "type": "image_url",
            "image_url": {"url": "https://signed.example/reference.png"},
        }
    ]

    body, files, url = config.transform_video_create_request(
        model=MODEL,
        prompt="A quiet paper boat floats through a moonlit canal.",
        api_base="https://openrouter.ai/api/v1",
        video_create_optional_request_params={
            "seconds": 6,
            "size": "720x1280",
            "frame_images": frame_images,
            "input_references": input_references,
            "extra_headers": {"x-ignored": "true"},
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert url == "https://openrouter.ai/api/v1/videos"
    assert files == []
    assert body == {
        "model": MODEL,
        "prompt": "A quiet paper boat floats through a moonlit canal.",
        "duration": 6,
        "size": "720x1280",
        "frame_images": frame_images,
        "input_references": input_references,
    }


def test_http_handler_keeps_the_video_request_inside_openrouter() -> None:
    """The shared HTTP layer uses the managed provider URL and credential only."""

    client = MagicMock(spec=HTTPHandler)
    client.post.return_value = _response(
        202,
        {"id": "or-job-123", "status": "queued"},
    )

    video = BaseLLMHTTPHandler().video_generation_handler(
        model=MODEL,
        prompt="A paper boat floats through a quiet canal.",
        video_generation_provider_config=OpenRouterVideoConfig(),
        video_generation_optional_request_params={"seconds": 4, "size": "720x1280"},
        custom_llm_provider="openrouter",
        litellm_params=GenericLiteLLMParams(
            api_base="https://openrouter.ai/api/v1",
            api_key="managed-test-key",
        ),
        logging_obj=MagicMock(),
        timeout=5,
        client=client,
    )

    assert video.status == "queued"
    client.post.assert_called_once_with(
        url="https://openrouter.ai/api/v1/videos",
        headers={
            "Authorization": "Bearer managed-test-key",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "prompt": "A paper boat floats through a quiet canal.",
            "duration": 4,
            "size": "720x1280",
        },
        timeout=5,
    )


def test_create_response_encodes_provider_and_model_for_followup_calls() -> None:
    """The durable job stores a gateway-safe id that retains route context."""

    video = OpenRouterVideoConfig().transform_video_create_response(
        model=MODEL,
        raw_response=_response(202, {"id": "or-job-123", "status": "queued"}),
        logging_obj=MagicMock(),
        custom_llm_provider="openrouter",
    )

    decoded = decode_video_id_with_provider(video.id)
    assert video.status == "queued"
    assert decoded["custom_llm_provider"] == "openrouter"
    assert decoded["model_id"] == MODEL
    assert decoded["video_id"] == "or-job-123"


def test_status_response_preserves_terminal_state_and_error() -> None:
    """OpenRouter poll objects stay readable by Kollab's shared job machinery."""

    video = OpenRouterVideoConfig().transform_video_status_retrieve_response(
        raw_response=_response(
            200,
            {
                "id": "or-job-123",
                "status": "completed",
                "progress": 100,
                "error": None,
            },
        ),
        logging_obj=MagicMock(),
        custom_llm_provider="openrouter",
    )

    assert video.status == "completed"
    assert video.progress == 100
    assert decode_video_id_with_provider(video.id)["video_id"] == "or-job-123"


def test_content_request_uses_openrouters_managed_content_stream() -> None:
    """Content download stays on OpenRouter's authenticated content endpoint."""

    config = OpenRouterVideoConfig()
    encoded_id = "video_b3BhcXVl"
    url, params = config.transform_video_content_request(
        video_id=encoded_id,
        api_base="https://openrouter.ai/api/v1",
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert url == "https://openrouter.ai/api/v1/videos/video_b3BhcXVl/content"
    assert params == {}
    assert config.transform_video_content_response(
        raw_response=httpx.Response(
            200,
            content=b"managed-video-bytes",
            request=httpx.Request(
                "GET", "https://openrouter.ai/api/v1/videos/video_b3BhcXVl/content"
            ),
        ),
        logging_obj=MagicMock(),
    ) == b"managed-video-bytes"


def test_upstream_http_failure_keeps_its_status_code() -> None:
    """Gateway callers receive the actual upstream rejection instead of a generic 500."""

    with pytest.raises(OpenRouterException) as raised:
        OpenRouterVideoConfig().transform_video_create_response(
            model=MODEL,
            raw_response=_response(401, {"error": {"message": "not authorized"}}),
            logging_obj=MagicMock(),
            custom_llm_provider="openrouter",
        )

    assert raised.value.status_code == 401
