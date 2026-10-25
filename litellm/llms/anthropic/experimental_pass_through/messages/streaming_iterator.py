"""Anthropic Messages stream iterator helpers.

Kollab traces this layer to distinguish provider header wait from the first
actual streaming bytes returned to LiteLLM. The iterator emits only byte counts
and timing metadata, never provider chunks or prompt text.
"""

import asyncio
import json
from datetime import datetime
from typing import Any, AsyncIterator, List, Union

from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
from litellm.proxy.kollab_latency_debug import latency_now, log_latency_stage
from litellm.proxy.pass_through_endpoints.success_handler import (
    PassThroughEndpointLogging,
)
from litellm.types.passthrough_endpoints.pass_through_endpoints import EndpointType
from litellm.types.utils import GenericStreamingChunk, ModelResponseStream

GLOBAL_PASS_THROUGH_SUCCESS_HANDLER_OBJ = PassThroughEndpointLogging()


class BaseAnthropicMessagesStreamingIterator:
    """
    Base class for Anthropic Messages streaming iterators that provides common logic
    for streaming response handling and logging.
    """

    def __init__(
        self,
        litellm_logging_obj: LiteLLMLoggingObj,
        request_body: dict,
    ):
        self.litellm_logging_obj = litellm_logging_obj
        self.request_body = request_body
        self.start_time = datetime.now()
        self.latency_started_at = latency_now()

    async def _handle_streaming_logging(self, collected_chunks: List[bytes]):
        """Handle the logging after all chunks have been collected."""
        from litellm.proxy.pass_through_endpoints.streaming_handler import (
            PassThroughStreamingHandler,
        )

        end_time = datetime.now()
        asyncio.create_task(
            PassThroughStreamingHandler._route_streaming_logging_to_handler(
                litellm_logging_obj=self.litellm_logging_obj,
                passthrough_success_handler_obj=GLOBAL_PASS_THROUGH_SUCCESS_HANDLER_OBJ,
                url_route="/v1/messages",
                request_body=self.request_body or {},
                endpoint_type=EndpointType.ANTHROPIC,
                start_time=self.start_time,
                raw_bytes=collected_chunks,
                end_time=end_time,
            )
        )

    def get_async_streaming_response_iterator(
        self,
        httpx_response,
        request_body: dict,
        litellm_logging_obj: LiteLLMLoggingObj,
    ) -> AsyncIterator:
        """Helper function to handle Anthropic streaming responses using the existing logging handlers"""
        from litellm.proxy.pass_through_endpoints.streaming_handler import (
            PassThroughStreamingHandler,
        )

        # Use the existing streaming handler for Anthropic
        return PassThroughStreamingHandler.chunk_processor(
            response=httpx_response,
            request_body=request_body,
            litellm_logging_obj=litellm_logging_obj,
            endpoint_type=EndpointType.ANTHROPIC,
            start_time=self.start_time,
            passthrough_success_handler_obj=GLOBAL_PASS_THROUGH_SUCCESS_HANDLER_OBJ,
            url_route="/v1/messages",
        )

    def _convert_chunk_to_sse_format(self, chunk: Union[dict, Any]) -> bytes:
        """
        Convert a chunk to Server-Sent Events format.

        This method should be overridden by subclasses if they need custom
        chunk formatting logic.
        """
        if isinstance(chunk, dict):
            event_type: str = str(chunk.get("type", "message"))
            payload = f"event: {event_type}\n" f"data: {json.dumps(chunk)}\n\n"
            return payload.encode()
        else:
            # For non-dict chunks, return as is
            return chunk

    async def async_sse_wrapper(
        self,
        completion_stream: AsyncIterator[
            Union[bytes, GenericStreamingChunk, ModelResponseStream, dict]
        ],
    ) -> AsyncIterator[bytes]:
        """
        Generic async SSE wrapper that converts streaming chunks to SSE format
        and handles logging.

        This method provides the common logic for both Anthropic and Bedrock implementations.
        """
        collected_chunks = []
        first_chunk_seen = False
        stream_started_at = latency_now()
        log_latency_stage(
            "stream_iteration_start",
            started_at=stream_started_at,
            data=self.request_body,
            logging_obj=self.litellm_logging_obj,
            route_type="anthropic_messages",
        )

        async for chunk in completion_stream:
            encoded_chunk = self._convert_chunk_to_sse_format(chunk)
            collected_chunks.append(encoded_chunk)
            if not first_chunk_seen:
                first_chunk_seen = True
                log_latency_stage(
                    "first_stream_chunk",
                    started_at=stream_started_at,
                    data=self.request_body,
                    logging_obj=self.litellm_logging_obj,
                    route_type="anthropic_messages",
                    details={
                        "chunk_type": type(chunk).__name__,
                        "encoded_bytes": len(encoded_chunk),
                    },
                )
            yield encoded_chunk

        # Handle logging after all chunks are processed
        log_latency_stage(
            "stream_iteration_finished",
            started_at=stream_started_at,
            data=self.request_body,
            logging_obj=self.litellm_logging_obj,
            route_type="anthropic_messages",
            details={"chunk_count": len(collected_chunks)},
        )
        await self._handle_streaming_logging(collected_chunks)
