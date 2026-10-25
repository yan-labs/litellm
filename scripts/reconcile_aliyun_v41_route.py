#!/usr/bin/env python3
"""Tokyo deployment step: provision explicitly opted-in private Aliyun DB deployments.

Reads db_managed_routes in the mounted YAML. The existing private Aliyun source
is immutable; its encrypted binding is reused only inside this process. YAML
reconciliation never owns this new DB route. PROD without the opt-in is a no-op.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
from pathlib import Path

import httpx
import yaml

ROUTES = {
    "deepseek-v4.1-flash": ("dashscope/deepseek-v4.1-flash", {"openai/responses/deepseek-v4-1-flash", "openrouter/deepseek/deepseek-v4.1-flash"}),
    "glm-5.3-flash": ("dashscope/ZHIPU/GLM-5.3-Flash", {"openrouter/z-ai/glm-5.3-flash"}),
}
SOURCE = "deepseek-v4-flash"


def decode(value, secret_box):
    """Decode a DB field in memory, preserving nonencrypted option values."""
    if not isinstance(value, str):
        return value
    try:
        return secret_box.decrypt(base64.urlsafe_b64decode(value)).decode()
    except Exception:
        return value


async def reconcile(alias, model, allowed_stale):
    """Called before YAML convergence; creates first, removes only this migrated alias.

    The opt-in specification is deliberately exact. Repeated deploys verify the
    existing new route rather than editing Aliyun parameters or credentials.
    """
    spec = yaml.safe_load(Path("/app/config.yaml").read_text()).get("db_managed_routes", {}).get(alias)
    if not spec:
        print("Aliyun private route: inactive; no changes")
        return
    if spec.get("model") != model or spec.get("source_alias") != SOURCE:
        raise RuntimeError("unexpected_private_route_spec")
    from prisma import Prisma
    from nacl.secret import SecretBox
    master = os.environ["LITELLM_MASTER_KEY"]
    box = SecretBox(hashlib.sha256(os.environ.get("LITELLM_SALT_KEY", master).encode()).digest())
    db = Prisma()
    await db.connect()
    async with httpx.AsyncClient(timeout=45, headers={"Authorization": "Bearer " + master}) as client:
        async def api(path, payload=None):
            """Use local Admin API; suppress upstream body and credential-bearing errors."""
            response = await client.request("POST" if payload is not None else "GET", "http://127.0.0.1:4000" + path, json=payload)
            if response.status_code != 200:
                raise RuntimeError("admin_status_" + str(response.status_code))
            return response.json()
        try:
            sources = await db.litellm_proxymodeltable.find_many(where={"model_name": SOURCE})
            if len(sources) != 1:
                raise RuntimeError("private_source_not_unique")
            source = sources[0]
            raw = json.loads(source.litellm_params) if isinstance(source.litellm_params, str) else source.litellm_params
            source_hash = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
            params = {k: decode(v, box) for k, v in raw.items()}
            if params.get("model") != "dashscope/deepseek-v4-flash-0731" or not params.get("api_base", "").endswith("cn-beijing.maas.aliyuncs.com/compatible-mode/v1"):
                raise RuntimeError("unexpected_private_source")
            params["model"] = model
            data = await api("/model/info")
            routes = [r for r in data["data"] if r.get("model_name") == alias]
            chosen = [r for r in routes if r.get("litellm_params", {}).get("model") == model]
            if len(chosen) > 1 or any(not r.get("model_info", {}).get("db_model") for r in routes):
                raise RuntimeError("conflicting_private_deployment")
            # Only stale rows of this explicitly enumerated migrated alias may be removed.
            stale = [r for r in routes if r not in chosen]
            def stale_model(route):
                # Admin normalizes openrouter/foo to model=foo plus provider=openrouter.
                p = route.get("litellm_params", {})
                value = p.get("model", "")
                provider = p.get("custom_llm_provider")
                return provider + "/" + value if provider and not value.startswith(provider + "/") else value
            if any(stale_model(r) not in allowed_stale for r in stale):
                raise RuntimeError("unknown_private_deployment")
            if not chosen:
                await api("/model/new", {"model_name": alias, "litellm_params": params, "model_info": spec["model_info"]})
            for route in stale:
                await api("/model/delete", {"id": route["model_info"]["id"]})
            target_rows = await db.litellm_proxymodeltable.find_many(where={"model_name": alias})
            for target in target_rows:
                raw_target = json.loads(target.litellm_params) if isinstance(target.litellm_params, str) else target.litellm_params
                decoded = {k: decode(v, box) for k, v in raw_target.items()}
                if any(decoded.get(k) != params.get(k) for k in ("api_base", "api_key", "litellm_credential_name")):
                    raise RuntimeError("private_binding_mismatch")
            data = await api("/model/info")
            routes = [r for r in data["data"] if r.get("model_name") == alias]
            if len(routes) != 1 or routes[0]["litellm_params"].get("model") != model:
                raise RuntimeError("private_readback_failed")
            after = await db.litellm_proxymodeltable.find_unique(where={"model_id": source.model_id})
            after_raw = json.loads(after.litellm_params) if isinstance(after.litellm_params, str) else after.litellm_params
            if source_hash != hashlib.sha256(json.dumps(after_raw, sort_keys=True).encode()).hexdigest():
                raise RuntimeError("private_source_changed")
            print("Aliyun private route: unique DB deployment verified; private source unchanged")
        finally:
            await db.disconnect()


async def retire_openrouter_chat(*, dry_run=False, export=None, export_only=False):
    """Deployment removes only enumerated obsolete DB chat routes with no recent traffic.

    YAML replacements must already exist; model fingerprints and zero seven-day
    spend-log rows prevent deleting a live or differently owned deployment. Voice
    remains outside this list because its public capability has no proven replacement.
    """
    from prisma import Prisma
    retired = {
        'gpt-6-astra-pro': {'openrouter/openai/gpt-6-astra-pro'},
        'glm-5v-turbo': {'openrouter/z-ai/glm-5v-turbo', 'anthropic/z-ai/glm-5v-turbo'},
        'glm-5.1': {'openrouter/z-ai/glm-5.1', 'z-ai/glm-5.1'},
        'old-minimax-m2.7': {'minimax/minimax-m2.7', 'openrouter/minimax/minimax-m2.7'},
        'openrouter/moonshotai/kimi-k2.5': {'openrouter/moonshotai/kimi-k2.5'},
        'kimi-k2.6': {'openrouter/moonshotai/kimi-k2.6'},
    }
    db = Prisma()
    await db.connect()
    master = os.environ['LITELLM_MASTER_KEY']
    async with httpx.AsyncClient(timeout=45, headers={'Authorization': 'Bearer ' + master}) as client:
        try:
            response = await client.get('http://127.0.0.1:4000/model/info')
            response.raise_for_status()
            data = response.json()['data']
            # Route-only snapshots contain no secret values; bindings must be restored through managed credentials.
            if export:
                backup = []
                for row in data:
                    if not row.get('model_info', {}).get('db_model'):
                        continue
                    params = row.get('litellm_params', {})
                    from urllib.parse import urlsplit
                    base = urlsplit(params.get('api_base') or '')
                    backup.append({'id': row['model_info']['id'], 'model_name': row['model_name'],
                                   'model': params.get('model'), 'custom_llm_provider': params.get('custom_llm_provider'),
                                   'api_base': (base.scheme + '://' + (base.hostname or '') + base.path) if base.scheme else '',
                                   'litellm_credential_name': params.get('litellm_credential_name')})
                with open(export, 'w', opener=lambda path, flags: os.open(path, flags, 0o600)) as snapshot:
                    json.dump(backup, snapshot, indent=2)
            if export_only:
                print('Route-only DB backup exported')
                return
            rows = {r['model_info']['id']: r for r in data if r.get('model_name') in retired}
            for ident, row in rows.items():
                alias = row['model_name']; params = row.get('litellm_params', {})
                model = params.get('model', ''); provider = params.get('custom_llm_provider')
                if provider and not model.startswith(provider + '/') and provider == 'openrouter':
                    model = provider + '/' + model
                if not row.get('model_info', {}).get('db_model') or model not in retired[alias]:
                    raise RuntimeError('unexpected_retired_deployment')
                from datetime import datetime, timedelta, timezone
                count = await db.litellm_spendlogs.count(where={'model_group': alias, 'startTime': {'gte': datetime.now(timezone.utc) - timedelta(days=7)}})
                if count:
                    raise RuntimeError('retired_alias_has_recent_traffic:' + alias)
            if dry_run:
                print('OpenRouter retirement dry-run candidates=' + str(len(rows)))
                return
            for ident in rows:
                response = await client.post('http://127.0.0.1:4000/model/delete', json={'id': ident})
                response.raise_for_status()
            response = await client.get('http://127.0.0.1:4000/model/info')
            response.raise_for_status()
            if any(r.get('model_name') in retired for r in response.json()['data']):
                raise RuntimeError('retired_alias_readback_failed')
            print('OpenRouter retired chat deployments removed=' + str(len(rows)))
        finally:
            await db.disconnect()


if __name__ == "__main__":
    # Credentials and arbitrary provider errors must never enter workflow output.
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument('--retire-only', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--export-only', action='store_true')
    parser.add_argument('--export', help='Route-only DB backup file; never contains credentials')
    args = parser.parse_args()
    try:
        async def main():
            """Provision only enumerated opt-in routes; never mutate the DB source."""
            if args.retire_only or args.dry_run or args.export_only:
                await retire_openrouter_chat(dry_run=args.dry_run, export=args.export, export_only=args.export_only)
            else:
                for alias, (model, allowed_stale) in ROUTES.items():
                    await reconcile(alias, model, allowed_stale)
        asyncio.run(main())
    except Exception as error:
        print("Aliyun reconciliation/retirement failed: " + type(error).__name__)
        if isinstance(error, RuntimeError) and str(error).startswith("retired_alias_has_recent_traffic:"):
            print("WARNING: kept live retired route " + str(error).split(":", 1)[1])
        raise SystemExit(1)
