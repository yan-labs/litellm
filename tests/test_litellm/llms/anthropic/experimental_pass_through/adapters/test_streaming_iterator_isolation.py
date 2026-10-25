"""
Concurrent AnthropicStreamWrapper instances must not share their chunk queue.

The wrapper buffers several events at once (e.g. content_block_stop +
content_block_start + input_json_delta when a text block switches to a tool_use
block) and drains them on the following __anext__ calls. When that buffer was a
class-level attribute, a second stream running on the same event loop popped
the first stream's buffered events: it started with a foreign content_block_start
and received another request's tool arguments.
"""

import asyncio
import os
import sys
from typing import Any, AsyncIterator, List

import pytest

sys.path.insert(0, os.path.abspath("../../../../.."))

from litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator import (
    AnthropicStreamWrapper,
)
from litellm.types.utils import (
    ChatCompletionDeltaToolCall,
    Delta,
    Function,
    ModelResponseStream,
    StreamingChoices,
)


def _text_chunk(text: str, finish_reason: str = None) -> ModelResponseStream:
    return ModelResponseStream(
        choices=[
            StreamingChoices(
                index=0, delta=Delta(content=text), finish_reason=finish_reason
            )
        ]
    )


def _tool_chunk(name: str, arguments: str) -> ModelResponseStream:
    tool_call = ChatCompletionDeltaToolCall(
        id=f"call_{name}",
        type="function",
        index=0,
        function=Function(name=name, arguments=arguments),
    )
    return ModelResponseStream(
        choices=[
            StreamingChoices(
                index=0,
                delta=Delta(content=None, tool_calls=[tool_call]),
                finish_reason=None,
            )
        ]
    )


async def _stream(items: List[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


async def _drain(wrapper: AnthropicStreamWrapper) -> List[dict]:
    return [event async for event in wrapper]


def test_concurrent_wrappers_do_not_share_chunk_queue():
    async def run():
        stream_a = AnthropicStreamWrapper(
            _stream(
                [
                    _text_chunk("text of request A "),
                    _tool_chunk("Read", '{"file_path": "/secret/of/A"}'),
                    _text_chunk("", "tool_calls"),
                ]
            ),
            model="m",
        )
        stream_b = AnthropicStreamWrapper(
            _stream([_text_chunk("text of request B", "stop")]), model="m"
        )

        # Advance A until the text -> tool_use switch leaves events buffered.
        for _ in range(4):
            await stream_a.__anext__()
        assert stream_a.chunk_queue, "setup: A should still hold buffered events"

        # B's very first event must be its own message_start, not A's leftovers.
        first_b = await stream_b.__anext__()
        assert first_b["type"] == "message_start"

        events_b = [first_b] + await _drain(stream_b)
        assert "/secret/of/A" not in str(events_b)
        assert "input_json_delta" not in str(events_b)

        # A still delivers its own buffered events afterwards.
        events_a = await _drain(stream_a)
        assert "/secret/of/A" in str(events_a)

    asyncio.run(run())


def test_chunk_queue_is_per_instance():
    first = AnthropicStreamWrapper(_stream([]), model="m")
    second = AnthropicStreamWrapper(_stream([]), model="m")
    assert first.chunk_queue is not second.chunk_queue
