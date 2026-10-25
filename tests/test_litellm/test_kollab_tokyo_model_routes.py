"""Validate the Git-replayable model routes for Kollab's Tokyo gateways."""

from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_MODELS = {
    "gpt-5.6-sol": "openrouter/openai/gpt-5.6-sol",
    "gpt-5.6-terra": "openai/responses/gpt-5-6-terra",
    "gpt-5.6-luna": "openrouter/openai/gpt-5.6-luna",
}
COMPATIBILITY_FLAGS = (
    "use_in_pass_through",
    "use_litellm_proxy",
    "merge_reasoning_content_in_choices",
)


@pytest.mark.parametrize(
    ("config_name", "credential_name"),
    (
        ("litellm.test.yaml", "openrouter-gpt"),
        ("litellm.prod.yaml", "openrouter-toyo"),
    ),
)
def test_gpt_5_6_routes_are_replayable_and_secret_free(
    config_name: str, credential_name: str
) -> None:
    """Ensure both environments keep exact, additive, secret-free managed routes."""
    config_path = REPO_ROOT / "config" / config_name
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    model_list = config["model_list"]

    assert any(model["model_name"] == "bedrock-claude-haiku" for model in model_list)

    for public_alias, upstream_model in EXPECTED_MODELS.items():
        matching_routes = [
            model for model in model_list if model["model_name"] == public_alias
        ]
        assert len(matching_routes) == 1

        params = matching_routes[0]["litellm_params"]
        assert params["model"] == upstream_model
        if public_alias == "gpt-5.6-terra":
            assert params["custom_llm_provider"] == "openai"
            assert params["api_base"] == "https://api.kie.ai/codex/v1"
            assert params["litellm_credential_name"] == "kie-ai"
        else:
            assert params["custom_llm_provider"] == "openrouter"
            assert params["litellm_credential_name"] == credential_name
        assert "api_key" not in params
        assert all(params[flag] is False for flag in COMPATIBILITY_FLAGS)
