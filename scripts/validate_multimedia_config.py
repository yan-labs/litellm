"""Validate a config-only Tokyo release without inspecting credential records.

The host deploy runner sends current/candidate YAML plus the target
environment name over stdin to this module inside the existing gateway
container, where PyYAML is already installed. Only fixed error codes and
approved alias names may leave this validation boundary.
"""

import copy
import json
import sys
from typing import Any, Dict, List

import yaml


# `approved_for()` resolves optional {"test": ..., "prod": ...} model mappings.
# Omni's complete TEST-verified row is approved for both environments; keep the
# config rows, deploy ALIASES and readiness names in sync so the restricted
# transaction cannot accept a route with a different binding or video price.
APPROVED: Dict[str, Dict[str, Any]] = {
    "gemini-omni": {
        "model_name": "gemini-omni",
        "litellm_params": {
            "model": "vertex_ai/gemini-omni-1.1-flash-preview",
            "custom_llm_provider": "vertex_ai",
            "litellm_credential_name": "vertex_ai/gemini-3.1-pro-preview-credential-a6d39be8-cdf4-4f8c-92b5-2ebdbf160cf5",
            "vertex_location": "global",
        },
        "model_info": {
            "mode": "video_generation",
            "input_cost_per_token": 0.0000015,
            "output_cost_per_token": 0.000009,
            "output_cost_per_video_token": 0.0000175,
        },
    },
    "openrouter-tts/*": {
        "model_name": "openrouter-tts/*",
        "litellm_params": {
            "model": "openai/*",
            "api_base": "https://openrouter.ai/api/v1",
            "custom_llm_provider": "openai",
            "litellm_credential_name": "openrouter-toyo",
        },
        "model_info": {"mode": "audio_speech"},
    },
    "openrouter-stt/*": {
        "model_name": "openrouter-stt/*",
        "litellm_params": {
            "model": "openai/*",
            "api_base": "https://openrouter.ai/api/v1",
            "custom_llm_provider": "openai",
            "litellm_credential_name": "openrouter-toyo",
        },
        "model_info": {"mode": "audio_transcription"},
    },
}
MANAGED_MEDIA_ROUTES = {
    "seedance-2": {
        "model_name": "seedance-2",
        "litellm_params": {
            "model": "kie/bytedance/seedance-2-fast",
            "custom_llm_provider": "kie",
            "litellm_credential_name": "kie-ai",
            "use_in_pass_through": False,
            "use_litellm_proxy": False,
            "timeout": 30,
        },
        "model_info": {"mode": "video_generation"},
    },
    "seedance-2.5": {
        "model_name": "seedance-2.5",
        "litellm_params": {
            "model": "kie/bytedance/seedance-2-5",
            "custom_llm_provider": "kie",
            "litellm_credential_name": "kie-ai",
            "use_in_pass_through": False,
            "use_litellm_proxy": False,
            "timeout": 30,
        },
        "model_info": {"mode": "video_generation"},
    },
    "seedance-2-mini": {
        "model_name": "seedance-2-mini",
        "litellm_params": {
            "model": "kie/bytedance/seedance-2-mini",
            "custom_llm_provider": "kie",
            "litellm_credential_name": "kie-ai",
            "use_in_pass_through": False,
            "use_litellm_proxy": False,
            "timeout": 30,
        },
        "model_info": {"mode": "video_generation"},
    },
    "hailuo-3": {
        "model_name": "hailuo-3",
        "litellm_params": {
            "model": "kie/kling-3.0/video",
            "custom_llm_provider": "kie",
            "litellm_credential_name": "kie-ai",
            "use_in_pass_through": False,
            "use_litellm_proxy": False,
            "timeout": 30,
        },
        "model_info": {"mode": "video_generation"},
    },
    "grok-imagine-video": {
        "model_name": "grok-imagine-video",
        "litellm_params": {
            "model": "kie/grok-imagine-video-1-5-preview",
            "custom_llm_provider": "kie",
            "litellm_credential_name": "kie-ai",
            "use_in_pass_through": False,
            "use_litellm_proxy": False,
            "timeout": 30,
        },
        "model_info": {"mode": "video_generation"},
    },
    "gpt-image-2": {
        "model_name": "gpt-image-2",
        "litellm_params": {
            "model": "kie/gpt-image-2-5-flare",
            "custom_llm_provider": "kie",
            "litellm_credential_name": "kie-ai",
            "use_in_pass_through": False,
            "use_litellm_proxy": False,
        },
        "model_info": {"mode": "image_generation"},
    },
    "grok-imagine-image": {
        "model_name": "grok-imagine-image",
        "litellm_params": {
            "model": "kie/grok-imagine-image-2-0",
            "custom_llm_provider": "kie",
            "litellm_credential_name": "kie-ai",
            "use_in_pass_through": False,
            "use_litellm_proxy": False,
        },
        "model_info": {"mode": "image_generation"},
    },
    "gemini-3.1-flash-image": {
        "model_name": "gemini-3.1-flash-image",
        "litellm_params": {
            "model": "vertex_ai/gemini-3.1-flash-image",
            "custom_llm_provider": "vertex_ai",
            "litellm_credential_name": "vertex_ai/gemini-3.1-pro-preview-credential-a6d39be8-cdf4-4f8c-92b5-2ebdbf160cf5",
            "vertex_location": "global",
            "max_tokens": 8000,
        },
        "model_info": {
            "mode": "chat",
            "input_cost_per_image": 0.00056,
            "input_cost_per_token": 5e-07,
            "max_input_tokens": 65536,
            "max_output_tokens": 32768,
            "output_cost_per_image": 0.0672,
            "output_cost_per_image_token": 6e-05,
            "output_cost_per_token": 3e-06,
        },
    },

}
CHAT_MODELS = {'claude-opus-5-5': 'anthropic/claude-opus-5-5',
 'claude-sonnet-5-5': 'anthropic/claude-sonnet-5-5',
 'gpt-6.1-sol': 'openai/responses/gpt-6-1-sol',
 'gpt-6-luna': 'openai/responses/gpt-6-luna',
 'gpt-6-astra': 'openai/responses/gpt-6-astra',
 'gpt-5.6-terra': 'openai/responses/gpt-5-6-terra',
 'grok-4.7': 'openai/responses/grok-4-7'}
