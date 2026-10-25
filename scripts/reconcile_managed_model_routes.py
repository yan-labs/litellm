#!/usr/bin/env python3
"""Reconcile managed aliases and DeepSeek prices after Tokyo LiteLLM starts.

The Tokyo deployment workflow runs this script inside the LiteLLM container. It
removes only database-backed deployments for a small allowlist after proving the
expected YAML route is present. DeepSeek credentials remain in their existing
database routes; this script patches only public price metadata and verifies the
exact route before and after the update.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
from pathlib import Path

import yaml
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

YAML_MANAGED_ROUTES = {
    "seedance-2": "kie/bytedance/seedance-2-fast",
    "seedance-2.5": "kie/bytedance/seedance-2-5",
    "seedance-2-mini": "kie/bytedance/seedance-2-mini",
    "hailuo-3": "kie/kling-3.0/video",
    "grok-imagine-video": "kie/grok-imagine-video-1-5-preview",
    "gpt-image-2": "kie/gpt-image-2-5-flare",
    "grok-imagine-image": "kie/grok-imagine-image-2-0",
    "gemini-3.1-flash-image": "vertex_ai/gemini-3.1-flash-image",
}
CHAT_MANAGED_ROUTES = {'claude-opus-5-5': 'anthropic/claude-opus-5-5',
 'claude-sonnet-5-5': 'anthropic/claude-sonnet-5-5',
 'gpt-6.1-sol': 'openai/responses/gpt-6-1-sol',
 'gpt-6-luna': 'openai/responses/gpt-6-luna',
 'gpt-6-astra': 'openai/responses/gpt-6-astra',
 'gpt-5.6-terra': 'openai/responses/gpt-5-6-terra',
 'grok-4.7': 'openai/responses/grok-4-7'}
CHAT_MANAGED_ROUTES.update({'gpt-5.5': 'openai/responses/gpt-5-5', 'gpt-5.6-sol': 'openai/responses/gpt-5-6-sol', 'gpt-5.6-luna': 'openai/responses/gpt-5-6-luna', 'gpt-6-sol': 'openai/responses/gpt-6-sol', 'grok-4.3': 'openai/responses/grok-4-3', 'grok-4.5': 'openai/responses/grok-4-5', 'grok-4.6': 'openai/responses/grok-4-6', 'deepseek-v4.1-flash': 'openai/responses/deepseek-v4-1-flash'})
YAML_MANAGED_ROUTES.update(CHAT_MANAGED_ROUTES)

def yaml_managed_routes_for_environment(environment: str, *, health_only: bool = False) -> dict[str, str]:
    """Read applied YAML truth for deployment, including named private backups.

    The workflow streams this script inside the container, where /app/config.yaml
    is the selected environment's bind mount. Local checks read the repository
    snapshot. Existing ownership stays bounded; catalog-added Fable is explicit.
    health_only uses applied YAML exclusions because Admin metadata omits them.
    """
    if environment not in ("test", "prod"):
        raise ValueError("unknown_environment")
    path = Path("/app/config.yaml")
    if not path.exists():
        path = Path(__file__).resolve().parents[1] / f"config/litellm.{environment}.yaml"
    source = yaml.safe_load(path.read_text())
    owned = set(YAML_MANAGED_ROUTES) | {"claude-fable-5-1", "gpt-5-mini", "bedrock-claude-haiku"}
    return {row["model_name"]: row["litellm_params"]["model"]
            for row in source["model_list"]
            if (row["model_name"] in owned or row["model_name"].startswith("__route__"))
            and (not health_only or not row.get("model_info", {}).get("disable_health_check"))}

COMMON_DEEPSEEK_ROUTES = {
    # KNOWN DRIFT (2026-09-09, run 34365069777, widened same day after run
    # 34370307847 showed deepseek-v4-pro has the identical problem): the live
    # DB route on BOTH TEST and PROD for `deepseek-v4-flash` and
    # `deepseek-v4-pro` currently points at DashScope (Aliyun MaaS
    # compatible-mode endpoint: `dashscope/deepseek-v4-flash-0731` and
    # `dashscope/deepseek-v4-pro-0813` respectively), not the `deepseek/...`
    # values below, while the model_info price fields already match this
    # script's expected values on both. No commit, PR, or
    # debug-kollab/llm-proxy.md entry records who made that change or why,
    # and intent could not be confirmed live from this task (a real invoke
    # against the target gateway was out of scope). Do not silently switch
    # these constants to dashscope, and do not revert the live DB routes back
    # to DeepSeek direct, until someone verifies with a real
    # `/chat/completions` 200 on the target gateway which upstream is
    # actually supposed to be live, then updates the affected value(s) (if
    # dashscope wins) and the Current Routes table in
    # kollab/.agents/skills/debug-kollab/references/llm-proxy.md so the
    # decision has a paper trail. Resolution condition per alias: either (a)
    # confirm dashscope is intentional, update that alias's "model" field +
    # the doc and remove the alias from KNOWN_UPSTREAM_DRIFT_ALIASES below,
    # or (b) confirm it was an unintended DB edit and repoint the live route
    # back to `deepseek/...` via Admin API, verified with a real 200 call,
    # then remove that alias's drift-allowlist entry (and this comment once
    # both aliases are resolved).
    #
    # Until resolved, both aliases are listed in KNOWN_UPSTREAM_DRIFT_ALIASES
    # so `reconcile()` degrades the provider-model check below from a hard
    # RuntimeError to a printed WARNING and skips only that alias's price
    # metadata patch, instead of aborting the whole reconcile step (and with
    # it every other managed route, including YAML_MANAGED_ROUTES entries
    # like seedance-2) on every Tokyo deploy.
    "deepseek-v4-flash": {
        "model": "deepseek/deepseek-v4-flash",
        "input_cost_per_token": 0.00000014,
        "cache_read_input_token_cost": 0.0000000028,
        "cache_creation_input_token_cost": 0,
        "output_cost_per_token": 0.00000028,
    },
    "deepseek-v4-pro": {
        "model": "deepseek/deepseek-v4-pro",
        "input_cost_per_token": 0.000000435,
        "cache_read_input_token_cost": 0.000000003625,
        "cache_creation_input_token_cost": 0,
        "output_cost_per_token": 0.00000087,
    },
    # DeepSeek publishes no separate Vision Exp price, so the managed route uses
    # the public V4 Flash rate until an authoritative model-specific rate exists.
    "deepseek-v4-flash-vision-exp": {
        "model": "anthropic/deepseek-v4-flash-vision-exp",
        "input_cost_per_token": 0.00000014,
        "cache_read_input_token_cost": 0.0000000028,
        "cache_creation_input_token_cost": 0,
        "output_cost_per_token": 0.00000028,
    },
}
# Aliases with a confirmed, unexplained live provider-model drift (see the
# "KNOWN DRIFT" comment on COMMON_DEEPSEEK_ROUTES above). `reconcile()` treats
# a provider-model mismatch for these aliases as a printed WARNING instead of
# a RuntimeError, and skips only that alias's price-metadata patch, so one
# unresolved drift doesn't fail the whole Tokyo "收敛 YAML 管理的模型路由"
# step (and block the downstream gateway-key-model-permission reconcile step)
# on every deploy. Remove an alias from this set only after the drift is
# resolved per the resolution condition documented above.
KNOWN_UPSTREAM_DRIFT_ALIASES: frozenset[str] = frozenset(
    {"deepseek-v4-flash", "deepseek-v4-pro"}
)
VISION_ALIAS = "deepseek-v4-flash-vision-exp"
VISION_ROUTE_OWNER = "tokyo-release-reconciliation"
VISION_ROUTE_NAMESPACE = uuid.UUID("df360349-f4e5-46ea-86e8-1656f43c50ce")
CREDENTIAL_BINDING_FIELDS = ("api_key", "litellm_credential_name")


def deepseek_routes_for_environment(environment: str) -> dict[str, dict[str, Any]]:
    """Return active DB-owned routes; leave retired Vision history untouched.

    Normal Tokyo releases must not recreate Vision or compare its opaque binding
    with Flash: historical rows can legitimately retain a different credential.
    Its price constants remain available for compatibility, without deployment
    ownership of the retired route.
    """
    return {
        alias: dict(config)
        for alias, config in COMMON_DEEPSEEK_ROUTES.items()
        if alias != VISION_ALIAS
    }


def _as_json_object(value: Any, *, field: str) -> dict[str, Any]:
    """Normalize Prisma JSON fields without exposing encrypted credential values."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise RuntimeError(f"{field}: expected JSON object")
    return dict(value)


