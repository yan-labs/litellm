"""Offline reproduction of Wokey's messages.1.output_config rejection."""
import copy
import pytest
from litellm.llms.anthropic.experimental_pass_through.messages.transformation import AnthropicMessagesConfig

@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5", "claude-haiku-4-5"])
@pytest.mark.parametrize("base,is_wokey", [("https://api.wokey.ai", True), ("https://api.wokey.ai/v1", True), ("https://api.anthropic.com", False), ("https://api.kie.ai", False)])
def test_message_output_config_scope_and_cache(model, base, is_wokey):
    messages = [{"role": "user", "content": "Help organize a weekend outing."}, {"role": "assistant", "content": [{"type": "text", "text": "Let me check the options.", "cache_control": {"type": "ephemeral"}}], "output_config": {"effort": "high"}}]
    before = copy.deepcopy(messages)
    params = {"max_tokens": 16000, "thinking": {"type": "enabled", "budget_tokens": 1024}, "output_config": {"effort": "medium"}, "tools": [{"name": "lookup", "input_schema": {"type": "object"}, "cache_control": {"type": "ephemeral"}}], "tool_choice": {"type": "auto"}, "system": [{"type": "text", "text": "Help plan practical outings.", "cache_control": {"type": "ephemeral"}}]}
    result = AnthropicMessagesConfig().transform_anthropic_messages_request(model=model, messages=messages, anthropic_messages_optional_request_params=copy.deepcopy(params), litellm_params={"api_base": base}, headers={})
    expected = copy.deepcopy(before)
    if is_wokey:
        expected[1].pop("output_config")
    assert result["messages"] == expected
    assert messages == before
    for key, value in params.items():
        assert result[key] == value

@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-fable-5-1"])
@pytest.mark.asyncio
async def test_public_task_http_mock(model):
    import json
    import httpx
    import litellm
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
    seen = []
    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        assert "output_config" not in body["messages"][1]
        assert body["output_config"] == {"effort": "medium"}
        assert body["messages"][1]["content"][0]["cache_control"] == {"type": "ephemeral"}
        return httpx.Response(200, json={"id": "msg_mock", "type": "message", "role": "assistant", "model": model, "content": [{"type": "text", "text": "Here is a practical plan."}], "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 29000, "output_tokens": 10, "cache_read_input_tokens": 28000}}, request=request)
    client = AsyncHTTPHandler()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    messages = [{"role": "user", "content": "Plan a relaxed weekend outing."}, {"role": "assistant", "content": [{"type": "text", "text": "Let me organize the options.", "cache_control": {"type": "ephemeral"}}], "output_config": {"effort": "high"}}]
    result = await litellm.anthropic_messages(model="anthropic/" + model, api_base="https://api.wokey.ai", api_key="mock-key", messages=messages, max_tokens=16000, output_config={"effort": "medium"}, tools=[{"name": "lookup", "input_schema": {"type": "object"}}], tool_choice={"type": "auto"}, client=client, num_retries=0)
    assert len(seen) == 1
    assert result["usage"]["cache_read_input_tokens"] == 28000
    await client.client.aclose()
