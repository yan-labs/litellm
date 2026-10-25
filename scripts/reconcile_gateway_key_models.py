#!/usr/bin/env python3
"""Keep Kollab's long-lived LiteLLM gateway key model grants replayable.

Tokyo deployment runs this inside the selected LiteLLM container after routes are
healthy. It finds one environment-owned key by alias, preserves its existing model
list except retired chat wildcard, adds required routes, and verifies without printing a key.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import yaml
import urllib.parse
import urllib.request
from typing import Any


KEY_ALIASES = {
    "test": "kollab-test-server-ai-models-20260529141754",
    "prod": "kollab-server-ai-models-20260529",
}
# Public grants mirror Kollab gateway-model-allowlist.json; private backups come from applied YAML.
PUBLIC_MODELS = (
    'claude-fable-5-1',
    'claude-opus-5-5',
    'claude-sonnet-5-5',
    'claude-haiku-5-5',
    'gpt-6-astra',
    'gpt-6-sol',
    'gpt-6.1-sol',
    'gpt-6-luna',
    'gpt-5.5',
    'gpt-5.6-sol',
    'gpt-5.6-terra',
    'gpt-5.6-luna',
    'grok-4.7',
    'grok-4.6',
    'grok-4.5',
    'gemini-4-argon',
    'gemini-3.8-flash',
    'gemini-3.5-flash',
    'gemini-3.5-flash-lite',
    'gemini-3.1-pro',
    'kimi-k3',
    'deepseek-v4-flash',
    'deepseek-v4.1-flash',
    'deepseek-v4-pro',
    'deepseek-v4-flash-vision-exp',
    'glm-5.3-flash',
    'gpt-5-mini',
    'bedrock-claude-haiku',
    'jev-latest',
    'gemini-3.1-flash-image',
    'gemini-nano-banana-2.1',
    'grok-imagine-image',
    'gpt-image-2',
    'kling-v3',
    'minimax-music',
    'tripo-3d',
    'veo-3',
    'gemini-omni',
    'seedance-2',
    'seedance-2-mini',
    'seedance-2.5',
    'hailuo-3',
    'grok-imagine-video',
    'minimax-m2.7',
    'doubao-tts',
    'doubao-asr',
    'happy-horse',
    'grok-stt',
    'openrouter-tts/*',
    'openrouter-stt/*',
)
REQUIRED_MODELS = {environment: PUBLIC_MODELS for environment in ("test", "prod")}

def managed_models(environment: str) -> tuple[str, ...]:
    """Grant applied YAML public/private aliases; retired chat wildcard is excluded.

    Private backups need explicit grants on restricted product keys. Read the
    selected bind mount inside the workflow and the local snapshot in tests.
    """
    path = Path("/app/config.yaml")
    if not path.exists():
        path = Path(__file__).resolve().parents[1] / f"config/litellm.{environment}.yaml"
    source = yaml.safe_load(path.read_text())
    return tuple(dict.fromkeys((*REQUIRED_MODELS[environment],
        *(row["model_name"] for row in source["model_list"]
          if row["model_name"].startswith("__route__") or row["model_name"] in {"claude-fable-5-1", "bedrock-claude-haiku", "claude-haiku-5-5", "gpt-5-mini"}),
        *source.get("db_managed_routes", {}),
        *(("jev-latest",) if source.get("jev_passthrough", {}).get("enabled", False) else ()))))


def _request_json(
    base_url: str,
    master_key: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call one local LiteLLM admin endpoint without logging credentials."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {master_key}",
            "Content-Type": "application/json",
        },
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        decoded = json.load(response)
    if not isinstance(decoded, dict):
        raise RuntimeError("LiteLLM admin endpoint returned a non-object response")
    return decoded


def _key_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the current `/key/list` envelope used by LiteLLM."""
    rows = payload.get("keys", [])
    if not isinstance(rows, list):
        raise RuntimeError("LiteLLM key list returned an invalid shape")
    return [row for row in rows if isinstance(row, dict)]


def reconcile(base_url: str, master_key: str, environment: str) -> list[str]:
    """Converge one environment key, removing only the retired chat wildcard."""
    alias = KEY_ALIASES[environment]
    required_models = managed_models(environment)
    query = urllib.parse.urlencode(
        {"key_alias": alias, "return_full_object": "true"}
    )
    rows = [
        row
        for row in _key_rows(
            _request_json(base_url, master_key, f"/key/list?{query}")
        )
        if row.get("key_alias") == alias
    ]
    if len(rows) != 1:
        raise RuntimeError(f"{alias}: expected exactly one gateway key")
    identifier = rows[0].get("token")
    current = rows[0].get("models")
    if not isinstance(identifier, str) or not identifier:
        raise RuntimeError(f"{alias}: key identifier is missing")
    if not isinstance(current, list) or not all(
        isinstance(model, str) for model in current
    ):
        raise RuntimeError(f"{alias}: model allowlist is invalid")
    # LiteLLM uses an empty model list to mean unrestricted access. Writing only
    # the wildcard would accidentally remove every existing non-OpenRouter grant.
    if not current:
        info = _request_json(
            base_url,
            master_key,
            f"/key/info?{urllib.parse.urlencode({'key': identifier})}",
        )
        observed = info.get("info", info).get("models", [])
        if observed != []:
            raise RuntimeError(
                f"{alias}: unrestricted model grant changed during readback"
            )
        print(f"verified unrestricted gateway key alias={alias}")
        return []
    # Chat wildcard retirement must survive later releases; audio grants remain explicit.
    desired = list(dict.fromkeys([*(m for m in current if m != "openrouter/*"), *required_models]))
    updates = {"key": identifier, "models": desired}
    allowed_routes = rows[0].get("allowed_routes")
    jev_route = "/jev/v1/systemone"
    config_path = Path("/app/config.yaml")
    if not config_path.exists():
        config_path = Path(__file__).resolve().parents[1] / f"config/litellm.{environment}.yaml"
    jev_enabled = yaml.safe_load(config_path.read_text()).get("jev_passthrough", {}).get("enabled", False)
    # Endpoint grants follow the passthrough flag; a catalog grant does not enable PROD JEV.
    if (
        jev_enabled and "jev-latest" in required_models
        and isinstance(allowed_routes, list)
        and allowed_routes
        and jev_route not in allowed_routes
    ):
        updates["allowed_routes"] = [*allowed_routes, jev_route]
    if desired != current or "allowed_routes" in updates:
        _request_json(base_url, master_key, "/key/update", updates)

    info = _request_json(
        base_url,
        master_key,
        f"/key/info?{urllib.parse.urlencode({'key': identifier})}",
    )
    observed = info.get("info", info).get("models", [])
    if not isinstance(observed, list) or any(
        model not in observed for model in desired
    ) or "openrouter/*" in observed:
        raise RuntimeError(f"{alias}: model allowlist did not converge")
    if (
        "allowed_routes" in updates
        and info.get("info", info).get("allowed_routes") != updates["allowed_routes"]
    ):
        raise RuntimeError(f"{alias}: JEV route grant did not converge")
    print(f"verified gateway key alias={alias} models={','.join(required_models)}")
    return desired


def main() -> int:
    """Read the container-local master key and reconcile the selected environment."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:4000")
    parser.add_argument("--environment", choices=tuple(KEY_ALIASES), required=True)
    args = parser.parse_args()
    master_key = os.environ.get("LITELLM_MASTER_KEY", "")
    if not master_key:
        raise RuntimeError("LITELLM_MASTER_KEY is not present in the container")
    reconcile(args.base_url, master_key, args.environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
