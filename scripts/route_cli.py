#!/usr/bin/env python3
"""Offline routing-table compiler used by gateway maintainers, never a deployer.

Reads YAML snapshots and a single JSON catalog; writes only the catalog when
explicitly requested; apply writes managed YAML fragments. Credentials are names,
not values. DB routes remain owned by deployment reconciliation. PyYAML is already a gateway dependency.
"""
import argparse
import copy
import difflib
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / 'config/routing/routes.json'


def load(path):
    """Read a catalog without losing duplicate JSON keys, which would hide aliases."""
    def pairs(items):
        """Reject duplicate object keys before constructing the catalog."""
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key: ' + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=pairs)


def dump(value):
    """Return deterministic YAML for rendering and semantic patch comparisons."""
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True)


def reject_values(value):
    """Require named credentials everywhere before any catalog data is printed."""
    if isinstance(value, dict):
        if {str(key).lower() for key in value} & {'api_key', 'password', 'authorization', 'aws_secret_access_key', 'vertex_credentials'}:
            raise ValueError('credential values forbidden')
        for child in value.values():
            reject_values(child)
    elif isinstance(value, list):
        for child in value:
            reject_values(child)


def infer(configs):
    """Import read-only YAML snapshots, retaining complete entries and fallback targets.

    Historical OpenRouter fallbacks retain their declared wildcard; current
    routes import without it so retired routing is never reconstructed implicitly.
    Connections are centralized once per platform/profile, not per alias.
    """
    reject_values(configs)
    catalog = {'version': 1, 'platforms': {}, 'environments': {}}
    for env, source in configs.items():
        routes, credentials = [], set()
        wildcard = next((x for x in source['model_list'] if x['model_name'] == 'openrouter/*'), None)
        fallbacks = {k: v for item in source.get('router_settings', {}).get('fallbacks', []) for k, v in item.items()}
        by_name = {entry['model_name']: entry for entry in source['model_list']}
        for entry in source['model_list']:
            if entry['model_name'].startswith('__route__'):
                continue
            params = copy.deepcopy(entry['litellm_params'])
            credential = params.pop('litellm_credential_name', None)
            if not credential or any(k in params for k in ('api_key', 'aws_secret_access_key')):
                raise ValueError('import requires named credentials, never inline secrets')
            credentials.add(credential)
            model = params.pop('model')
            provider = ('wokey' if credential == 'wokey' else 'kie' if credential == 'kie-ai' else 'dashscope' if model.startswith('dashscope/') or 'dashscope' in params.get('api_base', '') else 'openrouter' if model.startswith(('openrouter/', 'openai/')) else 'google')
            adapter = 'responses' if model.startswith('openai/responses/') else params.get('custom_llm_provider', model.split('/')[0])
            connection = {k: params.pop(k) for k in ('api_base', 'custom_llm_provider') if k in params}
            connection['litellm_credential_name'] = credential
            platform = catalog['platforms'].setdefault(provider, {'profiles': {}})
            profile = next((k for k, v in platform['profiles'].items() if v == {'adapter': adapter, 'params': connection}), None)
            if profile is None:
                profile = f'connection-{len(platform["profiles"])+1}'
                platform['profiles'][profile] = {'adapter': adapter, 'params': connection}
            route = {'alias': entry['model_name'], 'primary': provider, 'fallback': [],
                     'entry': {k: v for k, v in entry.items() if k not in ('model_name', 'litellm_params')},
                     'backends': {provider: {'profile': profile, 'model': model, 'params': params}}}
            for target in fallbacks.get(route['alias'], []):
                if target.startswith('__route__'):
                    backend_catalog = infer({env: {'model_list': ([wildcard] if wildcard else []) + [{**by_name[target], 'model_name': target.removeprefix('__route__')}], 'router_settings': {'fallbacks': []}}})
                    imported = backend_catalog['environments'][env]['routes'][-1]
                    backup = imported['primary']
                    original_profile = backend_catalog['platforms'][backup]['profiles'][imported['backends'][backup]['profile']]
                    profiles = catalog['platforms'].setdefault(backup, {'profiles': {}})['profiles']
                    key = next((k for k, v in profiles.items() if v == original_profile), f'connection-{len(profiles)+1}')
                    profiles[key] = original_profile
                    route['fallback'].append(backup)
                    route['backends'][backup] = {**imported['backends'][backup], 'profile': key}
                    credentials.add(original_profile['params']['litellm_credential_name'])
                    continue
                if not target.startswith('openrouter/') or provider == 'openrouter':
                    raise ValueError('unsupported snapshot fallback')
                wp = copy.deepcopy(wildcard['litellm_params'])
                wp.pop('model'); wc = wp.pop('litellm_credential_name')
                conn = {k: wp.pop(k) for k in ('api_base', 'custom_llm_provider') if k in wp}
                conn['litellm_credential_name'] = wc
                profiles = catalog['platforms'].setdefault('openrouter', {'profiles': {}})['profiles']
                key = next((k for k, v in profiles.items() if v['params'] == conn), None)
                if key is None:
                    key = f'connection-{len(profiles)+1}'
                    profiles[key] = {'adapter': 'openrouter', 'params': conn}
                route['fallback'].append('openrouter')
                route['backends']['openrouter'] = {'profile': key, 'model': target, 'params': wp, 'target': target}
            routes.append(route)
        # Reconstruct opt-in DB routes without exporting their private binding.
        for alias, spec in source.get('db_managed_routes', {}).items():
            catalog['platforms'].setdefault('aliyun', {'profiles': {}})['profiles']['private-deepseek'] = {'adapter': 'dashscope', 'params': {}, 'db_source_alias': spec['source_alias']}
            target = fallbacks.get(alias, [])
            if not target:
                routes.append({'alias': alias, 'primary': 'aliyun', 'fallback': [], 'entry': {'model_info': spec['model_info']}, 'backends': {'aliyun': {'profile': 'private-deepseek', 'model': spec['model'], 'params': {}}}})
                continue
            if len(target) != 1 or not target[0].startswith('openrouter/'):
                raise ValueError('private route requires one OpenRouter fallback')
            profile = next(k for k, v in catalog['platforms']['openrouter']['profiles'].items() if v['params'].get('litellm_credential_name') == wildcard['litellm_params']['litellm_credential_name'])
            routes.append({'alias': alias, 'primary': 'aliyun', 'fallback': ['openrouter'], 'entry': {'model_info': spec['model_info']}, 'backends': {
                'aliyun': {'profile': 'private-deepseek', 'model': spec['model'], 'params': {}},
                'openrouter': {'profile': profile, 'model': target[0], 'target': target[0], 'params': {}}}})
        aliases = [r['alias'] for r in routes]
        catalog['environments'][env] = {'credentials': sorted(credentials), 'routes': routes,
            'fallback_order': list(fallbacks),
            'groups': {'kie-chat': [r['alias'] for r in routes if r['primary'] == 'kie' and not r['backends']['kie']['model'].startswith('kie/')],
                       'gpt-demo': [a for a in ('gpt-6.1-sol', 'gpt-6-sol', 'gpt-6-luna') if a in aliases]}}
    return catalog