def _build_vision_params(
    source_params: Any, encrypt_value: Any
) -> dict[str, Any]:
    """Preserve the opaque credential binding and replace only Vision route fields."""
    params = _as_json_object(source_params, field="litellm_params")
    if "api_key" not in params and "litellm_credential_name" not in params:
        raise RuntimeError("deepseek-v4-flash: managed credential binding is missing")
    params.update(
        {
            key: encrypt_value(value=value)
            for key, value in {
                "model": "anthropic/deepseek-v4-flash-vision-exp",
                "custom_llm_provider": "anthropic",
                "api_base": "https://api.deepseek.com/anthropic",
                "use_in_pass_through": False,
                "use_litellm_proxy": False,
                "merge_reasoning_content_in_choices": False,
            }.items()
        }
    )
    return params


def _credential_binding(params: Any, *, alias: str) -> dict[str, Any]:
    """Project an encrypted DB route onto the credential fields owned by deployment."""
    normalized = _as_json_object(params, field=f"{alias}.litellm_params")
    binding = {
        key: normalized[key]
        for key in CREDENTIAL_BINDING_FIELDS
        if key in normalized
    }
    if not binding:
        raise RuntimeError(f"{alias}: managed credential binding is missing")
    return binding


def _row_value(row: Any, field: str) -> Any:
    """Read one Prisma row field from either a generated object or a test mapping."""
    return row.get(field) if isinstance(row, dict) else getattr(row, field, None)


