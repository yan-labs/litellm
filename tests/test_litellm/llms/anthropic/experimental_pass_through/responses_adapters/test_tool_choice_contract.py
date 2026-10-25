"""Offline tool-choice contracts for the shared messages request adapters.

Both streaming and non-streaming handlers consume these builders; no model is called.
"""

from copy import deepcopy

import pytest

from litellm.llms.anthropic.experimental_pass_through.adapters.handler import (
    LiteLLMMessagesToCompletionTransformationHandler,
)
from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
    LiteLLMAnthropicMessagesAdapter,
)
from litellm.llms.anthropic.experimental_pass_through.responses_adapters.handler import (
    _build_responses_kwargs,
)
from litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation import (
    LiteLLMAnthropicToResponsesAPIAdapter,
)

CHOICES = [
    ({"type": "auto"}, "auto", "auto"),
    ({"type": "any"}, "required", "required"),
    ({"type": "tool", "name": "ping"},
     {"type": "function", "name": "ping"},
     {"type": "function", "function": {"name": "ping"}}),
    ({"type": "none"}, "none", "none"),
]


@pytest.mark.parametrize("choice,responses,chat", CHOICES)
def test_direct_tool_choice(choice, responses, chat):
    """Check the scalar/object contract at both reusable conversion helpers."""
    assert LiteLLMAnthropicToResponsesAPIAdapter.translate_tool_choice_to_responses_api(choice) == responses
    assert LiteLLMAnthropicMessagesAdapter().translate_anthropic_tool_choice_to_openai(choice) == chat


@pytest.mark.parametrize("route", ["responses", "chat"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("disable_parallel", [None, False, True])
@pytest.mark.parametrize("choice,responses,chat", CHOICES)
def test_messages_tool_choice_request(route, stream, disable_parallel, choice, responses, chat):
    """Check real handler builders preserve choice and parallel settings without mutating input."""
    choice = deepcopy(choice)
    if disable_parallel is not None:
        choice["disable_parallel_tool_use"] = disable_parallel
    request = {
        "model": "gpt-4o",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "Call ping with value OK."}],
        "tools": [{"name": "ping", "input_schema": {"type": "object", "properties": {}}}],
        "tool_choice": choice,
        "stream": stream,
    }
    original = deepcopy(request)
    if route == "responses":
        result = _build_responses_kwargs(**request)
        expected = responses
    else:
        result, _ = LiteLLMMessagesToCompletionTransformationHandler._prepare_completion_kwargs(**request)
        expected = chat
    assert result["tool_choice"] == expected
    assert bool(result.get("stream", False)) == stream
    if disable_parallel is None:
        assert "parallel_tool_calls" not in result
    else:
        assert result["parallel_tool_calls"] is (not disable_parallel)
    assert request == original