def render(catalog, env):
    """Compile primary entries and ordered fallbacks using private per-alias targets.

    Shared wildcard targets remain unchanged. Explicit backup deployments get
    unique names, so switching one alias cannot create a cycle through another.
    """
    entries, fallback = [], []
    for route in catalog['environments'][env]['routes']:
        targets = []
        for index, provider in enumerate([route['primary']] + route['fallback']):
            backend = route['backends'][provider]
            name = route['alias'] if index == 0 else f'__route__{route["alias"]}__{provider}'
            if index and backend.get('target'):
                targets.append(backend['target'])
                continue
            if index:
                targets.append(name)
            profile = catalog['platforms'][provider]['profiles'][backend['profile']]
            # Private Aliyun bindings stay DB-owned; apply manages only their fallback.
            if profile.get('db_source_alias'):
                continue
            params = {**backend['params'], **profile['params'], 'model': backend['model']}
            entries.append({'model_name': name, 'litellm_params': params, **copy.deepcopy(route['entry'])})
        if targets:
            fallback.append({route['alias']: targets})
    order = catalog['environments'][env]['fallback_order']
    fallback.sort(key=lambda item: order.index(next(iter(item))) if next(iter(item)) in order else len(order))
    result = {'model_list': entries, 'router_settings': {'fallbacks': fallback}}
    db_routes = {}
    for route in catalog['environments'][env]['routes']:
        backend = route['backends'][route['primary']]
        profile = catalog['platforms'][route['primary']]['profiles'][backend['profile']]
        if profile.get('db_source_alias'):
            db_routes[route['alias']] = {'source_alias': profile['db_source_alias'], 'model': backend['model'], 'model_info': route['entry'].get('model_info', {})}
    if db_routes:
        result['db_managed_routes'] = db_routes
    return result


