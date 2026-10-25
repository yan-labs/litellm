"""
A Gemini candidate that carries only a finishReason (no `content` key) must still
produce a choice.

Reasoning models spend the whole maxOutputTokens budget on thinking when it is tiny
(e.g. max_tokens=16), and Vertex then answers with
`{"candidates": [{"finishReason": "MAX_TOKENS", "index": 0}]}`. The non-streaming
transform skipped such candidates, left `ModelResponse.choices` empty and every
caller that reads `choices[0]` (notably the Anthropic /v1/messages adapter) failed
with `IndexError: list index out of range`, surfacing as an HTTP 500.
"""

import os
import sys
from unittest.mock import MagicMock

import httpx

sys.path.insert(0, os.path.abspath("../../../../.."))

from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
    LiteLLMAnthropicMessagesAdapter,
)
from litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini import (
    VertexGeminiConfig,
)
from litellm.types.utils import ModelResponse

USAGE = {
    "promptTokenCount": 5,
    "candidatesTokenCount": 0,
    "totalTokenCount": 21,
    "thoughtsTokenCount": 16,
}


def _transform(completion_response: dict) -> ModelResponse:
    logging_obj = MagicMock()
    logging_obj.optional_params = {}
    return VertexGeminiConfig()._transform_google_generate_content_to_openai_model_response(
        completion_response=completion_response,
        model_response=ModelResponse(),
        model="gemini-3.8-flash",
        logging_obj=logging_obj,
        raw_response=httpx.Response(200),
    )


def test_candidate_with_only_finish_reason_yields_a_choice():
    response = _transform(
        {
            "candidates": [{"finishReason": "MAX_TOKENS", "index": 0}],
            "usageMetadata": USAGE,
        }
    )

    assert len(response.choices) == 1
    assert response.choices[0].finish_reason == "length"
    assert response.choices[0].message.content is None


def test_response_without_candidates_yields_an_empty_choice():
    response = _transform({"usageMetadata": USAGE})

    assert len(response.choices) == 1
    assert response.choices[0].finish_reason == "stop"
    assert response.choices[0].message.content is None


def test_anthropic_adapter_translates_content_less_candidate():
    response = _transform(
        {
            "candidates": [{"finishReason": "MAX_TOKENS", "index": 0}],
            "usageMetadata": USAGE,
        }
    )

    anthropic_response = LiteLLMAnthropicMessagesAdapter().translate_openai_response_to_anthropic(
        response=response
    )

    assert anthropic_response["stop_reason"] == "max_tokens"
    assert anthropic_response["content"] == []