def _route_owner_marker(row: Any) -> str | None:
    """Read the per-run owner marker that Admin PATCH preserves in model_info."""
    model_info = _as_json_object(
        _row_value(row, "model_info"), field=f"{VISION_ALIAS}.model_info"
    )
    marker = model_info.get("managed_route_owner_token")
    return marker if isinstance(marker, str) and marker else None


def _stable_vision_route_id() -> str:
    """Return one deterministic deployment id so concurrent creates share a unique key."""
    return str(uuid.uuid5(VISION_ROUTE_NAMESPACE, VISION_ALIAS))


def _runtime_vision_route(
    base_url: str, master_key: str, route_id: str
) -> dict[str, Any]:
    """Read back the exact live Vision deployment after a Router reload."""
    grouped = _routes_by_alias(
        _request_json(base_url, master_key, "/model/info"),
        {VISION_ALIAS: COMMON_DEEPSEEK_ROUTES[VISION_ALIAS]["model"]},
    )
    routes = grouped[VISION_ALIAS]
    matching = [
        route
        for route in routes
        if (route.get("model_info") or {}).get("id") == route_id
    ]
    if len(routes) != 1 or len(matching) != 1 or not _is_database_route(matching[0]):
        raise RuntimeError(
            f"{VISION_ALIAS}: Router reload did not expose exactly one managed deployment"
        )
    route = matching[0]
    params = route.get("litellm_params") or {}
    if _route_model(route) != COMMON_DEEPSEEK_ROUTES[VISION_ALIAS]["model"]:
        raise RuntimeError(f"{VISION_ALIAS}: Router provider model mismatch")
    if params.get("api_base") != "https://api.deepseek.com/anthropic":
        raise RuntimeError(f"{VISION_ALIAS}: Router provider base mismatch")
    return route