def check(catalog, env, source):
    """Validate catalog references, credential declarations, graph and YAML alias set.

    This proves declaration existence only: DB credential presence and provider
    capability are deliberately not asserted by an offline check.
    """
    reject_values(catalog)
    config = catalog['environments'][env]
    aliases = [r['alias'] for r in config['routes']]
    if len(aliases) != len(set(aliases)) or any(a.startswith('__route__') for a in aliases):
        raise ValueError('duplicate/reserved alias')
    expected = [e['model_name'] for e in source['model_list'] if not e['model_name'].startswith('__route__')]
    if len(expected) != len(set(expected)) or set(aliases) != set(expected) | set(source.get('db_managed_routes', {})) | set(config.get('additions', [])) | {r['alias'] for r in config['routes'] if any(catalog['platforms'][p]['profiles'][b['profile']].get('db_source_alias') for p, b in r['backends'].items())}:
        raise ValueError('YAML alias set mismatch')
    for members in config['groups'].values():
        if not set(members) <= set(aliases):
            raise ValueError('unknown group member')
    for route in config['routes']:
        chain = [route['primary']] + route['fallback']
        if len(chain) != len(set(chain)):
            raise ValueError('fallback platform cycle/duplicate')
        for provider, backend in route['backends'].items():
            profile = catalog['platforms'][provider]['profiles'][backend['profile']]
            if not profile.get('db_source_alias') and profile['params']['litellm_credential_name'] not in config['credentials']:
                raise ValueError('credential name not declared')
            if not profile['adapter'] or not backend['model']:
                raise ValueError('missing adapter/model')
            if set(backend['params']) & {'model', 'api_base', 'custom_llm_provider', 'litellm_credential_name', 'api_key'}:
                raise ValueError('connection fields belong in platform profile')
        if not set(chain) <= set(route['backends']):
            raise ValueError('platform has no model mapping')
    output = render(catalog, env)
    names = [x['model_name'] for x in output['model_list']]
    names += [r['alias'] for r in config['routes'] if catalog['platforms'][r['primary']]['profiles'][r['backends'][r['primary']]['profile']].get('db_source_alias')]
    if len(names) != len(set(names)):
        raise ValueError('rendered target collision')
    graph = {k: v for item in output['router_settings']['fallbacks'] for k, v in item.items()}
    def visit(node, stack):
        """Reject rendered alias cycles and unresolved wildcard fallback targets."""
        if node in stack:
            raise ValueError('fallback alias cycle')
        if node not in names and not any(n.endswith('*') and node.startswith(n[:-1]) for n in names):
            raise ValueError('unresolved fallback target')
        for target in graph.get(node, []):
            visit(target, stack | {node})
    for node in graph:
        visit(node, set())
    return len(aliases)


def switch(catalog, env, selector, primary, fallback):
    """Change an alias or group on a copy; callers validate before saving locally."""
    result = copy.deepcopy(catalog)
    config = result['environments'][env]
    members = config['groups'].get(selector, [selector])
    selected = [r for r in config['routes'] if r['alias'] in members]
    if not selected:
        raise ValueError('unknown alias/group')
    for route in selected:
        # Kie is optional for aliases whose previous route has no Kie backend.
        route.update(primary=primary, fallback=[p for p in fallback if p != 'kie' or p in route['backends']])
    return result


def save(path, before, after, env):
    """Persist one-step rollback in the same catalog, using atomic local replacement.

    History excludes itself; rollback restores the previous full catalog and
    removes history. Only one previous version is retained by design.
    """
    value = copy.deepcopy(after)
    value.pop('previous', None)
    value.pop('previous_env', None)
    old = copy.deepcopy(before); old.pop('previous', None); old.pop('previous_env', None)
    value['previous_env'] = env
    value['previous'] = old
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    temporary.replace(path)


