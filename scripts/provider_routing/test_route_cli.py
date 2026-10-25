"""Offline checks for gateway catalog import/compiler and local CLI transitions.

Runs with stdlib unittest, blocks socket connections, and uses temporary catalogs
so demonstrations cannot change the committed table or occupied YAML files.
"""
import contextlib
import copy
import importlib.util
import io
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('route_cli', ROOT / 'scripts/route_cli.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class RoutingTests(unittest.TestCase):
    """Exercise real snapshots, malformed declarations and isolated local history."""
    def setUp(self):
        """Load local fixtures and deny networking for every test."""
        self.block = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        self.block.start()
        self.addCleanup(self.block.stop)
        self.sources = {e: yaml.safe_load(subprocess.check_output(['git', 'show', f'942c4847a:config/litellm.{e}.yaml'], cwd=ROOT, text=True)) for e in ('test', 'prod')}
        self.table = cli.infer(self.sources)
        current = cli.load(cli.DEFAULT)
        self.table['platforms']['wokey'] = current['platforms']['wokey']
        fable = next(r for r in current['environments']['test']['routes'] if r['alias'] == 'claude-fable-5-1')
        self.table['environments']['test']['routes'].append(copy.deepcopy(fable))
        self.table['environments']['test']['routes'][-1].update(primary='openrouter', fallback=[])
        fable_rows = cli.render(self.table, 'test')['model_list'][-1:]
        self.sources['test']['model_list'].extend(fable_rows)
        for env in self.sources:
            self.table['environments'][env]['credentials'].append('wokey')
            backends = {r['alias']: r['backends'] for r in current['environments'][env]['routes']}
            for route in self.table['environments'][env]['routes']:
                if 'wokey' in backends.get(route['alias'], {}):
                    route['backends']['wokey'] = copy.deepcopy(backends[route['alias']]['wokey'])

    def test_snapshot_roundtrip(self):
        """Require exact structured YAML equality for both environment fragments."""
        inferred = cli.infer(self.sources)
        for env, source in self.sources.items():
            expected = {'model_list': source['model_list'], 'router_settings': {'fallbacks': source['router_settings']['fallbacks']}}
            self.assertEqual(cli.render(inferred, env), expected)

    def test_committed_table_roundtrip(self):
        """Inactive demo mappings must not affect production or TEST rendering."""
        for env in self.sources:
            source = yaml.safe_load((ROOT / f'config/litellm.{env}.yaml').read_text())
            current = cli.load(cli.DEFAULT)
            self.assertEqual(cli.render(current, env)['model_list'], source['model_list'])
            self.assertEqual(cli.render(current, env)['router_settings']['fallbacks'], source['router_settings']['fallbacks'])

    def test_private_deployment_apply_and_rollback(self):
        """DB opt-in survives import/apply; rollback removes it without copying keys."""
        current = cli.load(cli.DEFAULT)
        source = yaml.safe_load((ROOT / 'config/litellm.test.yaml').read_text())
        inferred = cli.infer({'test': source})
        self.assertEqual(cli.render(inferred, 'test')['db_managed_routes'], source['db_managed_routes'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            path.write_text(yaml.safe_dump(source))
            # Current gateways have no OpenRouter wildcard; rollback preserves the private binding without it.
            reverted = cli.switch(current, 'test', 'deepseek-v4.1-flash', 'kie', [])
            cli.apply(reverted, 'test', path)
            self.assertNotIn('deepseek-v4.1-flash', yaml.safe_load(path.read_text()).get('db_managed_routes', {}))
            cli.apply(current, 'test', path)
            self.assertEqual(yaml.safe_load(path.read_text())['db_managed_routes'], source['db_managed_routes'])
            self.assertEqual(cli.apply(current, 'test', path), 0)

    def test_nineteen_chat_aliases(self):
        """Assert the brief's 19 managed chat routes in each snapshot."""
        for env in self.sources:
            self.assertEqual(len(self.table['environments'][env]['groups']['kie-chat']), 19)

    def test_checks_both_environments(self):
        """Reconcile all public aliases and declared credential names offline."""
        self.assertEqual(cli.check(self.table, 'test', self.sources['test']), 38)
        self.assertEqual(cli.check(self.table, 'prod', self.sources['prod']), 36)

    def test_group_switch_isolated(self):
        """A TEST group switch keeps PROD and the input catalog unchanged."""
        result = cli.switch(self.table, 'test', 'gpt-demo', 'wokey', ['kie'])
        self.assertEqual(result['environments']['prod'], self.table['environments']['prod'])
        self.assertEqual(cli.check(result, 'test', self.sources['test']), 38)
        output = cli.render(result, 'test')
        for alias in result['environments']['test']['groups']['gpt-demo']:
            entry = next(e for e in output['model_list'] if e['model_name'] == alias)
            self.assertEqual(entry['litellm_params']['api_base'], 'https://api.wokey.ai/v1')
            target = f'__route__{alias}__kie'
            self.assertIn({alias: [target]}, output['router_settings']['fallbacks'])
            self.assertTrue(any(e['model_name'] == target and e['litellm_params']['litellm_credential_name'] == 'kie-ai' for e in output['model_list']))

    def test_ordered_multiple_fallbacks(self):
        """Preserve requested Kie then OpenRouter fallback order."""
        result = cli.switch(self.table, 'test', 'gpt-6.1-sol', 'wokey', ['kie', 'openrouter'])
        cli.check(result, 'test', self.sources['test'])
        self.assertIn({'gpt-6.1-sol': ['__route__gpt-6.1-sol__kie', 'openrouter/openai/gpt-6.1-sol']}, cli.render(result, 'test')['router_settings']['fallbacks'])

    def test_unknown_selector(self):
        """Reject a typo rather than silently succeeding on an empty selection."""
        with self.assertRaises(ValueError):
            cli.switch(self.table, 'test', 'unknown', 'kie', [])

    def test_unknown_model_mapping(self):
        """A platform declaration alone never invents model compatibility."""
        result = cli.switch(self.table, 'test', 'kie-chat', 'wokey', ['kie'])
        with self.assertRaises(ValueError):
            cli.check(result, 'test', self.sources['test'])

    def test_duplicate_alias(self):
        """Duplicate route entries are rejected before rendering."""
        self.table['environments']['test']['routes'].append(copy.deepcopy(self.table['environments']['test']['routes'][0]))
        with self.assertRaises(ValueError):
            cli.check(self.table, 'test', self.sources['test'])

    def test_missing_credential(self):
        """A connection must reference the environment's credential declaration."""
        self.table['environments']['test']['credentials'].remove('kie-ai')
        with self.assertRaises(ValueError):
            cli.check(self.table, 'test', self.sources['test'])

    def test_platform_cycle(self):
        """Repeated primary in the backup chain is a cycle."""
        result = cli.switch(self.table, 'test', 'gpt-6.1-sol', 'kie', ['openrouter', 'kie'])
        with self.assertRaises(ValueError):
            cli.check(result, 'test', self.sources['test'])

    def test_alias_cycle(self):
        """Reject a fallback target which returns to its own public alias."""
        route = next(r for r in self.table['environments']['test']['routes'] if r['alias'] == 'gpt-6.1-sol')
        route['backends']['openrouter']['target'] = route['alias']
        with self.assertRaises(ValueError):
            cli.check(self.table, 'test', self.sources['test'])

    def test_alias_set_mismatch(self):
        """Compare the catalog against authoritative local YAML names."""
        self.table['environments']['test']['routes'].pop()
        with self.assertRaises(ValueError):
            cli.check(self.table, 'test', self.sources['test'])

    def test_duplicate_json_keys(self):
        """Reject duplicate keys instead of accepting JSON's last-value behavior."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'table.json'
            path.write_text('{"alias":1,"alias":2}')
            with self.assertRaises(ValueError):
                cli.load(path)

    def test_local_set_rollback_and_dryrun(self):
        """CLI dry run is read-only, local set persists, rollback restores exactly."""
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'config').mkdir()
            for env, source in self.sources.items():
                (Path(directory) / f'config/litellm.{env}.yaml').write_text(yaml.safe_dump(source))
            self.addCleanup(patch.stopall)
            patch.object(cli, 'ROOT', Path(directory)).start()
            path = Path(directory) / 'table.json'
            path.write_text(json.dumps(self.table))
            before = path.read_bytes()
            args = ['--table', str(path), '--env', 'test']
            with contextlib.redirect_stdout(io.StringIO()) as output:
                cli.main(args + ['set', 'gpt-demo', '--primary', 'wokey', '--fallback', 'kie', '--dry-run'])
                self.assertEqual(path.read_bytes(), before)
                self.assertIn('api.wokey.ai', output.getvalue())
                cli.main(args + ['set', 'gpt-demo', '--primary', 'wokey', '--fallback', 'kie'])
                switched = path.read_bytes()
                cli.main(args + ['rollback', '--dry-run'])
                self.assertEqual(path.read_bytes(), switched)
                cli.main(args + ['rollback'])
            self.assertEqual(cli.load(path), self.table)
            self.assertNotIn('previous', cli.load(path))

    def test_prod_group_switch_and_rollback(self):
        """The same CLI switches PROD locally and restores both environment tables."""
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'config').mkdir()
            for env, source in self.sources.items():
                (Path(directory) / f'config/litellm.{env}.yaml').write_text(yaml.safe_dump(source))
            self.addCleanup(patch.stopall)
            patch.object(cli, 'ROOT', Path(directory)).start()
            path = Path(directory) / 'table.json'
            path.write_text(json.dumps(self.table))
            args = ['--table', str(path), '--env', 'prod']
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(args + ['set', 'gpt-demo', '--primary', 'wokey', '--fallback', 'kie'])
                switched = cli.load(path)
                self.assertEqual(switched['environments']['test'], self.table['environments']['test'])
                for route in switched['environments']['prod']['routes']:
                    if route['alias'] in switched['environments']['prod']['groups']['gpt-demo']:
                        self.assertEqual((route['primary'], route['fallback']), ('wokey', ['kie']))
                cli.main(args + ['rollback'])
            self.assertEqual(cli.load(path), self.table)

    def test_two_alias_fallback_cycle(self):
        """Check detects a loop through sibling aliases rather than only self-loops."""
        routes = {r['alias']: r for r in self.table['environments']['test']['routes']}
        routes['gpt-6.1-sol']['backends']['openrouter']['target'] = 'gpt-6-sol'
        routes['gpt-6-sol']['backends']['openrouter']['target'] = 'gpt-6.1-sol'
        with self.assertRaises(ValueError):
            cli.check(self.table, 'test', self.sources['test'])

    def test_invalid_set_does_not_write(self):
        """Failed mapping validation cannot change local catalog/history."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'table.json'
            path.write_text(json.dumps(self.table))
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                cli.main(['--table', str(path), 'set', 'kie-chat', '--primary', 'wokey', '--fallback', 'kie'])
            self.assertEqual(path.read_bytes(), before)

    def test_rollback_without_history(self):
        """One-step rollback reports absence of a previous version."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'table.json'
            path.write_text(json.dumps(self.table))
            with self.assertRaises(ValueError):
                cli.main(['--table', str(path), 'rollback'])

    def test_unknown_fallback_target(self):
        """External targets must resolve through a declared wildcard or alias."""
        route = next(r for r in self.table['environments']['test']['routes'] if r['alias'] == 'gpt-6.1-sol')
        route['backends']['openrouter']['target'] = 'missing/model'
        with self.assertRaises(ValueError):
            cli.check(self.table, 'test', self.sources['test'])

    def test_inline_credential_rejected(self):
        """Catalog rendering accepts credential names only, never secret values."""
        self.table['platforms']['kie']['profiles']['connection-1']['params']['api_key'] = 'dummy-not-a-secret'
        with self.assertRaises(ValueError):
            cli.check(self.table, 'test', self.sources['test'])

    def test_rollback_environment_fence(self):
        """Wrong environment cannot silently restore another environment's edit."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'table.json'
            switched = cli.switch(self.table, 'test', 'gpt-demo', 'wokey', ['kie'])
            cli.save(path, self.table, switched, 'test')
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                cli.main(['--table', str(path), '--env', 'prod', 'rollback'])
            self.assertEqual(path.read_bytes(), before)

    def test_cli_read_commands(self):
        """Status, render and import are local-only and leave the table untouched."""
        before = cli.DEFAULT.read_bytes()
        for command in ('status', 'render', 'import', 'check'):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                cli.main([command])
            self.assertTrue(output.getvalue())
        self.assertEqual(cli.DEFAULT.read_bytes(), before)

    def test_apply_preserves_unrelated_text_and_is_idempotent(self):
        """Apply changes only owned YAML entries; repeated apply and import are no-ops."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            source = copy.deepcopy(self.sources['test'])
            source['general_settings'] = {'unrelated_setting': 'keep'}
            raw = '# keep this heading\n' + cli.dump(source) + '# keep this footer\n'
            path.write_text(raw)
            self.assertEqual(cli.apply(self.table, 'test', path), 0)
            self.assertEqual(path.read_text(), raw)
            candidate = cli.switch(self.table, 'test', 'gpt-demo', 'wokey', ['kie', 'openrouter'])
            cli.apply(candidate, 'test', path)
            result = path.read_text()
            self.assertIn('# keep this heading', result)
            self.assertIn('# keep this footer', result)
            self.assertEqual(yaml.safe_load(result)['general_settings'], source['general_settings'])
            self.assertEqual(cli.apply(candidate, 'test', path), 0)
            inferred = cli.infer({'test': yaml.safe_load(result)})
            self.assertEqual(cli.render(inferred, 'test'), cli.render(candidate, 'test'))
            # A mapping can mix managed and wildcard-owned keys; preserve the latter.
            mixed = yaml.safe_load(result)
            mixed['router_settings']['fallbacks'][0]['openrouter/custom-model'] = ['openrouter/openai/gpt-6-sol']
            path.write_text(cli.dump(mixed))
            cli.apply(candidate, 'test', path)
            self.assertIn({'openrouter/custom-model': ['openrouter/openai/gpt-6-sol']}, yaml.safe_load(path.read_text())['router_settings']['fallbacks'])

    def test_import_rejects_inline_credentials(self):
        """Import never emits literal credential fields from source snapshots."""
        self.sources['test']['model_list'][0]['litellm_params']['api_key'] = 'dummy-not-a-secret'
        with self.assertRaises(ValueError):
            cli.infer(self.sources)


if __name__ == '__main__':
    unittest.main()
