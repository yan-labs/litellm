"""Tests for the pre-headers timeout + retry in async_anthropic_messages_handler.

Locks the Kollab fork behavior added for the "Bedrock accepts the POST but never
returns response headers" hang: streaming requests bound the wait for response
headers with KOLLAB_LITELLM_ANTHROPIC_HEADERS_TIMEOUT_SECONDS (default 240s),
retry once in-handler (re-signing the request), and surface litellm.Timeout via
the provider error class when every attempt stalls. Non-streaming requests and
knob<=0 keep the legacy plain-await path.

These tests call the real handler with a real AsyncHTTPHandler whose .post is
replaced, so the wait_for wiring, the retry loop, and the exception conversion
are all exercised without any network.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import litellm
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
from litellm.types.router import GenericLiteLLMParams

HEADERS_TIMEOUT_ENV = "KOLLAB_LITELLM_ANTHROPIC_HEADERS_TIMEOUT_SECONDS"


def _provider_config_mock() -> MagicMock:
    """Minimal BaseAnthropicMessagesConfig stand-in for the handler's call sequence."""
    config = MagicMock()
    config.max_retry_on_anthropic_messages_http_error = 2
    config.validate_anthropic_messages_environment.return_value = (
        {},
        "https://bedrock.example.invalid",
    )
    config.transform_anthropic_messages_request.return_value = {
        "model": "stub-model",
        "messages": [{"role": "user", "content": "hi"}],
    }
    config.get_complete_url.return_value = (
        "https://bedrock.example.invalid/model/stub/invoke-with-response-stream"
    )
    config.sign_request.return_value = ({}, b"{}")
    config.get_async_streaming_response_iterator.return_value = object()
    config.get_error_class.side_effect = (
        lambda error_message, status_code, headers: BaseLLMException(
            status_code=status_code, message=error_message, headers=headers
        )
    )
    return config


def _logging_obj_mock() -> MagicMock:
    logging_obj = MagicMock()
    logging_obj.model_call_details = {}
    return logging_obj


def _handler_with_hooks_stubbed() -> BaseLLMHTTPHandler:
    handler = BaseLLMHTTPHandler()
    handler._call_agentic_completion_hooks = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return handler


def _response_mock() -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    return response


async def _invoke(handler, provider_config, client, stream):
    return await handler.async_anthropic_messages_handler(
        model="stub-model",
        messages=[{"role": "user", "content": "hi"}],
        anthropic_messages_provider_config=provider_config,
        anthropic_messages_optional_request_params={},
        custom_llm_provider="bedrock",
        litellm_params=GenericLiteLLMParams(),
        logging_obj=_logging_obj_mock(),
        client=client,
        stream=stream,
    )


@pytest.mark.asyncio
async def test_streaming_pre_headers_hang_retries_then_raises_timeout(monkeypatch):
    """Both attempts stall pre-headers: expect one in-handler retry with a fresh
    signature, then the provider error class wrapping litellm.Timeout (408)."""
    monkeypatch.setenv(HEADERS_TIMEOUT_ENV, "0.05")
    handler = _handler_with_hooks_stubbed()
    provider_config = _provider_config_mock()
    client = AsyncHTTPHandler()
    post_calls = {"n": 0}

    async def hanging_post(*args, **kwargs):
        post_calls["n"] += 1
        await asyncio.sleep(30)

    client.post = hanging_post  # type: ignore[method-assign]

    with pytest.raises(BaseLLMException) as exc_info:
        await _invoke(handler, provider_config, client, stream=True)

    assert post_calls["n"] == 2
    # initial sign + re-sign before the retry (SigV4 timestamps age while stalled)
    assert provider_config.sign_request.call_count == 2
    # litellm.Timeout carries 408; _handle_error must preserve it
    assert exc_info.value.status_code == 408
    assert "no response headers" in str(exc_info.value.message)


@pytest.mark.asyncio
async def test_streaming_hang_then_success_recovers_transparently(monkeypatch):
    """First attempt stalls, second returns headers: caller sees a normal stream."""
    monkeypatch.setenv(HEADERS_TIMEOUT_ENV, "0.05")
    handler = _handler_with_hooks_stubbed()
    provider_config = _provider_config_mock()
    client = AsyncHTTPHandler()
    post_calls = {"n": 0}
    response = _response_mock()

    async def flaky_post(*args, **kwargs):
        post_calls["n"] += 1
        if post_calls["n"] == 1:
            await asyncio.sleep(30)
        return response

    client.post = flaky_post  # type: ignore[method-assign]

    result = await _invoke(handler, provider_config, client, stream=True)

    assert post_calls["n"] == 2
    assert result is provider_config.get_async_streaming_response_iterator.return_value


@pytest.mark.asyncio
async def test_non_streaming_is_not_bounded_by_headers_budget(monkeypatch):
    """stream=False POSTs wait for the full body; the headers budget must not
    apply or long non-streaming completions would be falsely killed."""
    monkeypatch.setenv(HEADERS_TIMEOUT_ENV, "0.05")
    handler = _handler_with_hooks_stubbed()
    provider_config = _provider_config_mock()
    provider_config.transform_anthropic_messages_response.return_value = {"ok": True}
    client = AsyncHTTPHandler()
    response = _response_mock()

    async def slow_post(*args, **kwargs):
        await asyncio.sleep(0.2)  # well past the 0.05s knob
        return response

    client.post = slow_post  # type: ignore[method-assign]

    result = await _invoke(handler, provider_config, client, stream=False)

    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_knob_zero_disables_headers_budget(monkeypatch):
    """KOLLAB_LITELLM_ANTHROPIC_HEADERS_TIMEOUT_SECONDS=0 restores the legacy
    plain-await path (operational rollback without redeploy)."""
    monkeypatch.setenv(HEADERS_TIMEOUT_ENV, "0")
    handler = _handler_with_hooks_stubbed()
    provider_config = _provider_config_mock()
    client = AsyncHTTPHandler()
    response = _response_mock()

    async def slow_post(*args, **kwargs):
        await asyncio.sleep(0.2)
        return response

    client.post = slow_post  # type: ignore[method-assign]

    result = await _invoke(handler, provider_config, client, stream=True)

    assert result is provider_config.get_async_streaming_response_iterator.return_value


@pytest.mark.asyncio
async def test_converted_litellm_timeout_from_post_is_retried(monkeypatch):
    """AsyncHTTPHandler.post converts httpx timeouts to litellm.Timeout before the
    handler sees them; that shape must enter the same retry path."""
    monkeypatch.setenv(HEADERS_TIMEOUT_ENV, "5")
    handler = _handler_with_hooks_stubbed()
    provider_config = _provider_config_mock()
    client = AsyncHTTPHandler()
    post_calls = {"n": 0}
    response = _response_mock()

    async def timeout_then_success(*args, **kwargs):
        post_calls["n"] += 1
        if post_calls["n"] == 1:
            raise litellm.Timeout(
                message="Connection timed out. Timeout passed=stub",
                model="stub-model",
                llm_provider="litellm-httpx-handler",
            )
        return response

    client.post = timeout_then_success  # type: ignore[method-assign]

    result = await _invoke(handler, provider_config, client, stream=True)

    assert post_calls["n"] == 2
    assert result is provider_config.get_async_streaming_response_iterator.return_value