def apply(catalog, env, path, dry_run=False):
    """Write managed entries/fallbacks atomically while preserving unrelated YAML text.

    CLI callers use node offsets to replace only owned entries, so comments and
    credentials/settings outside these nodes remain byte-for-byte unchanged.
    A catalog imported from the same file is a no-op, including formatting.
    """
    raw = path.read_text()
    source = yaml.safe_load(raw)
    check(catalog, env, source)
    compiled = render(catalog, env)
    if source['model_list'] == compiled['model_list'] and source.get('router_settings', {}).get('fallbacks', []) == compiled['router_settings']['fallbacks'] and source.get('db_managed_routes') == compiled.get('db_managed_routes'):
        return 0
    owned = {r['alias'] for r in catalog['environments'][env]['routes']}
    compiled_by_alias = {}
    for entry in compiled['model_list']:
        alias = entry['model_name']
        if alias.startswith('__route__'):
            alias = alias[len('__route__'):].rsplit('__', 1)[0]
        compiled_by_alias.setdefault(alias, []).append(entry)
    root = yaml.compose(raw)
    def data_end(node):
        """Locate the last scalar line; collection marks include following comments."""
        if isinstance(node, yaml.ScalarNode):
            return node.end_mark.index
        children = [child for pair in node.value for child in pair] if isinstance(node, yaml.MappingNode) else node.value
        return max((data_end(child) for child in children), default=node.end_mark.index)
    def line_end(node):
        """Include a data line's newline without consuming the next YAML key."""
        end = raw.find('\n', data_end(node))
        return len(raw) if end < 0 else end + 1
    edits = []
    for key, node in root.value:
        if key.value == 'model_list':
            for entry_node, entry in zip(node.value, source['model_list']):
                alias = entry['model_name']
                private = alias.startswith('__route__')
                owner = alias[len('__route__'):].rsplit('__', 1)[0] if private else alias
                if owner not in owned:
                    continue
                rows = [] if private else compiled_by_alias.get(owner, [])
                if rows == [entry]:
                    continue
                # Consume just the entry's data lines; following route comments survive.
                start = raw.rfind('\n', 0, entry_node.start_mark.index) + 1
                end = line_end(entry_node)
                indent = ' ' * max(0, entry_node.start_mark.column - 2)
                text = ''.join(indent + line + '\n' for line in dump(rows).splitlines()) if rows else ''
                edits.append((start, end, text))
            additions = [entry for entry in compiled['model_list'] if entry['model_name'] not in {row['model_name'] for row in source['model_list']} and not entry['model_name'].startswith('__route__')]
            for entry in additions:
                # Insert restored DB-to-YAML aliases at their catalog position;
                # order-preserving apply makes repeated apply a true no-op.
                present = {row['model_name']: n for row, n in zip(source['model_list'], node.value)}
                index = compiled['model_list'].index(entry)
                following = next((present[row['model_name']] for row in compiled['model_list'][index + 1:] if row['model_name'] in present), None)
                position = raw.rfind('\n', 0, following.start_mark.index) + 1 if following else line_end(node.value[-1])
                indent = ' ' * max(0, node.value[0].start_mark.column - 2)
                text = ''.join(indent + line + '\n' for line in dump([entry]).splitlines())
                edits.append((position, position, text))
        elif key.value == 'router_settings':
            for router_key, fallback_node in node.value:
                if router_key.value != 'fallbacks':
                    continue
                current = source['router_settings'].get('fallbacks', [])
                desired = compiled['router_settings']['fallbacks']
                # Preserve externally-owned fallback entries and their order.
                external = [{key: value for key, value in item.items() if key not in owned} for item in current]
                replacement = [item for item in external if item] + desired
                if current == replacement:
                    continue
                start = raw.rfind('\n', 0, router_key.start_mark.index) + 1
                end = line_end(fallback_node)
                indent = ' ' * router_key.start_mark.column
                text = ''.join(indent + line + '\n' for line in dump({'fallbacks': replacement}).splitlines())
                edits.append((start, end, text))
    # Replayable private deployments travel with YAML without copying secrets.
    desired_db = compiled.get('db_managed_routes')
    db_node = next(((k, n) for k, n in root.value if k.value == 'db_managed_routes'), None)
    if source.get('db_managed_routes') != desired_db:
        replacement = dump({'db_managed_routes': desired_db}) if desired_db else ''
        if db_node:
            key, node = db_node
            edits.append((raw.rfind('\n', 0, key.start_mark.index) + 1, line_end(node), replacement))
        else:
            edits.append((len(raw), len(raw), '\n' + replacement))
    for start, end, text in sorted(edits, reverse=True):
        raw = raw[:start] + text + raw[end:]
    updated = yaml.safe_load(raw)
    if updated['model_list'] != compiled['model_list']:
        raise ValueError('apply model_list mismatch')
    if dry_run:
        print(''.join(difflib.unified_diff(path.read_text().splitlines(True), raw.splitlines(True), fromfile=str(path), tofile=str(path))), end='')
    elif raw != path.read_text():
        temporary = path.with_suffix('.tmp')
        temporary.write_text(raw)
        temporary.replace(path)
    return len(edits)


