"""Validated static-data publication. Python standard library only."""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

ROOT = Path(__file__).resolve().parents[1]
VERSION = '1.0.0'
STATUSES = {'valid_quote', 'stale_quote', 'plan_field_missing', 'request_failed',
            'access_blocked', 'country_mismatch', 'amount_invalid',
            'period_unverified', 'unverified_range', 'not_attempted'}


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def read(path, fallback=None):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).is_file() else fallback


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(path, *args, check=True, env=None):
    result = subprocess.run(['git', '-C', str(path), *args], text=True, capture_output=True, env=env)
    if check and result.returncode:
        raise ValueError(f'git {args[0]} failed: {result.stderr.strip()}')
    return result.stdout.strip() if result.returncode == 0 else None


def current_head(path):
    return git(path, 'rev-parse', 'HEAD', check=False)


def safe_path(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or '..' in p.parts or '\\' in value or ':' in value:
        raise ValueError(f'unsafe relative path: {value}')
    return p


def positive(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:\.\d+)?', value):
        return False
    try:
        return Decimal(value).is_finite() and Decimal(value) > 0
    except InvalidOperation:
        return False


def validate(latest, coverage, fx, regions, plans, production=True):
    if any(doc.get('schema_version') != VERSION for doc in (latest, coverage, fx, regions, plans)):
        raise ValueError('unrecognized data schema version')
    targets = {r['target_id']: r for r in regions['regions']}
    if len(targets) != len(regions['regions']):
        raise ValueError('duplicate catalog target')
    plan_ids = {p['plan_id'] for p in plans['plans']}
    plan_keys = {p['plan_id']: p.get('source_plan_key') for p in plans['plans']}
    if len(plan_ids) != len(plans['plans']) or plan_ids != {'go', 'plus', 'pro_100', 'pro_200', 'pro_500'}:
        raise ValueError('unexpected plan mapping')
    expected = {f'{r["config_id"]}:web:{p}:month:standard' for r in targets.values() for p in plan_ids}
    cells = {c['cell_id']: c for c in coverage['cells']}
    if len(cells) != len(coverage['cells']) or cells.keys() != expected:
        raise ValueError('coverage must contain exactly one result per catalog target and plan')
    quotes = {q['record_id']: q for q in latest['quotes']}
    if len(quotes) != len(latest['quotes']):
        raise ValueError('duplicate quote')
    if latest['data_snapshot_id'] != coverage['data_snapshot_id']:
        raise ValueError('snapshot identity mismatch')
    if latest['catalog_version'] != regions['catalog_version'] or coverage['catalog_version'] != regions['catalog_version']:
        raise ValueError('catalog version mismatch')
    if latest['fx_snapshot_id'] != fx['fx_snapshot_id']:
        raise ValueError('FX snapshot identity mismatch')
    rates = fx.get('rates', [])
    rate_codes = {r['quote'] for r in rates}
    if len(rate_codes) != len(rates) or any(not positive(r['rate']) for r in rates):
        raise ValueError('invalid or duplicate FX rates')
    if fx['fx_snapshot_id'] and not any(r['quote'] == 'USD' and Decimal(r['rate']) == 1 for r in rates):
        raise ValueError('USD base must explicitly equal one')
    for cell in cells.values():
        if cell['status'] not in STATUSES or cell['attempt_status'] not in STATUSES - {'stale_quote'}:
            raise ValueError('invalid coverage status')
        target = targets.get(cell['target_id'])
        if not target or target['config_id'] != cell['config_id'] or cell['plan_id'] not in plan_ids:
            raise ValueError('cell identity mismatch')
        if cell['cell_id'] != f'{cell["config_id"]}:web:{cell["plan_id"]}:month:standard':
            raise ValueError('cell plan and logical key mismatch')
        if cell['status'] in {'valid_quote', 'stale_quote'}:
            quote = quotes.get(cell['quote_record_id'])
            if not quote or quote['record_id'] != cell['cell_id']:
                raise ValueError('quoted cell has no matching record')
        elif cell.get('quote_record_id') is not None:
            raise ValueError('unquoted cell unexpectedly references a quote')
    for key, q in quotes.items():
        if key not in cells or cells[key]['quote_record_id'] != key:
            raise ValueError('orphan quote')
        if q['target_id'] != cells[key]['target_id'] or q['config_id'] != cells[key]['config_id'] or q['plan_id'] != cells[key]['plan_id'] or q['source_plan_key'] != plan_keys[q['plan_id']]:
            raise ValueError('quote plan or config identity mismatch')
        if not positive(q['display_price']['amount']) or q['display_price']['tax'] not in {'inclusive', 'exclusive', 'unknown'}:
            raise ValueError('invalid display amount/tax')
        if q['channel'] != 'web' or q['billing_interval'] != 'month' or q['offer_type'] != 'standard':
            raise ValueError('unverified offer in quote table')
        if q['country_code'] != targets[q['target_id']]['country_code'] or q['availability']['purchase_verified'] is not False:
            raise ValueError('country or availability contract violation')
        if not re.fullmatch(r'[A-Z]{3}', q['currency']) or not re.fullmatch(r'[a-f0-9]{64}', q['source']['body_sha256']):
            raise ValueError('currency/evidence malformed')
        for estimate in q.get('estimates', {}).values():
            if estimate and (not positive(estimate['amount']) or estimate['fx_snapshot_id'] != fx['fx_snapshot_id']):
                raise ValueError('invalid estimate or mixed FX set')
    summary = coverage['summary']
    counts = dict(collections.Counter(c['status'] for c in cells.values()))
    attempted = len({c['target_id'] for c in cells.values() if c['last_attempt_at'] is not None})
    successful = {c['target_id'] for c in cells.values() if c.get('http_status') == 200 and c['attempt_status'] in {'valid_quote', 'plan_field_missing', 'amount_invalid'}}
    complete = collections.Counter(c['target_id'] for c in cells.values() if c['status'] == 'valid_quote')
    derived = {'target_count': len(targets), 'country_target_count': sum(bool(r.get('is_country_directory')) for r in targets.values()),
               'auxiliary_target_count': sum(not bool(r.get('is_country_directory')) for r in targets.values()),
               'expected_request_count': len(targets), 'attempted_request_count': attempted,
               'not_attempted_request_count': len(targets) - attempted, 'successful_response_count': len(successful),
               'expected_cell_count': len(cells), 'status_counts': counts,
               'current_quote_cell_count': counts.get('valid_quote', 0), 'historical_quote_cell_count': counts.get('stale_quote', 0),
               'complete_five_plan_target_count': sum(v == len(plan_ids) for v in complete.values())}
    for field, value in derived.items():
        if summary.get(field) != value:
            raise ValueError(f'coverage totals do not match cells: {field}')
    if production and not quotes:
        raise ValueError('no valid snapshot: refusing empty publication')
    return True


def previous_state(state_dir):
    folder = Path(state_dir) / 'state'
    if (folder / 'latest.json').is_file():
        return folder, read(folder / 'latest.json'), read(folder / 'coverage.json'), read(folder / 'fx.json')
    public = ROOT / 'public/data'
    manifest = read(public / 'manifest.json')
    if manifest:
        entries = manifest['entrypoints']
        return public, read(public / entries['latest']), read(public / entries['coverage']), read(public / entries['fx'])
    return folder, None, None, None


def publish_files(state, public_dir, evidence_dir=None):
    """Write immutable files first; manifest last. Never overwrite old version files."""
    public = Path(public_dir)
    public.mkdir(parents=True, exist_ok=True)
    snapshot = read(state / 'latest.json')
    entries, files = {}, []
    names = {'latest': 'latest.json', 'coverage': 'coverage.json', 'fx': 'fx.json',
             'reference_comparison': 'reference-comparison.json', 'regions': 'regions.json',
             'plans': 'plans.json', 'status': 'status.json'}
    for key, name in names.items():
        source = state / name
        if not source.is_file():
            raise ValueError(f'missing publish input {name}')
        rel = f'{Path(name).stem}.{digest(source)[:16]}.json'
        shutil.copyfile(source, public / rel)
        entries[key] = rel
        files.append({'path': rel, 'sha256': digest(source), 'bytes': source.stat().st_size, 'media_type': 'application/json'})
    if evidence_dir and Path(evidence_dir).is_dir():
        for source in sorted(Path(evidence_dir).glob('*.json')):
            if source.stem != digest(source):
                raise ValueError('evidence filename is not its response byte SHA256')
            rel = f'evidence/{source.name}'
            (public / 'evidence').mkdir(exist_ok=True)
            shutil.copyfile(source, public / rel)
            files.append({'path': rel, 'sha256': digest(source), 'bytes': source.stat().st_size, 'media_type': 'application/json'})
    manifest = {k: snapshot[k] for k in ('schema_version', 'data_snapshot_id', 'generated_at', 'catalog_version', 'fx_snapshot_id')}
    manifest.update(entrypoints=entries, files=files)
    write(public / 'manifest.json', manifest)
    return manifest


def candidate_hashes(candidate):
    return {str(p.relative_to(candidate)).replace('\\', '/'): digest(p)
            for folder in ('state', 'evidence')
            for p in sorted((candidate / folder).rglob('*')) if p.is_file()}


def collector_signature():
    files = ('scripts/collect.py', 'scripts/pricing.py', 'scripts/fx.py', 'docs/CONTRACT.md')
    return hashlib.sha256(''.join(digest(ROOT / name) for name in files).encode()).hexdigest()


def restore_progress(state_dir, checkpoint):
    progress = read(Path(state_dir) / 'progress/receipt.json')
    if not progress or progress.get('collector_signature') != collector_signature():
        return False
    if progress.get('catalog_sha256') != digest(ROOT / 'config/regions.json') or progress.get('plans_sha256') != digest(ROOT / 'config/plans.json'):
        return False
    saved_at = datetime.fromisoformat(progress['saved_at'].replace('Z', '+00:00'))
    if not 0 <= (datetime.now(timezone.utc) - saved_at).total_seconds() < 72 * 3600:
        return False
    saved_checkpoint = Path(state_dir) / 'progress/checkpoint.json'
    if not saved_checkpoint.is_file():
        return False
    shutil.copyfile(saved_checkpoint, checkpoint)
    return True


def prepare(args):
    from collect import collect
    from fx import fetch_fx, apply_estimates
    from reconcile_reference import reconcile

    candidate = Path(args.candidate_dir)
    if (candidate / 'build-receipt.json').exists():
        (candidate / 'build-receipt.json').unlink()
    checkpoint = candidate / 'collection-checkpoint.json'
    if checkpoint.exists() and not args.resume:
        checkpoint.unlink()  # a new refresh must not re-label the previous run's attempts
    state = candidate / 'state'
    state.mkdir(parents=True, exist_ok=True)
    evidence = candidate / 'evidence'
    evidence.mkdir(exist_ok=True)
    regions, plans = read(ROOT / 'config/regions.json'), read(ROOT / 'config/plans.json')
    old_dir, old, old_coverage, old_fx = previous_state(args.state_dir)
    if args.fx_seed:
        seed = read(args.fx_seed)
        source_hash = (seed or {}).get('source', {}).get('body_sha256', '')
        seed_evidence = evidence / f'{source_hash}.json'
        rates = (seed or {}).get('rates', [])
        if not seed or not seed.get('fx_snapshot_id') or seed.get('base') != 'USD' or not seed_evidence.is_file() or digest(seed_evidence) != source_hash or not all(positive(r['rate']) for r in rates) or not any(r['quote'] == 'USD' and Decimal(r['rate']) == 1 for r in rates):
            raise ValueError('FX recovery requires a real coherent snapshot and matching exact-byte evidence')
        old_fx = seed
    write(candidate / 'prepare-receipt.json', {'state_head': current_head(args.state_dir), 'source_head': current_head(ROOT), 'started_at': utcnow()})
    for previous_evidence in (Path(args.state_dir) / 'evidence', ROOT / 'public/data/evidence'):
        if previous_evidence.is_dir():
            shutil.copytree(previous_evidence, evidence, dirs_exist_ok=True)
    mode = args.mode
    if old is None or old.get('catalog_version') != regions['catalog_version'] or not old_coverage:
        mode = 'refresh'
    old_plans = read(old_dir / 'plans.json')
    if old_plans and old_plans != plans:
        mode = 'refresh'
    if os.environ.get('GITHUB_ACTIONS') == 'true' and current_head(args.state_dir) is None:
        mode = 'refresh'  # initial runner connectivity must be measured, not inferred from a seed
    if mode == 'reuse':
        latest, coverage, fx = old, old_coverage, old_fx
        status = read(old_dir / 'status.json', {'result': 'reused', 'last_run_at': utcnow()})
        reviews = []
    else:
        if not args.resume:
            restore_progress(args.state_dir, checkpoint)
        approvals = read(ROOT / 'config/accepted-changes.json', {'body_sha256': []})
        result = collect(regions['regions'], plans['plans'], old['quotes'] if old else [], evidence,
                         budget_seconds=args.budget_seconds, concurrency=2,
                         checkpoint_path=candidate / 'collection-checkpoint.json',
                         checkpoint_max_age_seconds=72 * 3600,
                         accepted_change_hashes=set(approvals.get('body_sha256', [])))
        generated = utcnow()
        identity = hashlib.sha256(json.dumps({'time': generated, 'cells': result['cells'], 'quotes': result['quotes']}, sort_keys=True).encode()).hexdigest()[:24]
        latest = {'schema_version': VERSION, 'data_snapshot_id': identity, 'generated_at': generated,
                  'catalog_version': regions['catalog_version'], 'fx_snapshot_id': None,
                  'collector_version': VERSION, 'quotes': result['quotes']}
        coverage = {k: latest[k] for k in ('schema_version', 'data_snapshot_id', 'generated_at', 'catalog_version')}
        coverage.update(summary=result['summary'], cells=result['cells'])
        fx = fetch_fx(old_fx, evidence, currencies={q['currency'] for q in latest['quotes']} | {'CNY'})
        latest['fx_snapshot_id'] = fx['fx_snapshot_id']
        estimated = apply_estimates(latest['quotes'], fx)
        if estimated is not None:
            latest['quotes'] = estimated
        reviews = result.get('change_reviews', [])
        status = {'schema_version': VERSION, 'data_snapshot_id': identity, 'result': 'candidate', 'last_run_at': generated,
                  'source_status': result.get('source_status'), 'coverage': result['summary'],
                  'deployment': {'state': 'not_deployed', 'note': 'Consult the Pages deployment run and live manifest for the deployed version.'}}
    for name, doc in [('latest', latest), ('coverage', coverage), ('fx', fx), ('status', status), ('regions', regions), ('plans', plans)]:
        write(state / f'{name}.json', doc)
    baseline = read(ROOT / 'config/reference-coverage.json', {'status': 'not_verified', 'entries': []})
    comparison = reconcile(regions, plans, coverage['cells'], baseline)
    write(state / 'reference-comparison.json', comparison)
    write(state / 'change-reviews.json', reviews)
    # Diagnostics are durable artifacts even when publication is refused.
    failures = []
    try:
        validate(latest, coverage, fx, regions, plans)
    except (ValueError, KeyError, TypeError) as exc:
        failures.append(str(exc))
    if mode == 'refresh':
        current = {c['cell_id'] for c in coverage['cells'] if c['status'] == 'valid_quote'}
        previous = {c['cell_id'] for c in (old_coverage or {}).get('cells', []) if c['status'] == 'valid_quote'}
        if not current:
            failures.append('all_sources_failed: no current validated quote; existing deployment is preserved')
        if not old and coverage['summary']['not_attempted_request_count']:
            failures.append('initial_full_catalog_incomplete')
        if previous and len(current & previous) / len(previous) < 0.7:
            failures.append('coverage_drop: <70% of previously valid targets refreshed')
    if failures:
        status.update(result='rejected', failures=failures)
        write(state / 'status.json', status)
        print(json.dumps({'result': 'rejected', 'diagnostics': str(candidate), 'failures': failures}, ensure_ascii=False))
        return 2
    status['result'] = 'validated' if mode == 'refresh' else 'reused'
    write(state / 'status.json', status)
    manifest = publish_files(state, args.public_dir, evidence)
    write(candidate / 'validated.json', {'data_snapshot_id': latest['data_snapshot_id'], 'manifest_sha256': digest(Path(args.public_dir) / 'manifest.json'), 'mode': mode,
          'candidate_hashes': candidate_hashes(candidate)})
    print(json.dumps({'result': status['result'], 'snapshot': latest['data_snapshot_id'], 'coverage': coverage['summary'], 'files': len(manifest['files'])}, ensure_ascii=False))
    return 0


def verify_dist(args):
    dist = Path(args.dist_dir)
    html = (dist / 'index.html').read_text(encoding='utf-8')
    base = os.environ.get('VITE_BASE', '/chatgpt-regional-prices/')
    resources = re.findall(r'(?:src|href)=["\']([^"\']+)["\']', html)
    for resource in resources:
        if resource.startswith(('https:', 'data:', '#')):
            continue
        if not resource.startswith(base):
            raise ValueError(f'asset bypasses project base: {resource}')
        if not (dist / resource.removeprefix(base)).is_file():
            raise ValueError(f'missing built resource: {resource}')
    public = dist / 'data'
    manifest = read(public / 'manifest.json')
    if not manifest:
        raise ValueError('production build has no validated data manifest')
    for item in manifest['files']:
        file = public / str(safe_path(item['path']))
        if not file.is_file() or file.stat().st_size != item['bytes'] or digest(file) != item['sha256']:
            raise ValueError(f'published file integrity mismatch: {item["path"]}')
    e = manifest['entrypoints']
    latest, coverage, fx, regions, plans = [read(public / e[k]) for k in ('latest', 'coverage', 'fx', 'regions', 'plans')]
    validate(latest, coverage, fx, regions, plans)
    if latest['data_snapshot_id'] != manifest['data_snapshot_id'] or fx['fx_snapshot_id'] != manifest['fx_snapshot_id']:
        raise ValueError('manifest identifies another snapshot')
    for quote in latest['quotes']:
        for source in [quote['source'], quote['display_price'].get('source'), (quote.get('psp_override') or {}).get('source')]:
            if source and not (public / 'evidence' / f'{source["body_sha256"]}.json').is_file():
                raise ValueError('quote evidence missing from publication')
    smoke_project_path(dist, base, resources, manifest)
    candidate = Path(args.candidate_dir)
    if (candidate / 'validated.json').is_file():
        if read(candidate / 'validated.json')['manifest_sha256'] != digest(public / 'manifest.json'):
            raise ValueError('build and candidate manifest differ')
        write(candidate / 'build-receipt.json', {'manifest_sha256': digest(public / 'manifest.json'),
              'data_snapshot_id': latest['data_snapshot_id'], 'source_head': current_head(ROOT), 'verified_at': utcnow()})
    print(f'Verified project-path assets, {len(coverage["cells"])} cells, snapshot {latest["data_snapshot_id"]}')
    return 0


def smoke_project_path(dist, base, resources, manifest):
    """Exercise HTTP loading at the project mount, including query-share URLs."""
    if not base.startswith('/') or not base.endswith('/'):
        raise ValueError('VITE_BASE must be an absolute directory path')

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if not parsed.path.startswith(base):
                self.send_error(404)
                return
            self.path = '/' + parsed.path[len(base):]
            super().do_GET()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(dist.resolve())))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    paths = [base + '?plan=pro_500&currency=CNY&sort=asc', base + 'data/manifest.json']
    paths += [p for p in resources if p.startswith(base)]
    paths += [base + 'data/' + p for p in manifest['entrypoints'].values()]
    try:
        for path in paths:
            with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}{path}', timeout=5) as response:
                if response.status != 200 or not response.read():
                    raise ValueError(f'project-path HTTP smoke failed: {path}')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def auth_environment(helper):
    helper.write_text('#!/bin/sh\ncase "$1" in *Username*) printf "%s" "x-access-token";; *) printf "%s" "$GITHUB_TOKEN";; esac\n', encoding='utf-8')
    helper.chmod(0o700)
    return dict(os.environ, GIT_ASKPASS=str(helper.resolve()), GIT_TERMINAL_PROMPT='0')


