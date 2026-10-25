"""
Tests for OpenRouterAnthropicMessagesConfig — the native Anthropic messages
pass-through path for OpenRouter that preserves cache token usage data.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath("../../../../.."))

from litellm.llms.openrouter.messages.transformation import (
    OpenRouterAnthropicMessagesConfig,
)


class TestOpenRouterAnthropicMessagesConfig:
    def setup_method(self):
        self.config = OpenRouterAnthropicMessagesConfig()

    def test_get_complete_url_default(self):
        url = self.config.get_complete_url(
            api_base=None,
            api_key="test-key",
            model="moonshotai/kimi-k2.5",
            optional_params={},
            litellm_params={},
        )
        assert url == "https://openrouter.ai/api/v1/messages"

    def test_get_complete_url_custom_base(self):
        url = self.config.get_complete_url(
            api_base="https://custom.openrouter.ai/api",
            api_key="test-key",
            model="moonshotai/kimi-k2.5",
            optional_params={},
            litellm_params={},
        )
        assert url == "https://custom.openrouter.ai/api/v1/messages"

    def test_get_complete_url_already_has_path(self):
        url = self.config.get_complete_url(
            api_base="https://custom.openrouter.ai/api/v1/messages",
            api_key="test-key",
            model="test",
            optional_params={},
            litellm_params={},
        )
        assert url == "https://custom.openrouter.ai/api/v1/messages"

    def test_validate_environment_sets_bearer_auth(self):
        headers, _ = self.config.validate_anthropic_messages_environment(
            headers={},
            model="moonshotai/kimi-k2.5",
            messages=[],
            optional_params={},
            litellm_params={},
            api_key="sk-or-test-key",
        )
        assert headers["authorization"] == "Bearer sk-or-test-key"
        assert headers["anthropic-version"] == "2023-06-01"
        assert headers["content-type"] == "application/json"

    def test_validate_environment_raises_without_key(self):
        with pytest.raises(ValueError, match="OpenRouter API key is required"):
            self.config.validate_anthropic_messages_environment(
                headers={},
                model="moonshotai/kimi-k2.5",
                messages=[],
                optional_params={},
                litellm_params={},
                api_key=None,
            )

    def test_validate_environment_preserves_existing_auth(self):
        """Should not overwrite existing authorization header."""
        headers, _ = self.config.validate_anthropic_messages_environment(
            headers={"authorization": "Bearer pre-existing"},
            model="test",
            messages=[],
            optional_params={},
            litellm_params={},
            api_key="sk-or-test-key",
        )
        assert headers["authorization"] == "Bearer pre-existing"

    def test_transform_request_strips_openrouter_prefix(self):
        """Model name 'openrouter/moonshotai/kimi-k2.5' should become 'moonshotai/kimi-k2.5'."""
        from litellm.types.router import GenericLiteLLMParams

        result = self.config.transform_anthropic_messages_request(
            model="openrouter/moonshotai/kimi-k2.5",
            messages=[{"role": "user", "content": "hi"}],
            anthropic_messages_optional_request_params={"max_tokens": 100},
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )
        assert result["model"] == "moonshotai/kimi-k2.5"

    def test_transform_request_preserves_plain_model(self):
        """Model name without prefix should pass through unchanged."""
        from litellm.types.router import GenericLiteLLMParams

        result = self.config.transform_anthropic_messages_request(
            model="moonshotai/kimi-k2.5",
            messages=[{"role": "user", "content": "hi"}],
            anthropic_messages_optional_request_params={"max_tokens": 100},
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )
        assert result["model"] == "moonshotai/kimi-k2.5"

    def test_registered_in_provider_config(self):
        """OpenRouter should be registered in get_provider_anthropic_messages_config."""
        from litellm.utils import ProviderConfigManager
        from litellm.types.utils import LlmProviders

        config = ProviderConfigManager.get_provider_anthropic_messages_config(
            model="moonshotai/kimi-k2.5",
            provider=LlmProviders.OPENROUTER,
        )
        assert config is not None
        assert isinstance(config, OpenRouterAnthropicMessagesConfig)
