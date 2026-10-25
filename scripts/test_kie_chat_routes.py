"""Offline YAML and HTTP contracts for batch 2 and both Router entry points."""
import copy
import json
import re
from urllib.parse import urlparse
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

import litellm
from litellm.types.utils import CredentialItem
from scripts import reconcile_gateway_key_models as grants
from scripts import reconcile_managed_model_routes as reconcile
from scripts.validate_multimedia_config import CHAT_MODELS, MANAGED_MEDIA_ROUTES, validate_media_routes, media_routes_for

ROOT = Path(__file__).resolve().parents[1]
# Kollab MANAGED_GATEWAY_MODEL_CATALOG snapshot, USD per million tokens.
PRICES = {
    'claude-opus-5': (5, 25, .5, 10),
    'claude-sonnet-5': (2, 10, .2, 4),
    'claude-sonnet-4-6': (3, 15, .3, 6),
    'global.anthropic.claude-sonnet-4-6': (3, 15, .3, 6),
    'gpt-5.5': (5, 30, .5, 0),
    'gpt-5.6-sol': (2, 10, .2, 2.5),
    'gpt-5.6-luna': (.2, 1.2, .02, .25),
    'gpt-6-sol': (2, 10, .2, 2.5),
    'grok-4.3': (1.25, 2.5, .2, 1.25),
    'grok-4.5': (2, 6, .3, 2),
    'grok-4.6': (2, 6, .5, 2),
    'deepseek-v4.1-flash': (.45, 1.8, .036, 0),
}
FIELDS = ('input_cost_per_token', 'output_cost_per_token',
          'cache_read_input_token_cost', 'cache_creation_input_token_cost')


def config(environment):
    return yaml.safe_load((ROOT / f'config/litellm.{environment}.yaml').read_text())


def raw_row(raw, alias):
    match = re.search(r'^  - model_name: ' + re.escape(alias) + r'\n.*?(?=^  - model_name:|^router_settings:|\Z)', raw, re.M | re.S)
    return match[0] if match else None


@pytest.mark.parametrize('environment', ['test', 'prod'])
def test_batch2_config_prices_scope_and_reconciliation(environment):
    # Price/scope gate compares applied YAML while the HTTP mock verifies its chosen provider.
    raw = (ROOT / f'config/litellm.{environment}.yaml').read_text()
    data = config(environment)
    rows = {r['model_name']: r for r in data['model_list']}
    old_raw = subprocess.check_output(['git', 'show', f'HEAD:config/litellm.{environment}.yaml'], text=True, cwd=ROOT)
    old = yaml.safe_load(old_raw)
    expected_media = media_routes_for(environment)
    validate_media_routes(raw, environment)
    for row in data['model_list']:
        params = row.get('litellm_params', {})
        if params.get('model', '').startswith('openai/responses/') and params.get('api_base', '').startswith('https://api.kie.ai/'):
            assert row['model_info']['health_check_max_tokens'] == 16
    for alias, prices in PRICES.items():
        assert sum(r['model_name'] == alias for r in data['model_list']) == 1
        info = rows[alias]['model_info']
        assert tuple(info[k] for k in FIELDS) == pytest.approx(tuple(p / 1e6 for p in prices))
        assert info['input_cost_per_token'] > 0 and info['output_cost_per_token'] > 0
        assert alias in grants.REQUIRED_MODELS[environment]
        assert reconcile.yaml_managed_routes_for_environment(environment)[alias] == rows[alias]['litellm_params']['model']
    for row in old['model_list']:
        alias = row['model_name']
        if alias in ('gemini-3.6-flash', 'gemini-3.7-flash'):
            continue  # Retired aliases with current replacements and zero 30-day traffic.
        if alias == 'glm-5.2':
            continue  # Reverted to the existing DB-owned DashScope route.
        if alias in MANAGED_MEDIA_ROUTES:
            continue  # Media rows share the full TEST definition in both environments.
        if alias not in CHAT_MODELS:
            assert rows[alias] == row
            # All unrelated raw definitions (including TEST media and Nano) stay intact.
            if alias not in ('bedrock-claude-haiku', 'glm-5.3-flash', 'veo-3'):  # Following route comments changed.
                assert raw_row(raw, alias) == raw_row(old_raw, alias)
        else:
            before = row.get('model_info', {})
            after = rows[alias].get('model_info', {})
            assert {k: v for k, v in before.items() if k not in (*FIELDS, "litellm_provider", "health_check_max_tokens")} == {k: v for k, v in after.items() if k not in (*FIELDS, "litellm_provider", "health_check_max_tokens")}
    for alias in MANAGED_MEDIA_ROUTES:
        assert rows[alias] == expected_media[alias]
    assert 'api_key' not in json.dumps(data['router_settings'])


