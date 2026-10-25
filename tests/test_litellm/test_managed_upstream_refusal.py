"""Offline-only terminal refusal contract for managed chat and Messages routing.

Mocks replace provider calls; sockets are disabled by the invocation. Retry
policy, ordinary/content-policy fallback and stream conversion are exercised.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import litellm
from litellm import Router
from litellm.litellm_core_utils.upstream_refusal import mapped_refusal, refusal_code
from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import LiteLLMAnthropicMessagesAdapter
from litellm.types.router import RetryPolicy
from litellm.types.utils import ModelResponse, ModelResponseStream


def upstream_error(code, status=403, usage=None):
    """Build a real SDK HTTP envelope; never rely on error prose for classification."""
    body = {"error": {"code": code, "type": code}}
    if usage is not None:
        body["usage"] = usage
    response = httpx.Response(status, json=body, request=httpx.Request("POST", "https://mock.invalid/v1/messages"))
    return httpx.HTTPStatusError("mock upstream", request=response.request, response=response)


@pytest.mark.parametrize("code,status", [("upstream_model_refusal", 400), ("upstream_model_refusal", 503), ("model_denylisted", 403)])
@pytest.mark.parametrize("entry", ["chat", "messages"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_refusal_never_retries_or_falls_back(code, status, entry, stream):
    """Both managed entries make one provider call despite explicit retry overrides."""
    error = mapped_refusal(upstream_error(code, status, {"input_tokens": 9, "output_tokens": 2}), "mock", "anthropic")
    upstream = AsyncMock(side_effect=error)
    with patch.object(litellm, "anthropic_messages", upstream):
        router = Router(model_list=[{"model_name": name, "litellm_params": {"model": "anthropic/claude-haiku-4-5", "api_key": "offline"}} for name in ("primary", "backup")],
                        num_retries=3, fallbacks=[{"primary": ["backup"]}],
                        content_policy_fallbacks=[{"primary": ["backup"]}], retry_policy=RetryPolicy(BadRequestErrorRetries=3))
    with patch.object(router, "_acompletion", upstream):
        call = router.acompletion if entry == "chat" else router.aanthropic_messages
        with pytest.raises(litellm.BadRequestError) as caught:
            await call(model="primary", messages=[{"role": "user", "content": "京都旅行怎么安排？"}], max_tokens=20, stream=stream)
    assert upstream.await_count == 1
    assert caught.value.code == code
    assert caught.value.status_code == status
    assert caught.value.provider_specific_fields["usage"] == {"input_tokens": 9, "output_tokens": 2}


@pytest.mark.parametrize("stream", [False, True])
def test_refusal_reason_roundtrip(stream):
    """OpenAI content_filter preserves native refusal and usage on both bridges."""
    adapter = LiteLLMAnthropicMessagesAdapter()
    if stream:
        response = ModelResponseStream(model="mock", choices=[{"index": 0, "delta": {}, "finish_reason": "content_filter"}], usage={"prompt_tokens": 11, "completion_tokens": 3})
        result = adapter.translate_streaming_openai_response_to_anthropic(response, 0)
        assert result["delta"]["stop_reason"] == "refusal"
        assert result["usage"]["output_tokens"] == 3
    else:
        response = ModelResponse(model="mock", choices=[{"index": 0, "message": {"role": "assistant", "content": "Cannot comply"}, "finish_reason": "content_filter"}], usage={"prompt_tokens": 11, "completion_tokens": 3})
        result = adapter.translate_openai_response_to_anthropic(response)
        assert result["stop_reason"] == "refusal"
        assert result["usage"]["input_tokens"] == 11
        router = Router(fallbacks=[{"*": ["backup"]}])
        assert router._should_raise_content_policy_error("primary", response, {}) is True
        response._hidden_params["upstream_stop_reason"] = "refusal"
        assert router._should_raise_content_policy_error("primary", response, {}) is False


def test_refusal_field_and_json_only_classification():
    """Refusal text is retained; error words or an ordinary auth 403 do not classify."""
    adapter = LiteLLMAnthropicMessagesAdapter()
    response = ModelResponse(model="mock", choices=[{"index": 0, "message": {"role": "assistant", "content": None, "refusal": "Cannot comply"}, "finish_reason": "stop"}], usage={"prompt_tokens": 9, "completion_tokens": 2})
    result = adapter.translate_openai_response_to_anthropic(response)
    assert result["stop_reason"] == "refusal"
    assert result["content"][0]["text"] == "Cannot comply"
    assert refusal_code(upstream_error("authentication_error")) is None
    assert refusal_code(upstream_error("model_denylisted", 500)) is None
    assert refusal_code(Exception("upstream_model_refusal")) is None
    mapped = mapped_refusal(upstream_error("upstream_model_refusal", 400), "mock", "anthropic")
    assert "usage" not in mapped.provider_specific_fields
    assert mapped_refusal(mapped, "mock", "anthropic") is mapped


@pytest.mark.parametrize("code,status", [("upstream_model_refusal", 503), ("model_denylisted", 403)])
def test_stream_error_skips_midstream_fallback(code, status):
    """The common sync/async stream error boundary cannot wrap a terminal error."""
    from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
    wrapper = SimpleNamespace(model="mock", custom_llm_provider="anthropic")
    with pytest.raises(litellm.BadRequestError) as caught:
        CustomStreamWrapper._handle_stream_fallback_error(wrapper, upstream_error(code, status))
    assert caught.value.code == code


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_refusal_delta_retains_final_reason(asynchronous):
    """Run actual sync/async stream bridge with refusal text then a normal stop."""
    from litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator import AnthropicStreamWrapper
    chunks = [ModelResponseStream(model="mock", choices=[{"index": 0, "delta": {"refusal": "Cannot comply"}, "finish_reason": None}]), ModelResponseStream(model="mock", choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}], usage={"prompt_tokens": 9, "completion_tokens": 2})]
    async def generator():
        """Yield only the fixed mock chunks, without upstream access."""
        for chunk in chunks:
            yield chunk
    wrapper = AnthropicStreamWrapper(generator() if asynchronous else iter(chunks), "mock")
    events = [event async for event in wrapper] if asynchronous else list(wrapper)
    finals = [e for e in events if e["type"] == "message_delta"]
    assert finals[-1]["delta"]["stop_reason"] == "refusal"


def test_proxy_error_wire_usage():
    """Stable refusal code and real usage survive JSON/SSE error serialization."""
    from litellm.proxy._types import ProxyException
    error = mapped_refusal(upstream_error("upstream_model_refusal", 503, {"input_tokens": 9}), "mock", "anthropic")
    proxy = ProxyException(message=error.message, type=error.type, param=None, code=error.status_code, openai_code=error.code, provider_specific_fields=error.provider_specific_fields)
    assert proxy.code == "503"
    assert proxy.to_dict()["code"] == "upstream_model_refusal"
    assert proxy.to_dict()["usage"] == {"input_tokens": 9}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_native_messages_refusal_is_returned_once(stream):
    """Native Anthropic 200 refusal returns original usage without any fallback."""
    body = {"id": "msg_mock", "type": "message", "role": "assistant", "model": "mock", "content": [], "stop_reason": "refusal", "usage": {"input_tokens": 9, "output_tokens": 2}}
    upstream = AsyncMock(return_value=body)
    with patch.object(litellm, "anthropic_messages", upstream):
        router = Router(model_list=[{"model_name": "primary", "litellm_params": {"model": "anthropic/claude-haiku-4-5", "api_key": "offline"}}], num_retries=3, fallbacks=[{"primary": ["backup"]}])
    result = await router.aanthropic_messages(model="primary", messages=[{"role": "user", "content": "京都旅行怎么安排？"}], max_tokens=20, stream=stream)
    assert result["stop_reason"] == "refusal"
    assert result["usage"] == body["usage"]
    assert upstream.await_count == 1


def test_explicit_none_provider_fields():
    """An SDK error with optional provider fields unset still maps safely."""
    error = upstream_error("upstream_model_refusal", 400)
    error.provider_specific_fields = None
    assert mapped_refusal(error, "mock", "anthropic").code == "upstream_model_refusal"
