"""Regression coverage for dynamic tier prices across LiteLLM model registration."""

import unittest

import litellm
from litellm.cost_calculator import completion_cost, cost_per_token
from litellm.litellm_core_utils.llm_cost_calc.utils import generic_cost_per_token
from litellm.types.utils import Usage
from litellm.utils import _invalidate_model_cost_lowercase_map


class DynamicTierCostRegistrationTest(unittest.TestCase):
    """Keep arbitrary token thresholds available to LiteLLM's generic cost path."""

    MODEL = "openrouter/openai/gpt-6.1-sol-dynamic-tier-regression"
    REQUEST_MODEL = "openai/gpt-6.1-sol-dynamic-tier-regression"
    PRICES = {
        "litellm_provider": "openrouter",
        "mode": "chat",
        "max_input_tokens": 1050000,
        "max_output_tokens": 128000,
        "input_cost_per_token": 0.000002,
        "output_cost_per_token": 0.00001,
        "cache_read_input_token_cost": 0.0000001,
        "cache_creation_input_token_cost": 0.0000025,
        "input_cost_per_token_above_271999_tokens": 0.000004,
        "output_cost_per_token_above_271999_tokens": 0.000015,
        "cache_read_input_token_cost_above_271999_tokens": 0.0000002,
        "cache_creation_input_token_cost_above_271999_tokens": 0.000005,
        "unrelated_custom_metadata": "must not leak into ModelInfoBase",
    }

    def setUp(self) -> None:
        """Register an isolated model-cost entry and remember any prior value."""
        self.previous_model_info = litellm.model_cost.get(self.MODEL)
        litellm.register_model(model_cost={self.MODEL: dict(self.PRICES)})

    def tearDown(self) -> None:
        """Restore global model-cost state so this test cannot affect other tests."""
        if self.previous_model_info is None:
            litellm.model_cost.pop(self.MODEL, None)
        else:
            litellm.model_cost[self.MODEL] = self.previous_model_info
        _invalidate_model_cost_lowercase_map()

    def _usage(self, prompt_tokens: int) -> Usage:
        """Build usage with both cache-read and cache-write tokens for cost checks."""
        return Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=5,
            total_tokens=prompt_tokens + 5,
            prompt_tokens_details={
                "cached_tokens": 100,
                "cache_creation_tokens": 50,
            },
        )

    def _expected_cost(self, prompt_tokens: int) -> tuple[float, float]:
        """Calculate the exact expected input and output prices at each boundary."""
        above_threshold = prompt_tokens >= 272000
        input_rate = 0.000004 if above_threshold else 0.000002
        cache_read_rate = 0.0000002 if above_threshold else 0.0000001
        cache_write_rate = 0.000005 if above_threshold else 0.0000025
        output_rate = 0.000015 if above_threshold else 0.00001
        input_cost = (
            (prompt_tokens - 150) * input_rate
            + 100 * cache_read_rate
            + 50 * cache_write_rate
        )
        return input_cost, 5 * output_rate

    def test_registration_public_model_info_and_cost_calculators_keep_threshold(self):
        """Exercise registration, get_model_info, generic cost, and completion cost."""
        model_info = litellm.get_model_info(
            model=self.REQUEST_MODEL, custom_llm_provider="openrouter"
        )
        self.assertEqual(
            model_info["input_cost_per_token_above_271999_tokens"], 0.000004
        )
        self.assertEqual(
            model_info["output_cost_per_token_above_271999_tokens"], 0.000015
        )
        self.assertEqual(
            model_info["cache_read_input_token_cost_above_271999_tokens"], 0.0000002
        )
        self.assertEqual(
            model_info["cache_creation_input_token_cost_above_271999_tokens"],
            0.000005,
        )
        self.assertNotIn("unrelated_custom_metadata", model_info)

        for prompt_tokens in (271999, 272000):
            with self.subTest(prompt_tokens=prompt_tokens):
                usage = self._usage(prompt_tokens)
                expected = self._expected_cost(prompt_tokens)
                self.assertEqual(
                    generic_cost_per_token(
                        model=self.REQUEST_MODEL,
                        usage=usage,
                        custom_llm_provider="openrouter",
                    ),
                    expected,
                )
                self.assertEqual(
                    cost_per_token(
                        model=self.REQUEST_MODEL,
                        custom_llm_provider="openrouter",
                        usage_object=usage,
                        call_type="completion",
                    ),
                    expected,
                )
                response = {
                    "id": "chatcmpl-dynamic-tier-test",
                    "model": self.REQUEST_MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": usage.model_dump(),
                }
                self.assertAlmostEqual(
                    completion_cost(
                        completion_response=response,
                        model=self.REQUEST_MODEL,
                        custom_llm_provider="openrouter",
                    ),
                    sum(expected),
                )


if __name__ == "__main__":
    unittest.main()
