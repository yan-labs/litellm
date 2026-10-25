"""Offline coverage for kie.ai's Anthropic-compatible authentication."""

import unittest

from litellm.llms.anthropic.common_utils import AnthropicModelInfo


class KieAnthropicHeadersTest(unittest.TestCase):
    def test_authentication_headers_match_exact_hostname(self):
        placeholder = "offline-placeholder"
        config = AnthropicModelInfo()
        for api_base in (
            "https://api.kie.ai/claude",
            "https://api.anthropic.com",
            "https://api.deepseek.com/anthropic",
            "https://api.minimax.io/anthropic",
            "https://api.kie.ai.example.com/claude",
            None,
        ):
            with self.subTest(api_base=api_base):
                headers = config.get_anthropic_headers(
                    api_key=placeholder, api_base=api_base
                )
                self.assertEqual(headers["x-api-key"], placeholder)
                validated = config.validate_environment(
                    headers={},
                    model="claude-sonnet-5-5",
                    messages=[],
                    optional_params={},
                    litellm_params={},
                    api_key=placeholder,
                    api_base=api_base,
                )
                self.assertEqual(validated, headers)
                if api_base == "https://api.kie.ai/claude":
                    self.assertEqual(headers["authorization"], f"Bearer {placeholder}")
                else:
                    self.assertNotIn("authorization", headers)