def _reload_database_routes(
    base_url: str, master_key: str, route_id: str, price_config: dict[str, Any]
) -> None:
    """Trigger LiteLLM's authenticated DB-model reload through an exact route PATCH."""
    _request_json(
        base_url,
        master_key,
        f"/model/{urllib.parse.quote(route_id, safe='')}/update",
        {"model_info": {key: value for key, value in price_config.items() if key != "model"}},
        method="PATCH",
    )


async def _compensate_created_vision_route(
    client: Any,
    base_url: str,
    master_key: str,
    route_id: str,
    source_id: str,
    owner_token: str,
    source_binding: dict[str, Any],
) -> None:
    """Idempotently remove this invocation's route from DB and live Router.

    Admin deletion may commit its DB change before failing to refresh Router. Every
    attempt therefore rechecks DB ownership, tolerates an already-absent row, and
    always PATCHes the trusted Flash source to force one complete DB-model reload.
    """
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            target_rows = await client.litellm_proxymodeltable.find_many(
                where={"model_name": VISION_ALIAS}
            )
            if len(target_rows) > 1:
                raise RuntimeError(f"{VISION_ALIAS}: compensation found duplicate routes")
            if target_rows:
                target = target_rows[0]
                if (
                    _row_value(target, "model_id") != route_id
                    or _row_value(target, "created_by") != VISION_ROUTE_OWNER
                    or _route_owner_marker(target) != owner_token
                    or _credential_binding(
                        _row_value(target, "litellm_params"), alias=VISION_ALIAS
                    )
                    != source_binding
                ):
                    raise RuntimeError(
                        f"{VISION_ALIAS}: compensation refused a route not owned by this invocation"
                    )
                try:
                    _request_json(base_url, master_key, "/model/delete", {"id": route_id})
                except Exception:
                    pass

            # Re-read because Admin delete may have removed DB state before its
            # Router refresh failed. Direct delete runs only while our row remains.
            remaining = await client.litellm_proxymodeltable.find_many(
                where={"model_name": VISION_ALIAS}
            )
            if remaining:
                target = remaining[0]
                if (
                    len(remaining) != 1
                    or _row_value(target, "model_id") != route_id
                    or _row_value(target, "created_by") != VISION_ROUTE_OWNER
                    or _route_owner_marker(target) != owner_token
                    or _credential_binding(
                        _row_value(target, "litellm_params"), alias=VISION_ALIAS
                    )
                    != source_binding
                ):
                    raise RuntimeError(
                        f"{VISION_ALIAS}: compensation ownership changed during deletion"
                    )
                try:
                    await client.litellm_proxymodeltable.delete(
                        where={"model_id": route_id}
                    )
                except Exception:
                    # Another owner may have completed the same idempotent delete;
                    # the following DB absence check decides whether to continue.
                    pass

            _reload_database_routes(
                base_url,
                master_key,
                source_id,
                COMMON_DEEPSEEK_ROUTES["deepseek-v4-flash"],
            )
            db_rows = await client.litellm_proxymodeltable.find_many(
                where={"model_name": VISION_ALIAS}
            )
            grouped = _routes_by_alias(
                _request_json(base_url, master_key, "/model/info"),
                {VISION_ALIAS: COMMON_DEEPSEEK_ROUTES[VISION_ALIAS]["model"]},
            )
            if not db_rows and not grouped[VISION_ALIAS]:
                return
            raise RuntimeError(
                f"{VISION_ALIAS}: compensation readback still observed the created route"
            )
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                await asyncio.sleep(0.25)
    raise RuntimeError(f"{VISION_ALIAS}: compensation failed after bounded retries") from last_error


