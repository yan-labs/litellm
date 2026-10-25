"""End-to-end offline contracts for the public messages/chat/responses interfaces."""

import json

import httpx
import pytest

import litellm

BASE = "https://api.kie.ai/codex/v1"
KEY = "offline-placeholder"
MODELS = ["gpt-6-1-sol", "gpt-6-luna", "grok-4-7"]
TOOL = {
    "name": "lookup",
    "description": "Lookup",
    "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
}
MESSAGES = [
    {"role": "user", "content": "First"},
    {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "call_old",
                "name": "lookup",
                "input": {"q": "old"},
            }
        ],
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "call_old", "content": "Found"},
            {"type": "text", "text": "Next"},
        ],
    },
]


def response(model):
    return {
        "id": "resp_offline",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": "msg_text",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "Hello", "annotations": []}
                ],
            },
            {
                "id": "fc_new",
                "type": "function_call",
                "call_id": "call_new",
                "name": "lookup",
                "arguments": '{"q":"new"}',
                "status": "completed",
            },
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


def sse(events):
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def responses_events(model):
    result = response(model)
    text, tool = result["output"]
    events = [
        {
            "type": "response.created",
            "response": {**result, "status": "in_progress", "output": []},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**text, "content": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": text["id"],
            "output_index": 0,
            "content_index": 0,
            "delta": "Hello",
        },
        {"type": "response.output_item.done", "output_index": 0, "item": text},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {**tool, "arguments": ""},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": tool["id"],
            "output_index": 1,
            "delta": '{"q":',
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": tool["id"],
            "output_index": 1,
            "delta": '"new"}',
        },
        {"type": "response.output_item.done", "output_index": 1, "item": tool},
        {"type": "response.completed", "response": result},
    ]
    return [{**event, "sequence_number": index} for index, event in enumerate(events)]


