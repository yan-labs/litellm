"""
OpenRouter Anthropic Messages pass-through configuration.

Allows OpenRouter to handle /v1/messages requests natively in Anthropic format,
avoiding the lossy Anthropic→OpenAI→Anthropic double-transformation that drops
cache_creation_input_tokens and other Anthropic-specific usage fields.
"""

from typing import Any, Dict, List, Optional, Tuple

import litellm
from litellm.llms.anthropic.experimental_pass_through.messages.transformation import (
    AnthropicMessagesConfig,
)
from litellm.secret_managers.main import get_secret_str


class OpenRouterAnthropicMessagesConfig(AnthropicMessagesConfig):
    """
    OpenRouter-specific config for native Anthropic /v1/messages pass-through.

    Inherits the standard Anthropic messages config and overrides only what
    differs for OpenRouter: API base URL, authentication, and model name
    normalization.
    """

    def get_complete_url(
        self,
        api_base: Optional[str],
        api_key: Optional[str],
        model: str,
        optional_params: dict,
        litellm_params: dict,
        stream: Optional[bool] = None,
    ) -> str:
        api_base = (
            api_base
            or get_secret_str("OPENROUTER_API_BASE")
            or "https://openrouter.ai/api"
        )
        api_base = api_base.rstrip("/")
        if not api_base.endswith("/v1/messages"):
            api_base = f"{api_base}/v1/messages"
        return api_base

    def validate_anthropic_messages_environment(
        self,
        headers: dict,
        model: str,
        messages: List[Any],
        optional_params: dict,
        litellm_params: dict,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> Tuple[dict, Optional[str]]:
        api_key = (
            api_key
            or litellm.api_key
            or get_secret_str("OPENROUTER_API_KEY")
        )
        if not api_key:
            raise ValueError(
                "OpenRouter API key is required. Set OPENROUTER_API_KEY "
                "or pass api_key parameter."
            )

        # OpenRouter uses Bearer auth (not x-api-key)
        if "authorization" not in headers and "x-api-key" not in headers:
            headers["authorization"] = f"Bearer {api_key}"
        if "anthropic-version" not in headers:
            headers["anthropic-version"] = "2023-06-01"
        if "content-type" not in headers:
            headers["content-type"] = "application/json"

        return headers, api_base

    def transform_anthropic_messages_request(
        self,
        model: str,
        messages: List[Dict],
        anthropic_messages_optional_request_params: Dict,
        litellm_params: "litellm.types.router.GenericLiteLLMParams",
        headers: dict,
    ) -> Dict:
        # Normalize model name: strip "openrouter/" prefix if present
        if model.startswith("openrouter/"):
            model = model.split("/", 1)[1]

        return super().transform_anthropic_messages_request(
            model=model,
            messages=messages,
            anthropic_messages_optional_request_params=anthropic_messages_optional_request_params,
            litellm_params=litellm_params,
            headers=headers,
        )