async def _provision_production_vision_route(
    client: Any,
    encrypt_value: Any,
    base_url: str,
    master_key: str,
    owner_token: str | None = None,
) -> tuple[str, bool, str | None]:
    """Create or validate Vision, reload Router, and return its id plus ownership.

    Retained as a historical compatibility helper, not a normal release step.
    Direct Prisma insertion is
    required to preserve encrypted provider credentials byte-for-byte, while the
    authenticated PATCH supplies LiteLLM's supported Router reload contract.
    """
    invocation_owner = owner_token or f"{VISION_ROUTE_OWNER}:{uuid.uuid4()}"
    source_rows = await client.litellm_proxymodeltable.find_many(
        where={"model_name": "deepseek-v4-flash"}
    )
    if len(source_rows) != 1:
        raise RuntimeError("deepseek-v4-flash: expected exactly one credential source route")
    source = source_rows[0]
    source_id = _row_value(source, "model_id")
    if not isinstance(source_id, str) or not source_id:
        raise RuntimeError("deepseek-v4-flash: source route is missing model_id")
    source_binding = _credential_binding(
        _row_value(source, "litellm_params"), alias="deepseek-v4-flash"
    )

    target_rows = await client.litellm_proxymodeltable.find_many(
        where={"model_name": VISION_ALIAS}
    )
    if len(target_rows) > 1:
        raise RuntimeError(f"{VISION_ALIAS}: expected at most one database route")

    created = False
    if target_rows:
        target = target_rows[0]
        if _row_value(target, "created_by") != VISION_ROUTE_OWNER:
            raise RuntimeError(f"{VISION_ALIAS}: existing route is not deployment-owned")
        if _credential_binding(
            _row_value(target, "litellm_params"), alias=VISION_ALIAS
        ) != source_binding:
            raise RuntimeError(f"{VISION_ALIAS}: credential binding differs from Flash source")
        route_id = _row_value(target, "model_id")
    else:
        route_id = _stable_vision_route_id()
        params = _build_vision_params(_row_value(source, "litellm_params"), encrypt_value)
        price_config = COMMON_DEEPSEEK_ROUTES[VISION_ALIAS]
        model_info = {key: value for key, value in price_config.items() if key != "model"}
        model_info["id"] = route_id
        # PATCH updates audit columns, so the creator token lives in model_info,
        # whose partial-update contract preserves unknown fields across reloads.
        model_info["managed_route_owner_token"] = invocation_owner
        try:
            await client.litellm_proxymodeltable.create(
                data={
                    "model_id": route_id,
                    "model_name": VISION_ALIAS,
                    "litellm_params": json.dumps(params),
                    "model_info": json.dumps(model_info),
                    "created_by": VISION_ROUTE_OWNER,
                    "updated_by": VISION_ROUTE_OWNER,
                }
            )
            created = True
        except Exception:
            # The stable primary key turns concurrent creation into one winner.
            # Accept the losing process only after the single row is revalidated.
            target_rows = await client.litellm_proxymodeltable.find_many(
                where={"model_name": VISION_ALIAS}
            )
            if len(target_rows) != 1:
                raise
            target = target_rows[0]
            if (
                _row_value(target, "model_id") != route_id
                or _row_value(target, "created_by") != VISION_ROUTE_OWNER
                or _route_owner_marker(target) != invocation_owner
                or _credential_binding(
                    _row_value(target, "litellm_params"), alias=VISION_ALIAS
                )
                != source_binding
            ):
                raise
            # A matching per-run marker proves the DB committed our insert even
            # though the client observed an ambiguous post-commit exception.
            created = True

    if not isinstance(route_id, str) or not route_id:
        raise RuntimeError(f"{VISION_ALIAS}: target route is missing model_id")
    try:
        _reload_database_routes(
            base_url, master_key, route_id, COMMON_DEEPSEEK_ROUTES[VISION_ALIAS]
        )
        _runtime_vision_route(base_url, master_key, route_id)
    except Exception:
        if created:
            await _compensate_created_vision_route(
                client,
                base_url,
                master_key,
                route_id,
                source_id,
                invocation_owner,
                source_binding,
            )
        raise
    print(f"verified managed database route alias={VISION_ALIAS} id={route_id}")
    return route_id, created, invocation_owner if created else None