@pytest.fixture
def outbound(monkeypatch, request):
    """Replace every httpx transport, so no SDK or handler can use the network."""
    captured = []
    force_sse = getattr(request, "param", False) is True
    compact = getattr(request, "param", False) == "compact"

    def handle(request):
        captured.append(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/messages"):
            result = {
                "id": "msg_offline",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [{"type": "text", "text": "Hello"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
            events = [
                {
                    "type": "message_start",
                    "message": {**result, "content": [], "stop_reason": None},
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 5},
                },
                {"type": "message_stop"},
            ]
        else:
            assert request.url.path in (
                "/codex/v1/responses",
                "/openai/v1/responses",
            ), str(request.url)
            result = response(body["model"])
            events = responses_events(body["model"])
            if compact:
                result = compact_response()
                events = [
                    {
                        "type": "response.completed",
                        "response": result,
                        "sequence_number": 0,
                    }
                ]
        if body.get("stream") or force_sse:
            return httpx.Response(
                200, content=sse(events), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=result)

    for cls in (httpx.Client, httpx.AsyncClient):
        original = cls.__init__

        def init(self, *args, _original=original, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            _original(self, *args, **kwargs)

        monkeypatch.setattr(cls, "__init__", init)
    monkeypatch.setattr(litellm, "telemetry", False)
    litellm.in_memory_llm_clients_cache.flush_cache()
    yield captured
    litellm.in_memory_llm_clients_cache.flush_cache()


async def anthropic_events(stream):
    chunks = [chunk async for chunk in stream]
    events = []
    for chunk in chunks:
        if isinstance(chunk, dict):
            events.append(chunk)
        else:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            events.extend(
                json.loads(line[6:])
                for line in text.splitlines()
                if line.startswith("data: ")
            )
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "base",
    [
        "https://api.kie.ai/claude",
        "https://api.anthropic.com",
        "https://api.kie.ai.example.com/claude",
    ],
)
async def test_native_messages_headers(outbound, base, stream):
    result = await litellm.anthropic.messages.acreate(
        model="anthropic/claude-sonnet-5-5",
        api_base=base,
        api_key=KEY,
        max_tokens=100,
        messages=MESSAGES,
        system="System",
        tools=[TOOL],
        stream=stream,
    )
    if stream:
        events = await anthropic_events(result)
        assert events[0]["type"] == "message_start"
        assert events[-1]["type"] == "message_stop"
    else:
        assert result["type"] == "message"
    assert len(outbound) == 1
    request = outbound[0]
    assert str(request.url) == base + "/v1/messages"
    assert bool(json.loads(request.content).get("stream")) is stream
    assert request.headers["x-api-key"] == KEY
    if base == "https://api.kie.ai/claude":
        assert request.headers["authorization"] == "Bearer " + KEY
    else:
        assert "authorization" not in request.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("completion_fallback", [False, True])
async def test_messages_responses_route(
    outbound, monkeypatch, model, stream, completion_fallback
):
    monkeypatch.setattr(
        litellm, "use_chat_completions_url_for_anthropic_messages", completion_fallback
    )
    result = await litellm.anthropic.messages.acreate(
        model="openai/responses/" + model,
        custom_llm_provider="openai",
        api_base=BASE,
        api_key=KEY,
        max_tokens=100,
        messages=MESSAGES,
        system="System",
        tools=[TOOL],
        stream=stream,
    )
    if stream:
        events = await anthropic_events(result)
        types = [event["type"] for event in events]
        assert types == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]
        assert events[4]["content_block"]["type"] == "tool_use"
        assert events[4]["content_block"]["id"] == "call_new"
        assert [event["index"] for event in events if "index" in event] == [
            0,
            0,
            0,
            1,
            1,
            1,
            1,
        ]
        assert json.loads(
            "".join(
                event["delta"]["partial_json"]
                for event in events
                if event.get("delta", {}).get("type") == "input_json_delta"
            )
        ) == {"q": "new"}
        assert events[-2]["delta"]["stop_reason"] == "tool_use"
        assert events[-2]["usage"]["output_tokens"] == 5
    else:
        assert result["type"] == "message"
        assert result["stop_reason"] == "tool_use"
        assert result["content"][0]["text"] == "Hello"
        assert result["content"][1]["input"] == {"q": "new"}
    assert len(outbound) == 1
    request = outbound[0]
    assert str(request.url) == BASE + "/responses"
    assert request.headers["authorization"] == "Bearer " + KEY
    body = json.loads(request.content)
    assert body["model"] == model
    assert body["stream"] is stream
    assert isinstance(body["input"], list)
    assert body.get("instructions") == "System" or any(
        item.get("role") == "system" for item in body["input"]
    )
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["name"] == "lookup"
    assert body["tools"][0]["parameters"] == TOOL["input_schema"]
    assert any(item.get("type") == "function_call_output" for item in body["input"])


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("interface", ["chat", "responses"])
async def test_openai_interface_regression(outbound, model, stream, interface):
    common = {
        "model": "openai/responses/" + model,
        "custom_llm_provider": "openai",
        "api_base": BASE,
        "api_key": KEY,
        "stream": stream,
    }
    if interface == "chat":
        result = await litellm.acompletion(
            messages=[{"role": "user", "content": "Hello"}], **common
        )
    else:
        result = await litellm.aresponses(
            input=[{"role": "user", "content": "Hello"}], **common
        )
    if stream:
        assert [chunk async for chunk in result]
    assert len(outbound) == 1
    assert str(outbound[0].url) == BASE + "/responses"
    assert outbound[0].headers["authorization"] == "Bearer " + KEY
    assert json.loads(outbound[0].content)["model"] == model
    assert json.loads(outbound[0].content)["stream"] is stream


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_claude_chat_regression(outbound, stream):
    result = await litellm.acompletion(
        model="anthropic/claude-sonnet-5-5",
        api_base="https://api.kie.ai/claude",
        api_key=KEY,
        messages=[{"role": "user", "content": "Hello"}],
        stream=stream,
    )
    if stream:
        assert [chunk async for chunk in result]
    assert str(outbound[0].url) == "https://api.kie.ai/claude/v1/messages"
    assert outbound[0].headers["x-api-key"] == KEY
    assert outbound[0].headers["authorization"] == "Bearer " + KEY


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_explicit_responses_overrides_registered_chat_mode(
    outbound, monkeypatch, stream
):
    # Mirrors the proxy's custom model_info.mode=chat registration.
    monkeypatch.setitem(
        litellm.model_cost,
        "openai/responses/gpt-6-1-sol",
        {
            "mode": "chat",
            "litellm_provider": "openai",
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
    )
    result = await litellm.acompletion(
        model="openai/responses/gpt-6-1-sol",
        custom_llm_provider="openai",
        api_base=BASE,
        api_key=KEY,
        messages=[{"role": "user", "content": "Hello"}],
        stream=stream,
    )
    if stream:
        assert [chunk async for chunk in result]
    assert str(outbound[0].url) == BASE + "/responses"
    assert json.loads(outbound[0].content)["model"] == "gpt-6-1-sol"


def test_fake_stream_respects_explicit_capability(monkeypatch):
    from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig

    config = OpenAIResponsesAPIConfig()
    monkeypatch.setitem(
        litellm.model_cost,
        "openai/offline-no-stream",
        {
            "mode": "responses",
            "litellm_provider": "openai",
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
            "supports_native_streaming": False,
        },
    )
    assert config.should_fake_stream("offline-no-stream", True, "openai") is True
    assert (
        config.should_fake_stream("responses/offline-no-stream", True, "openai") is True
    )
    assert config.should_fake_stream("offline-unregistered", True, "openai") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("outbound", [True], indirect=True)
@pytest.mark.parametrize("model", MODELS)
async def test_messages_non_streaming_unexpected_sse(outbound, model):
    result = await litellm.anthropic.messages.acreate(
        model="openai/responses/" + model,
        custom_llm_provider="openai",
        api_base=BASE,
        api_key=KEY,
        max_tokens=100,
        messages=MESSAGES,
        tools=[TOOL],
    )
    assert result["type"] == "message"
    assert result["content"][0]["text"] == "Hello"
    assert result["content"][1]["type"] == "tool_use"
    assert result["content"][1]["input"] == {"q": "new"}
    assert result["stop_reason"] == "tool_use"
    assert str(outbound[0].url) == BASE + "/responses"
    assert json.loads(outbound[0].content)["stream"] is False


@pytest.mark.parametrize("content_type", ["text/event-stream", "application/json"])
def test_unexpected_sse_completed_and_missing(content_type):
    from litellm.llms.openai.common_utils import OpenAIError
    from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig

    config = OpenAIResponsesAPIConfig()
    raw = httpx.Response(
        200,
        content=sse(responses_events(MODELS[0])),
        headers={"content-type": content_type},
    )
    assert config._get_non_streaming_response_json(raw) == response(MODELS[0])
    raw = httpx.Response(
        200,
        content=sse(responses_events(MODELS[0])[:-1]),
        headers={"content-type": content_type},
    )
    with pytest.raises(OpenAIError, match="received events: response.created"):
        config._get_non_streaming_response_json(raw)


@pytest.mark.parametrize(
    "base", ["https://api.openai.com/v1", "https://openrouter.ai/api/v1"]
)
@pytest.mark.parametrize("stream", [None, False, True])
def test_explicit_stream_preserves_other_request_fields(base, stream):
    from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig
    from litellm.types.router import GenericLiteLLMParams

    params = {"temperature": 0.7}
    if stream is not None:
        params["stream"] = stream
    result = OpenAIResponsesAPIConfig().transform_responses_api_request(
        "responses/offline", "Hello", params, GenericLiteLLMParams(api_base=base), {}
    )
    assert result == {
        "model": "offline",
        "input": "Hello",
        "temperature": 0.7,
        "stream": stream is True,
    }


def test_sync_responses_default_stream(outbound):
    result = litellm.responses(
        model="openai/responses/" + MODELS[0], input="Hello", api_base=BASE, api_key=KEY
    )
    assert result.output[0].content[0].text == "Hello"
    assert json.loads(outbound[0].content)["stream"] is False


def compact_response():
    return {
        "output": [
            {
                "role": "assistant",
                "id": "msg_x",
                "type": "message",
                "content": [{"annotations": [], "text": "2**.", "type": "output_text"}],
                "status": "completed",
            }
        ],
        "usage": {
            "input_tokens_details": {"cached_tokens": 0},
            "total_tokens": 92,
            "output_tokens": 3,
            "input_tokens": 89,
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "model": "gpt-5.6-luna",
        "id": "resp_x",
        "credits_consumed": 0.01,
        "object": "response",
        "status": "completed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("outbound", ["compact"], indirect=True)
@pytest.mark.parametrize(
    "interface", ["chat", "responses", "messages", "messages_stream"]
)
async def test_compact_response_interfaces(outbound, interface, monkeypatch):
    monkeypatch.setitem(
        litellm.model_cost,
        "openai/gpt-5-6-luna",
        {
            "mode": "responses",
            "litellm_provider": "openai",
            "supports_native_streaming": False,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
    )
    common = dict(
        model="openai/responses/gpt-5-6-luna",
        api_base=BASE,
        api_key=KEY,
        custom_llm_provider="openai",
    )
    if interface.startswith("messages"):
        result = await litellm.anthropic.messages.acreate(
            messages=[{"role": "user", "content": "1+1"}],
            max_tokens=16,
            stream=interface.endswith("stream"),
            **common,
        )
        if interface.endswith("stream"):
            events = await anthropic_events(result)
            assert events[-1]["type"] == "message_stop"
            assert (
                "".join(event.get("delta", {}).get("text", "") for event in events)
                == "2**."
            )
            assert events[-2]["usage"]["output_tokens"] == 3
            usage = events[-2]["usage"]
        else:
            assert result["content"][0]["text"] == "2**."
            usage = result["usage"]
        assert usage["input_tokens"] == 89
        assert usage.get("cache_read_input_tokens", 0) == 0
    elif interface == "chat":
        result = await litellm.acompletion(
            messages=[{"role": "user", "content": "1+1"}], **common
        )
        assert result.choices[0].message.content == "2**."
        assert (
            result.usage.prompt_tokens,
            result.usage.completion_tokens,
            result.usage.total_tokens,
        ) == (89, 3, 92)
    else:
        result = await litellm.aresponses(input="1+1", **common)
        assert result.output_text == "2**."
        assert result.created_at > 0
        assert (
            result.usage.input_tokens,
            result.usage.output_tokens,
            result.usage.total_tokens,
        ) == (89, 3, 92)
    assert len(outbound) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("max_tokens", [None, 16])
async def test_targeted_health_check_token_limit(outbound, max_tokens):
    from litellm.proxy.health_check import _run_model_health_check

    info = {"mode": "chat"}
    if max_tokens is not None:
        info["health_check_max_tokens"] = max_tokens
    result = await _run_model_health_check(
        {
            "model_info": info,
            "litellm_params": dict(
                model="openai/responses/deepseek-v4-1-flash",
                api_base="https://api.kie.ai/openai/v1",
                api_key=KEY,
                custom_llm_provider="openai",
            ),
        }
    )
    assert "error" not in result
    assert len(outbound) == 1
    assert json.loads(outbound[0].content)["max_output_tokens"] == (max_tokens or 1)


def test_compact_validation_and_complete_regression():
    from unittest.mock import Mock, patch
    from pydantic import ValidationError
    from litellm.types.llms.openai import ResponsesAPIResponse
    from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig

    with pytest.raises(ValidationError) as exc:
        ResponsesAPIResponse(**compact_response())
    assert [e["loc"] for e in exc.value.errors()] == [("created_at",)]
    config = OpenAIResponsesAPIConfig()
    with patch(
        "litellm.llms.openai.responses.transformation.time.time", return_value=123
    ):
        parsed = config.transform_response_api_response(
            "offline", httpx.Response(200, json=compact_response()), Mock()
        )
    assert parsed.created_at == 123
    complete = response(MODELS[0])
    parsed = config.transform_response_api_response(
        "offline", httpx.Response(200, json=complete), Mock()
    )
    assert parsed.model_dump() == ResponsesAPIResponse(**complete).model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("outbound", ["compact"], indirect=True)
@pytest.mark.parametrize("stream", [False, True])
async def test_compact_cached_usage_mapping(outbound, monkeypatch, stream):
    original = compact_response

    def cached_response():
        result = original()
        result["usage"]["input_tokens_details"]["cached_tokens"] = 7
        return result

    monkeypatch.setitem(globals(), "compact_response", cached_response)
    monkeypatch.setitem(
        litellm.model_cost,
        "openai/gpt-5-6-luna",
        {
            "mode": "responses",
            "litellm_provider": "openai",
            "supports_native_streaming": False,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
    )
    result = await litellm.anthropic.messages.acreate(
        model="openai/responses/gpt-5-6-luna",
        api_base=BASE,
        api_key=KEY,
        messages=[{"role": "user", "content": "1+1"}],
        max_tokens=16,
        stream=stream,
    )
    usage = (await anthropic_events(result))[-2]["usage"] if stream else result["usage"]
    assert usage["cache_read_input_tokens"] == 7
    assert usage["input_tokens"] == 82