CHAT_MODELS.update({'claude-opus-5': 'anthropic/claude-opus-5', 'claude-sonnet-5': 'anthropic/claude-sonnet-5', 'claude-sonnet-4-6': 'anthropic/claude-sonnet-4-6', 'global.anthropic.claude-sonnet-4-6': 'anthropic/claude-sonnet-4-6', 'gpt-5.5': 'openai/responses/gpt-5-5', 'gpt-5.6-sol': 'openai/responses/gpt-5-6-sol', 'gpt-5.6-luna': 'openai/responses/gpt-5-6-luna', 'gpt-6-sol': 'openai/responses/gpt-6-sol', 'grok-4.3': 'openai/responses/grok-4-3', 'grok-4.5': 'openai/responses/grok-4-5', 'grok-4.6': 'openai/responses/grok-4-6', 'deepseek-v4.1-flash': 'openai/responses/deepseek-v4-1-flash'})

def media_routes_for(environment: str) -> Dict[str, Dict[str, Any]]:
    """Validate both environments against their approved managed media catalog.

    The local full-YAML gate uses these expectations, while the separate remote
    restricted config-only transaction retains its existing APPROVED boundary.
    """
    if environment not in ("test", "prod"):
        raise ValueError("unknown_environment")
    routes = copy.deepcopy(MANAGED_MEDIA_ROUTES)
    from scripts.route_cli import load, render, DEFAULT

    rows = {row["model_name"]: row for row in render(load(DEFAULT), environment)["model_list"]}
    for name in ("gpt-image-2", "grok-imagine-image", "grok-imagine-video"):
        routes[name] = rows[name]
    return routes

APPROVED["gpt-image-2"] = MANAGED_MEDIA_ROUTES["gpt-image-2"]

