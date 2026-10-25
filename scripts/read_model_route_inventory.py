#!/usr/bin/env python3
"""Read-only runtime route/key inventory for TEST verification and production cutover.

Run inside the selected LiteLLM container; master credentials stay in process memory.
Only routing fields, model grants and seven-day counts are emitted, never full rows.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


def read_inventory(environment):
    """Project all YAML/DB routes and the environment product key from local Admin API."""
    master = os.environ['LITELLM_MASTER_KEY']
    def get(path):
        """Read Admin metadata without exposing credential-bearing error bodies."""
        with urlopen(Request('http://127.0.0.1:4000' + path, headers={'Authorization': 'Bearer ' + master}), timeout=30) as response:
            return json.load(response)
    routes = []
    seen = set()
    for row in get('/model/info')['data']:
        params = row.get('litellm_params', {})
        info = row.get('model_info', {})
        ident = info.get('id')
        # Admin expands audio wildcards to catalog entries sharing one deployment id.
        if ident in seen:
            continue
        if ident:
            seen.add(ident)
        base = urlsplit(params.get('api_base') or '')
        routes.append({'id': info.get('id'), 'alias': row['model_name'], 'db_model': info.get('db_model', False),
                       'model': params.get('model'), 'provider': params.get('custom_llm_provider'),
                       'api_base': (base.scheme + '://' + (base.hostname or '') + base.path) if base.scheme else ''})
    print(json.dumps({'routes': routes}, ensure_ascii=False, separators=(',', ':')))
    alias = {'test': 'kollab-test-server-ai-models-20260529141754', 'prod': 'kollab-server-ai-models-20260529'}[environment]
    keys = [k for k in get('/key/list?' + urlencode({'key_alias': alias, 'return_full_object': 'true'})).get('keys', []) if k.get('key_alias') == alias]
    if len(keys) != 1:
        raise RuntimeError('product_key_not_unique')
    info = get('/key/info?' + urlencode({'key': keys[0]['token']}))
    print(json.dumps({'product_key_alias': alias, 'models': info.get('info', info).get('models', []),
                      'allowed_routes': info.get('info', info).get('allowed_routes', [])}))


async def recent_retired_counts():
    """Production preflight counts all retirement aliases plus stale migrated GLM."""
    from prisma import Prisma
    aliases = ['gpt-6-astra-pro', 'glm-5v-turbo', 'glm-5.1', 'old-minimax-m2.7',
               'openrouter/moonshotai/kimi-k2.5', 'kimi-k2.6', 'glm-5.3-flash']
    db = Prisma()
    await db.connect()
    try:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        counts = {alias: await db.litellm_spendlogs.count(where={'model_group': alias, 'startTime': {'gte': since}}) for alias in aliases}
        print(json.dumps({'seven_day_calls': counts}))
    finally:
        await db.disconnect()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--environment', choices=['test', 'prod'], required=True)
    parser.add_argument('--recent-counts', action='store_true')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        read_inventory(args.environment)
        if args.recent_counts:
            asyncio.run(recent_retired_counts())
    except Exception as error:
        print('readback_failed:' + type(error).__name__)
        raise SystemExit(1)