def save_progress_files(candidate, state_dir, coverage):
    folder = state_dir / 'progress'
    folder.mkdir(exist_ok=True)
    shutil.copyfile(candidate / 'collection-checkpoint.json', folder / 'checkpoint.json')
    write(folder / 'coverage.json', coverage)
    write(folder / 'receipt.json', {'saved_at': utcnow(), 'catalog_sha256': digest(ROOT / 'config/regions.json'),
          'plans_sha256': digest(ROOT / 'config/plans.json'), 'source_head': current_head(ROOT),
          'collector_signature': collector_signature(),
          'unvalidated': True, 'note': 'Incomplete acquisition only; never a deployable snapshot.'})


def clear_completed_progress(state_dir):
    for name in ('checkpoint.json', 'coverage.json', 'receipt.json'):
        path = state_dir / 'progress' / name
        if not path.resolve().is_relative_to(state_dir.resolve()):
            raise ValueError('progress path escaped state directory')
        path.unlink(missing_ok=True)


def checkout_state(args):
    repository = os.environ.get('GITHUB_REPOSITORY', 'ggbo/chatgpt-regional-prices')
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repository):
        raise ValueError('invalid repository identity')
    target = Path(args.state_dir)
    if target.exists() and any(target.iterdir()):
        raise ValueError('state checkout directory must be empty')
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='regional-prices-auth-') as temp:
        env = auth_environment(Path(temp) / 'askpass')
        remote = f'https://github.com/{repository}.git'
        git(target, 'init', '-b', 'data')
        git(target, 'config', 'core.autocrlf', 'false')
        git(target, 'remote', 'add', 'origin', remote)
        refs = git(target, 'ls-remote', '--heads', 'origin', 'refs/heads/data', env=env)
        if refs:
            git(target, 'fetch', '--depth=1', 'origin', 'data', env=env)
            git(target, 'checkout', '-B', 'data', 'FETCH_HEAD')
    print('Restored persisted state' if current_head(target) else 'Data branch has no baseline; a full scan is required')
    return 0