def main(argv=None):
    """CLI entry: inspect, edit only local catalog, or print reviewable YAML patch."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', choices=('test', 'prod'), default='test')
    parser.add_argument('--table', type=Path, default=DEFAULT)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('status', 'render', 'check', 'import'):
        sub.add_parser(command)
    apply_parser = sub.add_parser('apply')
    apply_parser.add_argument('--dry-run', action='store_true')
    set_parser = sub.add_parser('set')
    set_parser.add_argument('selector')
    set_parser.add_argument('--primary', required=True)
    set_parser.add_argument('--fallback', default='')
    set_parser.add_argument('--dry-run', action='store_true')
    set_parser.add_argument('--apply', action='store_true', help='also write selected environment YAML')
    rollback = sub.add_parser('rollback')
    rollback.add_argument('--dry-run', action='store_true')
    rollback.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    sources = {env: yaml.safe_load((ROOT / f'config/litellm.{env}.yaml').read_text()) for env in ('test', 'prod')}
    if args.command == 'import':
        print(json.dumps(infer(sources), indent=2, ensure_ascii=False))
        return
    catalog = load(args.table)
    count = check(catalog, args.env, sources[args.env])
    if args.command == 'apply':
        edits = apply(catalog, args.env, ROOT / f'config/litellm.{args.env}.yaml', args.dry_run)
        print(f'{args.env} YAML: {edits} managed replacements; no deployment executed.')
    elif args.command == 'check':
        print(f'OK {args.env}: {count} aliases; credential names declared (values not checked)')
    elif args.command == 'status':
        for r in catalog['environments'][args.env]['routes']:
            print(f'{r["alias"]}: {r["primary"]} -> {",".join(r["fallback"]) or "(none)"}')
    elif args.command == 'render':
        print(dump(render(catalog, args.env)), end='')
    else:
        if args.command == 'set':
            candidate = switch(catalog, args.env, args.selector, args.primary, [x for x in args.fallback.split(',') if x])
        else:
            if 'previous' not in catalog or catalog.get('previous_env') != args.env:
                raise ValueError('no previous version for selected environment')
            candidate = catalog['previous']
        for env in sources:
            check(candidate, env, sources[env])
        before, after = dump(render(catalog, args.env)), dump(render(candidate, args.env))
        print(''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile=f'{args.env}/before', tofile=f'{args.env}/proposed')), end='')
        print('OFFLINE ONLY: review model_list/router_settings patch; register named credentials; align reconcile/key/media validators; deploy via existing environment workflow after approval; verify DB route precedence and real fallback. No deployment executed.')
        if not args.dry_run:
            if args.command == 'set':
                save(args.table, catalog, candidate, args.env)
            else:
                temporary = args.table.with_suffix('.tmp')
                temporary.write_text(json.dumps(candidate, indent=2, ensure_ascii=False) + '\n')
                temporary.replace(args.table)
            if args.apply:
                apply(candidate, args.env, ROOT / f'config/litellm.{args.env}.yaml')
            print('Local catalog/YAML updated; no deployment executed.' if args.apply else 'Local catalog updated; gateway unchanged.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, TypeError):
        # Do not echo malformed input: it may contain an accidentally pasted secret.
        print('ERROR: invalid catalog/input; inspect local declarations (no values echoed).', file=sys.stderr)
        sys.exit(1)