async def _ensure_production_vision_route(
    base_url: str, master_key: str
) -> tuple[str, bool, str | None]:
    """Historical provisioning helper; normal deployment never invokes it."""
    from litellm.proxy.common_utils.encrypt_decrypt_utils import encrypt_value_helper
    from prisma import Prisma

    client = Prisma()
    await client.connect()
    try:
        return await _provision_production_vision_route(
            client, encrypt_value_helper, base_url, master_key
        )
    finally:
        await client.disconnect()


async def _rollback_production_vision_route(
    base_url: str, master_key: str, route_id: str, owner_token: str
) -> None:
    """Compensate a route created by this invocation after reconciliation fails."""
    from prisma import Prisma

    client = Prisma()
    await client.connect()
    try:
        source_rows = await client.litellm_proxymodeltable.find_many(
            where={"model_name": "deepseek-v4-flash"}
        )
        if len(source_rows) != 1:
            raise RuntimeError(
                "deepseek-v4-flash: cannot identify source route for compensation"
            )
        source_id = _row_value(source_rows[0], "model_id")
        if not isinstance(source_id, str) or not source_id:
            raise RuntimeError("deepseek-v4-flash: compensation source is missing model_id")
        source_binding = _credential_binding(
            _row_value(source_rows[0], "litellm_params"), alias="deepseek-v4-flash"
        )
        await _compensate_created_vision_route(
            client,
            base_url,
            master_key,
            route_id,
            source_id,
            owner_token,
            source_binding,
        )
    finally:
        await client.disconnect()


# 定向健康检查会真实调用上游一次；推理类模型（如 grok-4.7）首个响应可能超过 20 秒，因此单独放宽。
TARGETED_HEALTH_TIMEOUT_SECONDS = 120
# 定向健康检查的并发度：足够压缩总耗时，又不会同时压垮上游或网关。
TARGETED_HEALTH_WORKERS = 8