MAX_CONFIG_BYTES = 512 * 1024


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys rather than hiding unexpected config changes."""


def unique_mapping(loader: UniqueLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    """Build one mapping with duplicate-key rejection, including merge keys."""
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError("duplicate_yaml_key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def load_config(raw: str) -> dict:
    """Parse bounded config with safe errors that never include source content."""
    try:
        if not isinstance(raw, str) or len(raw.encode()) > MAX_CONFIG_BYTES:
            raise ValueError("config_size")
        data = yaml.load(raw, Loader=UniqueLoader)
        if not isinstance(data, dict) or not isinstance(data.get("model_list"), list):
            raise ValueError("config_shape")
        for row in data["model_list"]:
            if not isinstance(row, dict) or not isinstance(row.get("model_name"), str):
                raise ValueError("route_shape")
        return data
    except Exception:
        raise ValueError("invalid_config") from None


def approved_for(environment: str) -> Dict[str, Dict[str, Any]]:
    """Resolve APPROVED to the single set of expected rows for one environment.

    Called by `validate()` (and by the release tests) so every comparison
    downstream sees one concrete `litellm_params.model` value per alias
    instead of the raw {"test": ..., "prod": ...} branch. Keeping the branch
    resolution in one place means adding another per-environment alias later
    only touches this function, not the comparison logic in `validate()`.
    """
    if environment not in ("test", "prod"):
        raise ValueError("unknown_environment")
    resolved: Dict[str, Dict[str, Any]] = {}
    for name, entry in APPROVED.items():
        entry = copy.deepcopy(entry)
        model = entry["litellm_params"]["model"]
        if isinstance(model, dict):
            if environment not in model:
                raise ValueError("unknown_environment")
            entry["litellm_params"]["model"] = model[environment]
        resolved[name] = entry
    return resolved


def validate(current_raw: str, candidate_raw: str, environment: str) -> List[str]:
    """Permit only missing approved aliases; preserve all existing config meaning.

    Existing unrelated deployment groups can legitimately share a model_name.
    Target alias duplicates are rejected; unrelated rows retain exact order and
    content. Existing target drift is never silently repaired by this mode.
    `environment` selects which side of a per-environment APPROVED branch (see
    `approved_for`) this call must match, so the same alias can be legitimately
    approved to different upstream values in TEST vs PROD.
    """
    approved = approved_for(environment)
    current, candidate = load_config(current_raw), load_config(candidate_raw)
    old_rows, new_rows = current.pop("model_list"), candidate.pop("model_list")
    if canonical(current) != canonical(candidate):
        raise ValueError("settings_changed")
    if canonical([r for r in old_rows if r["model_name"] not in approved]) != canonical([r for r in new_rows if r["model_name"] not in approved]):
        raise ValueError("unrelated_routes_changed")
    added = []
    for name, expected in approved.items():
        old = [r for r in old_rows if r["model_name"] == name]
        new = [r for r in new_rows if r["model_name"] == name]
        if canonical(new) != canonical([expected]) or canonical(old) not in ("[]", canonical([expected])):
            raise ValueError("approved_route_mismatch")
        if not old:
            added.append(name)
    return added


def validate_media_routes(raw: str, environment: str) -> List[str]:
    """Verify every migrated media alias without accessing provider credentials."""
    data = load_config(raw)
    by_name = {}
    for row in data["model_list"]:
        by_name.setdefault(row["model_name"], []).append(row)
    source = by_name["gemini-3.5-flash-lite"][0]["litellm_params"]["litellm_credential_name"]
    for name, expected in media_routes_for(environment).items():
        expected = copy.deepcopy(expected)
        if expected["litellm_params"]["custom_llm_provider"] == "vertex_ai":
            expected["litellm_params"]["litellm_credential_name"] = source
        if canonical(by_name.get(name, [])) != canonical([expected]):
            raise ValueError("managed_media_route_mismatch:" + name)
    # Applied catalog entries own chat expectations; multimedia rows stay fixed.
    # This gate runs locally on committed snapshots, not the restricted remote
    # config-only transaction, which remains limited to APPROVED media additions.
    from scripts.route_cli import load, render, DEFAULT
    compiled = render(load(DEFAULT), environment)
    expected_rows = {row['model_name']: row for row in compiled['model_list']}
    for name in set(CHAT_MODELS) | {'claude-fable-5-1'}:
        if name not in expected_rows:
            continue
        if canonical(by_name.get(name, [])) != canonical([expected_rows[name]]):
            raise ValueError("managed_chat_route_mismatch:" + name)
    for name, expected in expected_rows.items():
        if name.startswith('__route__') and canonical(by_name.get(name, [])) != canonical([expected]):
            raise ValueError("managed_chat_backup_mismatch:" + name)
    fallbacks = data.get("router_settings", {}).get("fallbacks", [])
    if canonical(fallbacks) != canonical(compiled['router_settings']['fallbacks']):
        raise ValueError("managed_chat_fallback_mismatch")
    wildcard = by_name.get("openrouter/*", [])
    if wildcard:
        raise ValueError("retired_chat_openrouter_wildcard_present")
    return list(media_routes_for(environment))


def canonical(value: Any) -> str:
    """Keep bool/number distinctions so YAML coercion cannot bypass the gate."""
    try:
        return json.dumps(value, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("unsupported_config_value") from None


def main() -> int:
    """Validate stdin data; emit no raw config or parser exception details.

    `environment` in the payload comes from the host runner's fixed
    --environment choice (via `validate_in_container`'s container-name
    derivation), not from anything inside the YAML itself, so the caller
    cannot smuggle a different approval branch by editing config content.
    """
    try:
        payload = json.loads(sys.stdin.read(MAX_CONFIG_BYTES * 4))
        environment = payload["environment"]
        print(json.dumps({"added_aliases": validate(payload["current"], payload["candidate"], environment)}))
        return 0
    except Exception:
        print("multimedia_config_rejected", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
