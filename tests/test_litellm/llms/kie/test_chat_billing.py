"""Offline billing contract for the seven managed Kie conversation aliases."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
import litellm
from litellm.completion_extras.litellm_responses_transformation.transformation import (
    LiteLLMResponsesTransformationHandler,
)
from litellm.llms.anthropic.chat.transformation import AnthropicConfig
from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
    LiteLLMAnthropicMessagesAdapter,
)
from litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation import (
    LiteLLMAnthropicToResponsesAPIAdapter,
)
from litellm.llms.anthropic.experimental_pass_through.responses_adapters.streaming_iterator import (
    AnthropicResponsesStreamWrapper,
)
from litellm.types.llms.openai import ResponseAPIUsage, ResponsesAPIResponse
from litellm.types.utils import ModelResponse

ROOT = Path(__file__).resolve().parents[4]
# Independent expected USD/M: input, output, cache read, 5m write, 1h write.
RATES = {
    "claude-opus-5-5": (4, 20, 0.2, 5, 8),
    "claude-sonnet-5-5": (2, 10, 0.2, 2.5, 4),
    "gpt-6.1-sol": (2, 10, 0.1, 2.5, 2.5),
    "gpt-6-luna": (0.1, 0.5, 0.01, 0.125, 0.125),
    "gpt-6-astra": (10, 50, 1, 12.5, 12.5),
    "gpt-5.6-terra": (2, 12, 0.2, 2.5, 2.5),
    "grok-4.7": (1.6, 4.8, 0.4, 0, 0),
}


def response(
    input_tokens=1000, output_tokens=100, cached_tokens=600, reasoning_tokens=40
):
    return ResponsesAPIResponse.model_construct(
        id="resp-offline",
        created_at=0,
        object="response",
        model="gpt-6-luna",
        status="completed",
        error=None,
        incomplete_details=None,
        output=[
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}],
            }
        ],
        usage=ResponseAPIUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            input_tokens_details={"cached_tokens": cached_tokens},
            output_tokens_details={"reasoning_tokens": reasoning_tokens},
        ),
    )


def chat_response(raw):
    return LiteLLMResponsesTransformationHandler().transform_response(
        model=raw.model,
        raw_response=raw,
        model_response=ModelResponse(),
        logging_obj=Mock(),
        request_data={},
        messages=[],
        optional_params={},
        litellm_params={},
        encoding=None,
    )


@pytest.mark.parametrize("environment", ["test", "prod"])
@pytest.mark.parametrize("alias", RATES)
def test_should_bill_seven_aliases_at_existing_user_rates(
    environment, alias, monkeypatch
):
    rows = yaml.safe_load(
        (ROOT / "config" / f"litellm.{environment}.yaml").read_text()
    )["model_list"]
    row = next(r for r in rows if r["model_name"] == alias)
    info = row["model_info"]
    # register_model mirrors the router's custom model_info cost registration;
    # restore global price state at test exit.
    monkeypatch.setattr(litellm, "model_cost", dict(litellm.model_cost))
    model = row["litellm_params"]["model"]
    provider = row["litellm_params"]["custom_llm_provider"]
    litellm.register_model({model: {**info, "litellm_provider": provider}})
    inp, out, read, write5, write1 = RATES[alias]
    if alias.startswith("claude"):
        usage = AnthropicConfig().calculate_usage(
            {
                "input_tokens": 400,
                "output_tokens": 100,
                "cache_read_input_tokens": 600,
                "cache_creation_input_tokens": 300,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 200,
                    "ephemeral_1h_input_tokens": 100,
                },
            },
            reasoning_content=None,
        )
        result = ModelResponse(model=model, usage=usage)
        expected = (
            400 * inp + 100 * out + 600 * read + 200 * write5 + 100 * write1
        ) / 1e6
    else:
        result = chat_response(response())
        assert result.usage.prompt_tokens_details.cached_tokens == 600
        assert result.usage.completion_tokens_details.reasoning_tokens == 40
        # reasoning is already INCLUDED in output=100; never add 40 again.
        expected = (400 * inp + 100 * out + 600 * read) / 1e6
    cost = litellm.completion_cost(
        completion_response=result, model=model, custom_llm_provider=provider
    )
    assert cost > 0
    assert cost == pytest.approx(expected)
    if not alias.startswith("claude"):
        # Direct /messages converts to disjoint buckets; reconstituting chat usage
        # for logging must yield the very same cost and inclusive token total.
        translated = LiteLLMAnthropicToResponsesAPIAdapter().translate_response(
            response()
        )
        usage = AnthropicConfig().calculate_usage(
            translated["usage"], reasoning_content=None
        )
        assert usage.prompt_tokens == 1000
        assert litellm.completion_cost(
            completion_response=ModelResponse(model=model, usage=usage),
            model=model,
            custom_llm_provider=provider,
        ) == pytest.approx(expected)


@pytest.mark.parametrize("cached", [0, 600, 1000])
def test_should_preserve_cache_buckets_through_both_messages_paths(cached):
    raw = response(cached_tokens=cached)
    chat = chat_response(raw)
    assert chat.usage.prompt_tokens == 1000
    assert chat.usage.completion_tokens == 100
    assert chat.usage.prompt_tokens_details.cached_tokens == cached
    assert chat.usage.completion_tokens_details.reasoning_tokens == 40
    translated = LiteLLMAnthropicToResponsesAPIAdapter().translate_response(raw)
    assert translated["usage"]["input_tokens"] == 1000 - cached
    assert translated["usage"].get("cache_read_input_tokens", 0) == cached
    assert translated["usage"]["output_tokens"] == 100
    via_chat = LiteLLMAnthropicMessagesAdapter().translate_openai_response_to_anthropic(
        chat
    )
    assert via_chat["usage"] == translated["usage"]
    wrapper = AnthropicResponsesStreamWrapper(iter(()), raw.model)
    wrapper._process_event(SimpleNamespace(type="response.completed", response=raw))
    delta = next(c for c in wrapper._chunk_queue if c["type"] == "message_delta")
    assert delta["usage"] == translated["usage"]


def test_should_reconcile_luna_observed_main_call_without_double_counting_reasoning():
    # Cached=11776 is inferred from the observed cost; not a captured provider payload.
    raw = response(14625, 5, 11776, 0)
    translated = LiteLLMAnthropicToResponsesAPIAdapter().translate_response(raw)
    usage = translated["usage"]
    expected = (2849 * 0.1 + 11776 * 0.01 + 5 * 0.5) / 1e6
    assert (
        usage["input_tokens"] * 0.1
        + usage.get("cache_read_input_tokens", 0) * 0.01
        + usage["output_tokens"] * 0.5
    ) / 1e6 == pytest.approx(expected)
    assert expected == pytest.approx(0.00040516)
    assert (14625 * 0.1 + 5 * 0.5) / 1e6 == pytest.approx(0.001465)


@pytest.mark.parametrize("alias", ["claude-opus-5-5", "claude-sonnet-5-5"])
def test_should_use_five_minute_write_floor_when_ttl_is_missing(alias, monkeypatch):
    rows = yaml.safe_load((ROOT / "config/litellm.test.yaml").read_text())["model_list"]
    row = next(r for r in rows if r["model_name"] == alias)
    model = row["litellm_params"]["model"]
    monkeypatch.setattr(litellm, "model_cost", dict(litellm.model_cost))
    litellm.register_model(
        {model: {**row["model_info"], "litellm_provider": "anthropic"}}
    )
    usage = AnthropicConfig().calculate_usage(
        {
            "input_tokens": 400,
            "output_tokens": 100,
            "cache_read_input_tokens": 600,
            "cache_creation_input_tokens": 300,
        },
        reasoning_content=None,
    )
    inp, out, read, write, _ = RATES[alias]
    expected = (400 * inp + 100 * out + 600 * read + 300 * write) / 1e6
    assert litellm.completion_cost(
        completion_response=ModelResponse(model=model, usage=usage),
        model=model,
        custom_llm_provider="anthropic",
    ) == pytest.approx(expected)
