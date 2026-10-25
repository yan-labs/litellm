"""Offline retirement checks cover live traffic, route-only backups and deploy ordering."""
import contextlib
import io
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx

SPEC = importlib.util.spec_from_file_location('retirement', Path(__file__).with_name('reconcile_aliyun_v41_route.py'))
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)


class RetirementTest(unittest.TestCase):
    """Retirement never deletes active routes or blocks prior convergence/key grants."""

    def run_retirement(self, count=0, dry_run=False, export=None):
        """Replace Prisma and Admin transport with one exact legacy DB deployment."""
        rows = [{'model_name': 'gpt-6-astra-pro', 'litellm_params': {
            'model': 'openai/gpt-6-astra-pro', 'custom_llm_provider': 'openrouter',
            'api_base': 'https://user:secret@example.com/v1?key=secret', 'api_key': 'never-export'},
            'model_info': {'id': 'route-id', 'db_model': True}}]
        deleted = []
        def handler(request):
            if request.url.path == '/model/delete':
                deleted.append(json.loads(request.content)['id'])
                rows.clear()
            return httpx.Response(200, json={'data': rows.copy()})
        db = types.SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(),
             litellm_spendlogs=types.SimpleNamespace(count=AsyncMock(return_value=count)))
        factory = httpx.AsyncClient
        with patch.dict(sys.modules, {'prisma': types.SimpleNamespace(Prisma=lambda: db)}), \
             patch.dict(os.environ, {'LITELLM_MASTER_KEY': 'test-secret'}), \
             patch.object(subject.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            asyncio.run(subject.retire_openrouter_chat(dry_run=dry_run, export=export))
        return deleted

    def test_dry_run_exports_only_routes_and_does_not_delete(self):
        """A backup contains ids and sanitized routing fields, never secret values."""
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'routes.json')
            self.assertEqual(self.run_retirement(dry_run=True, export=path), [])
            raw = Path(path).read_text()
            self.assertNotIn('secret', raw)
            self.assertNotIn('api_key', raw)
            self.assertEqual(json.loads(raw)[0]['api_base'], 'https://example.com/v1')
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)

    def test_live_alias_is_preserved(self):
        """A recent successful call blocks deletion with an actionable alias."""
        with self.assertRaisesRegex(RuntimeError, 'retired_alias_has_recent_traffic:gpt-6-astra-pro'):
            self.run_retirement(count=1)

    def test_zero_traffic_exact_alias_is_deleted(self):
        """Only a known deployment with no seven-day usage is eligible."""
        self.assertEqual(self.run_retirement(), ['route-id'])

    def test_inventory_never_exposes_admin_key_or_route_secrets(self):
        """The production readback command projects credentials out before output."""
        spec = importlib.util.spec_from_file_location('inventory', Path(__file__).with_name('read_model_route_inventory.py'))
        inventory = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inventory)
        payloads = [
            {'data': [{'model_name': 'chat', 'litellm_params': {'model': 'dashscope/chat',
                       'api_key': 'secret-value', 'api_base': 'https://u:secret@example.com/v1?token=secret'}, 'model_info': {'id': 'id'}}]},
            {'keys': [{'key_alias': 'kollab-server-ai-models-20260529', 'token': 'secret-identifier'}]},
            {'info': {'models': ['chat'], 'api_key': 'secret-value'}},
        ]
        payloads[0]['data'].append(payloads[0]['data'][0].copy())
        with patch.dict(os.environ, {'LITELLM_MASTER_KEY': 'secret-master'}), \
             patch.object(inventory, 'urlopen', side_effect=[io.StringIO(json.dumps(p)) for p in payloads]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            inventory.read_inventory('prod')
        self.assertEqual(len(json.loads(output.getvalue().splitlines()[0])['routes']), 1)
        self.assertNotIn('secret', output.getvalue())
        self.assertIn('https://example.com/v1', output.getvalue())

    def test_retirement_runs_after_key_and_yaml_convergence(self):
        """A retirement rejection leaves convergence and product grants already completed."""
        workflow = (Path(__file__).parents[1] / '.github/workflows/deploy-tokyo.yml').read_text()
        self.assertIn('"$TEST_PYTHON" -m unittest scripts/test_reconcile_aliyun_v41_route.py', workflow)
        self.assertLess(workflow.index('SCRIPT_B64=$(base64 < scripts/reconcile_managed_model_routes.py'), workflow.index('python - --retire-only'))
        self.assertLess(workflow.index('SCRIPT_B64=$(base64 < scripts/reconcile_gateway_key_models.py'), workflow.index('python - --retire-only'))
        source = Path(__file__).with_name('reconcile_aliyun_v41_route.py').read_text()
        self.assertIn('if args.retire_only or args.dry_run or args.export_only:', source)


if __name__ == '__main__':
    unittest.main()