def persist(args):
    candidate, state_dir = Path(args.candidate_dir), Path(args.state_dir)
    receipt, built = read(candidate / 'prepare-receipt.json'), read(candidate / 'build-receipt.json')
    validated = read(candidate / 'validated.json')
    if not receipt or not built or not validated or built['manifest_sha256'] != validated['manifest_sha256']:
        raise ValueError('a validated candidate and matching successful build receipt are required')
    if validated.get('candidate_hashes') != candidate_hashes(candidate):
        raise ValueError('candidate bytes changed after validation')
    if receipt['source_head'] != current_head(ROOT) or built['source_head'] != current_head(ROOT):
        raise ValueError('source changed since preparation/build')
    if receipt['state_head'] != current_head(state_dir):
        raise ValueError('local persisted head changed; rerun')
    if git(state_dir, 'status', '--porcelain'):
        raise ValueError('state worktree was dirty before persistence')
    latest = read(candidate / 'state/latest.json')
    if latest['data_snapshot_id'] != built['data_snapshot_id']:
        raise ValueError('candidate changed after build')
    with tempfile.TemporaryDirectory(prefix='regional-prices-auth-') as temp:
        env = auth_environment(Path(temp) / 'askpass')
        remote_ref = git(state_dir, 'ls-remote', '--heads', 'origin', 'refs/heads/data', env=env)
        remote_head = remote_ref.split()[0] if remote_ref else None
        if remote_head != receipt['state_head']:
            raise ValueError('remote data head changed; refusing overwrite, rerun')
        old = read(state_dir / 'state/latest.json')
        old_prices = {q['record_id']: (q['currency'], q['display_price']['amount'], q['display_price']['tax']) for q in (old or {}).get('quotes', [])}
        changed = [q for q in latest['quotes'] if old_prices.get(q['record_id']) != (q['currency'], q['display_price']['amount'], q['display_price']['tax'])]
        for name in ('state', 'evidence'):
            shutil.copytree(candidate / name, state_dir / name, dirs_exist_ok=True)
        coverage = read(candidate / 'state/coverage.json')
        if validated['mode'] == 'refresh':
            if coverage['summary']['not_attempted_request_count']:
                save_progress_files(candidate, state_dir, coverage)
            else:
                clear_completed_progress(state_dir)
        if changed:
            history = state_dir / 'history' / latest['generated_at'][:7] / f'{latest["data_snapshot_id"]}.json.gz'
            history.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(history, 'wt', encoding='utf-8') as stream:
                json.dump({'data_snapshot_id': latest['data_snapshot_id'], 'observed_at': latest['generated_at'], 'changes': changed}, stream, ensure_ascii=False)
        git(state_dir, 'add', '--', 'state', 'evidence')
        if (state_dir / 'history').exists():
            git(state_dir, 'add', '--', 'history')
        if (state_dir / 'progress').exists():
            git(state_dir, 'add', '-A', '--', 'progress')
        staged = git(state_dir, 'diff', '--cached', '--name-only')
        if not staged:
            print('Persisted state is unchanged')
            return 0
        if any(p.split('/')[0] not in {'state', 'evidence', 'history', 'progress'} for p in staged.splitlines()):
            raise ValueError('non-whitelisted staged path')
        git(state_dir, 'config', 'user.name', 'github-actions[bot]')
        git(state_dir, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
        git(state_dir, 'commit', '-m', f'Keep verified regional observations recoverable\n\nSnapshot {latest["data_snapshot_id"]}; Pages deployment is recorded separately.\n\nConfidence: high\nScope-risk: narrow\nTested: Candidate integrity and project-path production build')
        git(state_dir, 'push', 'origin', 'HEAD:refs/heads/data', env=env)
    print(f'Persisted {latest["data_snapshot_id"]}; deployment remains a separate outcome')
    return 0


def persist_progress(args):
    """Save an incomplete acquisition cycle without replacing validated state."""
    candidate, state_dir = Path(args.candidate_dir), Path(args.state_dir)
    coverage = read(candidate / 'state/coverage.json')
    checkpoint = candidate / 'collection-checkpoint.json'
    if not coverage or not checkpoint.is_file() or not coverage['summary']['not_attempted_request_count']:
        print('No incomplete collection cycle to checkpoint')
        return 0
    receipt = read(candidate / 'prepare-receipt.json')
    if not receipt or current_head(state_dir) != receipt['state_head'] or git(state_dir, 'status', '--porcelain'):
        raise ValueError('state checkout changed before progress persistence')
    with tempfile.TemporaryDirectory(prefix='regional-prices-auth-') as temp:
        env = auth_environment(Path(temp) / 'askpass')
        refs = git(state_dir, 'ls-remote', '--heads', 'origin', 'refs/heads/data', env=env)
        remote_head = refs.split()[0] if refs else None
        if remote_head != receipt['state_head']:
            raise ValueError('remote state changed; refusing progress overwrite')
        save_progress_files(candidate, state_dir, coverage)
        shutil.copytree(candidate / 'evidence', state_dir / 'evidence', dirs_exist_ok=True)
        git(state_dir, 'add', '--', 'progress', 'evidence')
        staged = git(state_dir, 'diff', '--cached', '--name-only')
        if any(p.split('/')[0] not in {'progress', 'evidence'} for p in staged.splitlines()):
            raise ValueError('non-progress path staged')
        git(state_dir, 'config', 'user.name', 'github-actions[bot]')
        git(state_dir, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
        git(state_dir, 'commit', '-m', 'Let the next runner continue unfinished catalog acquisition\n\nUnvalidated progress is isolated from deployable state.\n\nScope-risk: narrow\nTested: Checkpoint/catalog identity; no validated state overwritten')
        git(state_dir, 'push', 'origin', 'HEAD:refs/heads/data', env=env)
    print('Saved incomplete cycle; next runner resumes observations and unfinished targets')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--mode', choices=['refresh', 'reuse'], required=True)
    p.add_argument('--state-dir', default='data-state')
    p.add_argument('--candidate-dir', default='work/candidate')
    p.add_argument('--public-dir', default='public/data')
    p.add_argument('--budget-seconds', type=float, default=900)
    p.add_argument('--resume', action='store_true', help='Resume the same interrupted collection; never use for a new daily run')
    p.add_argument('--fx-seed', help='Recover a prior real FX snapshot; exact-byte evidence must already be in candidate/evidence')
    p = sub.add_parser('verify-dist')
    p.add_argument('--dist-dir', default='dist')
    p.add_argument('--candidate-dir', default='work/candidate')
    p = sub.add_parser('checkout-state')
    p.add_argument('--state-dir', default='data-state')
    for command in ('persist', 'persist-progress'):
        p = sub.add_parser(command)
        p.add_argument('--state-dir', default='data-state')
        p.add_argument('--candidate-dir', default='work/candidate')
    args = parser.parse_args()
    try:
        return {'prepare': prepare, 'verify-dist': verify_dist, 'checkout-state': checkout_state, 'persist': persist, 'persist-progress': persist_progress}[args.command](args)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'Publication refused: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
