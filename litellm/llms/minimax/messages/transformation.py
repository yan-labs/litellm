"""
MiniMax Anthropic transformation config - extends AnthropicConfig for MiniMax's Anthropic-compatible API
"""
from typing import Optional

import litellm
from litellm.llms.anthropic.experimental_pass_through.messages.transformation import (
    AnthropicMessagesConfig,
)
from litellm.secret_managers.main import get_secret_str


class MinimaxMessagesConfig(AnthropicMessagesConfig):
    """
    MiniMax Anthropic configuration that extends AnthropicConfig.
    MiniMax provides an Anthropic-compatible API at:
    - International: https://api.minimax.io/anthropic
    - China: https://api.minimaxi.com/anthropic

    Supported models:
    - MiniMax-M2.1
    - MiniMax-M2.1-lightning
    - MiniMax-M2
    """

    @property
    def custom_llm_provider(self) -> Optional[str]:
        return "minimax"

    def get_supported_anthropic_messages_params(self, model: str) -> list:
        """
        MiniMax's Anthropic-compatible endpoint does not support structured output
        parameters like ``output_format`` / ``output_config``.

        Keeping these out of the supported list lets ``drop_params=True`` remove
        them before LiteLLM sends the upstream request.

        Why ``stream`` is added explicitly here:
        Base ``AnthropicMessagesConfig`` does NOT include ``stream`` in its
        supported list — Anthropic's own endpoint accepts ``stream`` via header
        / query negotiation, so the body field is not strictly required there.
        MiniMax's Anthropic-compatible endpoint at ``/anthropic/v1/messages``
        is stricter: it only enables SSE streaming when ``"stream": true`` is
        present in the request **body**. Without this override the
        MiniMax-only ``_filter_unsupported_minimax_anthropic_messages_params``
        path (with ``drop_params=true``) silently drops ``stream`` from the
        request body, so MiniMax responds with a single JSON blob even when
        the caller requested ``stream=true``. LiteLLM then wraps that blob
        as a single SSE chunk, producing a ``Content-Type: text/event-stream``
        response with zero ``event:``/``data:`` frames — which breaks every
        Anthropic SDK client downstream (Kollab AgentCore included).
        Adding ``"stream"`` to the supported list keeps the field in the
        outgoing body and restores native MiniMax SSE streaming.
        """
        unsupported_params = {"output_format", "output_config"}
        supported = [
            param
            for param in super().get_supported_anthropic_messages_params(model=model)
            if param not in unsupported_params
        ]
        # Append rather than rely on parent — base class deliberately omits
        # ``stream``; MiniMax requires it in body. Idempotent against future
        # base-class changes that might add it.
        if "stream" not in supported:
            supported.append("stream")
        return supported

    @staticmethod
    def get_api_key(api_key: Optional[str] = None) -> Optional[str]:
        """
        Get MiniMax API key from environment or parameters.
        """
        return api_key or get_secret_str("MINIMAX_API_KEY") or litellm.api_key

    @staticmethod
    def get_api_base(
        api_base: Optional[str] = None,
    ) -> str:
        """
        Get MiniMax API base URL.
        Defaults to international endpoint: https://api.minimax.io/anthropic
        For China, set to: https://api.minimaxi.com/anthropic
        """
        return (
            api_base
            or get_secret_str("MINIMAX_API_BASE")
            or "https://api.minimax.io/anthropic/v1/messages"
        )

    def get_complete_url(
        self,
        api_base: Optional[str],
        api_key: Optional[str],
        model: str,
        optional_params: dict,
        litellm_params: dict,
        stream: Optional[bool] = None,
    ) -> str:
        """
        Get the complete URL for MiniMax API.
        Override to ensure we use MiniMax's endpoint, not Anthropic's.
        """
        # Get the base URL (either provided or default MiniMax endpoint)
        base_url = self.get_api_base(api_base=api_base)

        # If the base URL already includes the full path, return it
        if base_url.endswith("/v1/messages"):
            return base_url

        # Otherwise append the messages endpoint
        if base_url.endswith("/"):
            return f"{base_url}v1/messages"
        else:
            return f"{base_url}/v1/messages"