@pytest.mark.asyncio
@pytest.mark.parametrize('environment', ['test', 'prod'])
@pytest.mark.parametrize('entry', ['chat', 'messages'])
@pytest.mark.parametrize('alias', list(CHAT_MODELS))
@pytest.mark.parametrize('fail_kie', [False, True], ids=['success', '500-fallback'])
async def test_router_kie_preferred_and_500_openrouter_fallback(monkeypatch, environment, entry, alias, fail_kie):
    captured = []
    primary_host = urlparse(next(r['litellm_params']['api_base'] for r in config(environment)['model_list'] if r['model_name'] == alias)).hostname

    def handle(request):
        body = json.loads(request.content)
        captured.append((request.url.host, request.url.path, body['model']))
        if request.url.host == primary_host and fail_kie:
            return httpx.Response(500, json={'error': {'type': 'api_error', 'message': 'api_type error: Internal error, please try again later'}})
        if request.url.path.endswith('/messages'):
            result = {'id': 'msg_offline', 'type': 'message', 'role': 'assistant', 'model': body['model'],
                      'content': [{'type': 'text', 'text': 'Hello'}], 'stop_reason': 'end_turn',
                      'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 5}}
        elif request.url.path.endswith('/responses'):
            result = {'id': 'resp_offline', 'object': 'response', 'created_at': 1, 'status': 'completed', 'model': body['model'],
                      'output': [{'id': 'msg_offline', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                                  'content': [{'type': 'output_text', 'text': 'Hello', 'annotations': []}]}],
                      'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15},
                      'parallel_tool_calls': True, 'tool_choice': 'auto', 'tools': []}
        else:
            assert request.url.host == 'openrouter.ai'
            assert request.url.path == '/api/v1/chat/completions'
            result = {'id': 'chatcmpl_offline', 'object': 'chat.completion', 'created': 1, 'model': body['model'],
                      'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Hello'}, 'finish_reason': 'stop'}],
                      'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}
        return httpx.Response(200, json=result)

    for cls in (httpx.Client, httpx.AsyncClient):
        original = cls.__init__

        def init(self, *args, _original=original, **kwargs):
            kwargs['transport'] = httpx.MockTransport(handle)
            _original(self, *args, **kwargs)

        monkeypatch.setattr(cls, '__init__', init)
    monkeypatch.setattr(litellm, 'credential_list', [CredentialItem(credential_name=name, credential_info={}, credential_values={'api_key': 'offline-placeholder'}) for name in {r['litellm_params'].get('litellm_credential_name') for r in config(environment)['model_list']} if name])
    monkeypatch.setattr(litellm, 'telemetry', False)
    litellm.in_memory_llm_clients_cache.flush_cache()
    data = copy.deepcopy(config(environment))
    # Redis is an external service; use the Router's in-memory cache for this offline run.
    settings = {k: v for k, v in data['router_settings'].items() if not k.startswith('redis_')}
    router = litellm.Router(model_list=data['model_list'], **settings)
    try:
        kwargs = {'model': alias, 'messages': [{'role': 'user', 'content': 'Hello'}], 'max_tokens': 32, 'stream': False}
        result = await (router.acompletion(**kwargs) if entry == 'chat' else router.aanthropic_messages(**kwargs))
        assert result
        params = next(r['litellm_params'] for r in data['model_list'] if r['model_name'] == alias)
        expected_model = params['model'].split('anthropic/', 1)[1] if params['model'].startswith('anthropic/') else params['model'].split('openai/responses/', 1)[1]
        parsed = urlparse(params['api_base'])
        expected_path = parsed.path.rstrip('/') + ('/v1/messages' if params['model'].startswith('anthropic/') else '/responses')
        assert captured[0] == (parsed.hostname, expected_path, expected_model)
        if fail_kie:
            target = next(f[alias][0] for f in settings['fallbacks'] if alias in f)
            if target.startswith('__route__'):
                backup = next(r['litellm_params'] for r in data['model_list'] if r['model_name'] == target)
                endpoint = urlparse(backup['api_base'])
                path = endpoint.path.rstrip('/') + ('/v1/messages' if backup['model'].startswith('anthropic/') else '/responses')
                model = backup['model'].split('anthropic/', 1)[1] if backup['model'].startswith('anthropic/') else backup['model'].split('openai/responses/', 1)[1]
                assert captured[-1] == (endpoint.hostname, path, model)
            else:
                fallback_path = '/api/v1/messages' if entry == 'messages' else '/api/v1/chat/completions'
                assert captured[-1] == ('openrouter.ai', fallback_path, target.removeprefix('openrouter/'))
            assert all(host == primary_host for host, _, _ in captured[:-1])
        else:
            assert len(captured) == 1
            if entry == 'chat' and alias in PRICES:
                # Exercise LiteLLM's token cost calculation, using the deployed pricing metadata.
                deployment = next(r for r in router.model_list if r['model_name'] == alias)
                cost = litellm.completion_cost(completion_response=result, model=params['model'], custom_llm_provider=params['custom_llm_provider'], custom_pricing=True, router_model_id=deployment['model_info']['id'])
                assert cost > 0
                assert cost == pytest.approx((10 * PRICES[alias][0] + 5 * PRICES[alias][1]) / 1e6)
    finally:
        router.reset()
        litellm.in_memory_llm_clients_cache.flush_cache()
