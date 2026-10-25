"""Offline check for the TEST managed Vertex Interactions contract.

Run directly with Python; mocks only prove routing/shape, never provider access.
"""
import asyncio
from unittest.mock import MagicMock, patch
import httpx
from litellm.llms.vertex_ai.interactions.transformation import VertexAIInteractionsConfig
from litellm.router import Router
from litellm.types.interactions import InteractionsAPIResponse


def check():
    """Prove Cloud URL/auth, mixed usage and routed polling reuse named credentials."""
    config = VertexAIInteractionsConfig()
    params = {"vertex_project": "test-project", "vertex_credentials": "fixture-only"}
    with patch.object(config, "get_access_token", return_value=("fixture-token", "test-project")):
        assert config.validate_environment({}, "gemini-omni", params)["Authorization"] == "Bearer fixture-token"
        assert config.get_complete_url(None, None, litellm_params=params).endswith("/projects/test-project/locations/global/interactions")
        assert config.transform_get_interaction_request("id/1", "", params, {})[0].endswith("/id%2F1")
    body = config.transform_request("vertex_ai/gemini-omni-1.1-flash-preview", None,
        [{"type": "text", "text": "A ball rolls"}],
        {"response_format": [{"type": "video", "resolution": "360p", "duration": "3s"}], "background": True}, params, {})
    assert body["model"] == "gemini-omni-1.1-flash-preview"
    assert body["response_format"][0]["duration"] == "3s"
    raw = {"id": "i1", "status": "completed", "steps": [{"type": "model_output", "content": [{"type": "video", "data": "fixture"}]}],
        "usage": {"total_input_tokens": 20, "output_tokens_by_modality": [{"modality": "video", "tokens": 5793}], "total_thought_tokens": 10}}
    response = config.transform_get_interaction_response(httpx.Response(200, json=raw), MagicMock())
    assert response.outputs[0]["data"] == "fixture"
    assert response.usage == raw["usage"]

    async def poll():
        router = Router(model_list=[{"model_name": "gemini-omni", "litellm_params": {"model": "vertex_ai/gemini-omni-1.1-flash-preview", "custom_llm_provider": "vertex_ai", "vertex_project": "test-project"}}])
        captured = {}
        async def provider(**kwargs):
            captured.update(kwargs)
            return InteractionsAPIResponse(id="i1", status="in_progress")
        await router._init_interactions_api_endpoints(provider, custom_llm_provider="vertex_ai", model="gemini-omni", interaction_id="i1")
        assert captured["vertex_project"] == "test-project"
        assert captured["custom_llm_provider"] == "vertex_ai"
        assert captured["interaction_id"] == "i1"
    asyncio.run(poll())


if __name__ == "__main__":
    check()
    print("Vertex Omni Interactions offline check passed")