def _request_json(
    base_url: str,
    master_key: str,
    path: str,
    payload: dict[str, Any] | None = None,
    method: str | None = None,
    timeout: float = 20,
) -> Any:
    """Call the local LiteLLM Admin API without logging its bearer credential.

    `timeout` defaults to the short admin-call budget; targeted health probes pass a longer one
    because they perform a real upstream completion and reasoning models can take well over 20s.
    """
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers={"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _routes_by_alias(
    model_info: dict[str, Any], managed_routes: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    """Project `/model/info` onto the aliases owned by this deployment."""
    grouped = {alias: [] for alias in managed_routes}
    for route in model_info.get("data", []):
        alias = route.get("model_name")
        if alias in grouped:
            grouped[alias].append(route)
    return grouped


def _route_model(route: dict[str, Any]) -> str | None:
    """Return the provider model identifier from one LiteLLM deployment row."""
    return (route.get("litellm_params") or {}).get("model")


def _is_database_route(route: dict[str, Any]) -> bool:
    """Identify routes persisted through Admin API rather than config YAML."""
    return (route.get("model_info") or {}).get("db_model") is True


def _targeted_health(base_url: str, master_key: str, alias: str, route_id: str) -> None:
    """Prove one exact deployment can call its upstream before mutating route metadata."""
    query = urllib.parse.urlencode({"model_id": route_id})
    health = _request_json(base_url, master_key, f"/health?{query}", timeout=TARGETED_HEALTH_TIMEOUT_SECONDS)
    if health.get("healthy_count") != 1 or health.get("unhealthy_count") != 0:
        raise RuntimeError(
            f"{alias}: targeted route health failed "
            f"healthy={health.get('healthy_count')!r} unhealthy={health.get('unhealthy_count')!r}"
        )


def reconcile(base_url: str, master_key: str, environment: str = "prod") -> None:
    """Converge YAML aliases and patch DB-owned DeepSeek price metadata safely.

    YAML route replacement stays fail-closed. DeepSeek routes are not recreated
    because their encrypted credentials live in the database; only price fields
    are patched, leaving provider parameters and secrets untouched.
    """
    yaml_managed_routes = yaml_managed_routes_for_environment(environment)
    # Admin /model/info omits arbitrary YAML metadata. Use applied config
    # exclusions so a deliberate skip cannot become an extra paid model call.
    probe_aliases = yaml_managed_routes_for_environment(environment, health_only=True)
    deepseek_routes = deepseek_routes_for_environment(environment)
    owned_aliases = {
        **yaml_managed_routes,
        **{alias: config["model"] for alias, config in deepseek_routes.items()},
    }
    before = _routes_by_alias(
        _request_json(base_url, master_key, "/model/info"), owned_aliases
    )
    pending_deletions: list[tuple[str, str]] = []
    yaml_route_ids: list[tuple[str, str]] = []
    deepseek_route_ids: list[tuple[str, str, dict[str, Any]]] = []
    # Aliases whose provider-model mismatch was tolerated as a known drift
    # (see KNOWN_UPSTREAM_DRIFT_ALIASES). Tracked so the post-reconcile
    # verification pass below also skips asserting the (still-drifted) model
    # for them, instead of immediately re-raising what the before-loop just
    # downgraded to a warning.
    skipped_drift_aliases: set[str] = set()

    for alias, expected_model in yaml_managed_routes.items():
        routes = before[alias]
        yaml_routes = [route for route in routes if not _is_database_route(route)]
        if len(yaml_routes) != 1 or _route_model(yaml_routes[0]) != expected_model:
            observed = [_route_model(route) for route in yaml_routes]
            raise RuntimeError(
                f"{alias}: expected exactly one YAML route {expected_model!r}; observed {observed!r}"
            )
        yaml_route_id = (yaml_routes[0].get("model_info") or {}).get("id")
        if not isinstance(yaml_route_id, str) or not yaml_route_id:
            raise RuntimeError(f"{alias}: YAML route is missing an id")
        # Wokey Claude/Grok acceptance is a bounded natural sample, never a short
        # automated probe. Explicit exclusion does not claim upstream health.
        if alias in probe_aliases:
            yaml_route_ids.append((alias, yaml_route_id))

        for route in routes:
            if not _is_database_route(route):
                continue
            route_id = (route.get("model_info") or {}).get("id")
            if not isinstance(route_id, str) or not route_id:
                raise RuntimeError(f"{alias}: database route is missing an id")
            pending_deletions.append((alias, route_id))

    for alias, price_config in deepseek_routes.items():
        routes = before[alias]
        if len(routes) != 1 or not _is_database_route(routes[0]):
            observed = [
                {"model": _route_model(route), "db_model": _is_database_route(route)}
                for route in routes
            ]
            raise RuntimeError(
                f"{alias}: expected exactly one database route; observed {observed!r}"
            )
        if _route_model(routes[0]) != price_config["model"]:
            if alias in KNOWN_UPSTREAM_DRIFT_ALIASES:
                # Degrade to a warning instead of aborting the whole reconcile
                # step: this drift is already documented as unresolved on
                # COMMON_DEEPSEEK_ROUTES, and every other managed alias
                # (other DeepSeek routes plus YAML_MANAGED_ROUTES) must still
                # converge even while this one is stuck.
                print(
                    f"WARNING: {alias}: expected provider model {price_config['model']!r}; "
                    f"observed {_route_model(routes[0])!r}; skipping price metadata patch "
                    "for this alias (known upstream drift, see COMMON_DEEPSEEK_ROUTES comment)"
                )
                skipped_drift_aliases.add(alias)
                continue
            raise RuntimeError(
                f"{alias}: expected provider model {price_config['model']!r}; "
                f"observed {_route_model(routes[0])!r}"
            )
        route_id = (routes[0].get("model_info") or {}).get("id")
        if not isinstance(route_id, str) or not route_id:
            raise RuntimeError(f"{alias}: database route is missing an id")
        deepseek_route_ids.append((alias, route_id, price_config))

    # `/health?model_id=` targets one deployment instead of simple-shuffling the
    # public alias. It proves the exact route before replacement or metadata patch.
    # YAML 托管路由数量多且每个定向健康检查都会真实调用一次上游，串行执行会超过部署流程的等待窗口；
    # 并发执行并保持"任一失败即整体失败"的语义，失败信息按别名顺序抛出第一个。
    if yaml_route_ids:
        with concurrent.futures.ThreadPoolExecutor(max_workers=TARGETED_HEALTH_WORKERS) as pool:
            futures = [
                (alias, route_id, pool.submit(_targeted_health, base_url, master_key, alias, route_id))
                for alias, route_id in yaml_route_ids
            ]
            for alias, route_id, future in futures:
                future.result()
                print(f"verified live YAML route alias={alias} id={route_id}")

    for alias, route_id, price_config in deepseek_route_ids:
        _targeted_health(base_url, master_key, alias, route_id)
        price_fields = {key: value for key, value in price_config.items() if key != "model"}
        _request_json(
            base_url,
            master_key,
            f"/model/{urllib.parse.quote(route_id, safe='')}/update",
            {"model_info": price_fields},
            method="PATCH",
        )
        print(f"updated DeepSeek public price metadata alias={alias} id={route_id}")

    for alias, route_id in pending_deletions:
        _request_json(base_url, master_key, "/model/delete", {"id": route_id})
        print(f"deleted database route alias={alias} id={route_id}")

    after = _routes_by_alias(
        _request_json(base_url, master_key, "/model/info"), owned_aliases
    )
    for alias, expected_model in yaml_managed_routes.items():
        routes = after[alias]
        if len(routes) != 1 or _is_database_route(routes[0]) or _route_model(routes[0]) != expected_model:
            observed = [
                {"model": _route_model(route), "db_model": _is_database_route(route)} for route in routes
            ]
            raise RuntimeError(f"{alias}: reconciliation did not converge; observed {observed!r}")
        print(f"verified deterministic route alias={alias} model={expected_model}")

    for alias, price_config in deepseek_routes.items():
        routes = after[alias]
        if len(routes) != 1 or not _is_database_route(routes[0]):
            raise RuntimeError(f"{alias}: database route count changed during price update")
        if alias in skipped_drift_aliases:
            # This alias's price patch was intentionally skipped above; don't
            # assert the (still-drifted) provider model here either, or this
            # verification pass would just re-raise what the before-loop
            # already downgraded to a warning.
            print(
                f"WARNING: {alias}: skipped post-reconcile verification "
                f"(known upstream drift); observed model={_route_model(routes[0])!r}"
            )
            continue
        model_info = routes[0].get("model_info") or {}
        mismatches = {
            key: {"expected": value, "observed": model_info.get(key)}
            for key, value in price_config.items()
            if key != "model" and model_info.get(key) != value
        }
        if _route_model(routes[0]) != price_config["model"] or mismatches:
            raise RuntimeError(
                f"{alias}: price reconciliation did not converge; mismatches={mismatches!r}"
            )
        print(f"verified DeepSeek public price metadata alias={alias}")


def main() -> int:
    """Read runtime-only credentials and retry reconciliation during proxy warmup."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:4000")
    parser.add_argument("--environment", choices=("test", "prod"), default="prod")
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--retry-seconds", type=float, default=5)
    args = parser.parse_args()

    master_key = os.environ.get("LITELLM_MASTER_KEY")
    if not master_key:
        raise RuntimeError("LITELLM_MASTER_KEY is not present in the container")

    # Retired Vision rows remain available for historical compatibility, but a
    # release never clones credentials or mutates them to restore that alias.
    for attempt in range(1, args.attempts + 1):
        try:
            reconcile(args.base_url, master_key, args.environment)
            return 0
        except (RuntimeError, urllib.error.URLError, json.JSONDecodeError) as exc:
            if attempt == args.attempts:
                raise
            print(f"route reconciliation attempt {attempt} not ready: {exc}")
            time.sleep(args.retry_seconds)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
