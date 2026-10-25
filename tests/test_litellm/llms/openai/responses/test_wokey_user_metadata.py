"""Offline Wokey Responses contract used by native, chat and messages adapters.

Wokey rejects the deprecated user field on Responses while accepting metadata;
these checks retain attribution without changing other providers or request inputs.
"""
from copy import deepcopy

import pytest

from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig
from litellm.types.router import GenericLiteLLMParams


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("as_dict", [False, True])
def test_wokey_user_is_retained_as_metadata(stream, as_dict):
    """Both HTTP handlers use this builder and must retain tools and attribution."""
    params = {"user": "test-user", "metadata": {"trace": "test"}, "stream": stream,
              "tools": [{"type": "function", "name": "ping", "parameters": {"type": "object"}}],
              "reasoning": {"effort": "low"}, "max_output_tokens": 32}
    before = deepcopy(params)
    route = {"api_base": "https://api.wokey.ai/v1"}
    result = OpenAIResponsesAPIConfig().transform_responses_api_request(
        model="responses/gpt-6.1-sol", input="Reply OK.",
        response_api_optional_request_params=params,
        litellm_params=route if as_dict else GenericLiteLLMParams(**route), headers={},
    )
    assert "user" not in result
    assert result["metadata"] == {"trace": "test", "user_id": "test-user"}
    assert result["tools"] == before["tools"]
    assert result["reasoning"] == before["reasoning"]
    assert result["stream"] is stream
    assert params == before


@pytest.mark.parametrize("base", ["https://api.kie.ai/codex/v1", "https://api.openai.com/v1",
                                  "https://api.wokey.ai.example.com/v1", None])
def test_other_endpoints_keep_user(base):
    """Only the exact Wokey host receives its documented compatibility mapping."""
    result = OpenAIResponsesAPIConfig().transform_responses_api_request(
        model="gpt-6.1-sol", input="OK", response_api_optional_request_params={"user": "test-user"},
        litellm_params=GenericLiteLLMParams(api_base=base), headers={},
    )
    assert result["user"] == "test-user"


def test_wokey_explicit_metadata_wins_and_absent_user_is_unchanged():
    """Keep client attribution authoritative and do not invent absent identifiers."""
    config = OpenAIResponsesAPIConfig()
    for params, expected in [({"user": "legacy", "metadata": {"user_id": "explicit"}}, {"user_id": "explicit"}),
                             ({"metadata": {"trace": "test"}}, {"trace": "test"})]:
        before = deepcopy(params)
        result = config.transform_responses_api_request(
            model="gpt-6.1-sol", input="OK", response_api_optional_request_params=params,
            litellm_params=GenericLiteLLMParams(api_base="https://api.wokey.ai/v1"), headers={},
        )
        assert result["metadata"] == expected
        assert "user" not in result
        assert params == before
