"""Durable, fair, serial acquisition scheduling, separate from valid prices."""
from __future__ import annotations

import collections
import copy
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def policy_ledger(regions, plans, previous, policy, now):
    plans_signature = hashlib.sha256(json.dumps([(p['plan_id'], p['source_plan_key'], p['billing_interval']) for p in plans], sort_keys=True).encode()).hexdigest()
    compatible = previous and previous.get('policy_revision') == policy['policy_revision'] and previous.get('plans_signature') == plans_signature
    ledger = copy.deepcopy(previous) if compatible else {
        'schema_version': '1.0.0', 'policy_revision': policy['policy_revision'],
        'plans_signature': plans_signature, 'bootstrap': {'started_at': now, 'completed_at': None}, 'targets': {}}
    targets = {}
    for region in regions:
        old = ledger['targets'].get(region['target_id'], {})
        targets[region['target_id']] = old if old.get('config_id') == region['config_id'] else {
            'config_id': region['config_id'], 'bootstrap_attempted': False,
            'last_attempt_at': None, 'last_success_at': None, 'last_attempt_status': 'not_attempted',
            'last_http_status': None, 'last_run_id': None}
    ledger['targets'] = targets
    ledger['catalog_version'] = regions[0].get('catalog_version') if regions else None
    ledger['updated_at'] = now
    ledger['target_count'] = len(targets)
    ledger['plan_count'] = len(plans)
    if any(not value['bootstrap_attempted'] for value in targets.values()):
        ledger['bootstrap']['completed_at'] = None
    return ledger


def select_targets(regions, plans, previous, policy, now, force_full=False):
    ledger = policy_ledger(regions, plans, previous, policy, now)
    pending = [r for r in regions if not ledger['targets'][r['target_id']]['bootstrap_attempted']]
    bootstrap = bool(pending)
    if force_full:
        selected = list(regions)
    elif bootstrap:
        selected = list(regions)  # same catalog signature lets an interrupted bootstrap restore its checkpoint
    else:
        size = max(1, math.ceil(len(regions) * policy['schedule_interval_hours'] / policy['target_cycle_hours']))
        # Missing attempts first, then oldest, then country/config ID. Failures do not starve other targets.
        first = datetime.min.replace(tzinfo=timezone.utc)
        selected = sorted(regions, key=lambda r: (parse_time(ledger['targets'][r['target_id']]['last_attempt_at']) or first, r['target_id']))[:size]
    return {'ledger': ledger, 'targets': selected, 'bootstrap': bootstrap,
            'mode': 'bootstrap' if bootstrap else 'full' if force_full else 'rotation'}


def record_attempts(selection, cells, policy, started_at, completed_at, run_id):
    ledger = copy.deepcopy(selection['ledger'])
    by_target = collections.defaultdict(list)
    for cell in cells:
        by_target[cell['target_id']].append(cell)
    attempted, not_started, resumed = [], [], []
    for region in selection['targets']:
        key = region['target_id']
        target_cells = by_target.get(key, [])
        real = [c for c in target_cells if c.get('last_attempt_at') and c['attempt_status'] != 'not_attempted']
        if not real:
            not_started.append(key)
            continue
        entry = ledger['targets'][key]
        observed = max((c['last_attempt_at'] for c in real), key=parse_time)
        if entry.get('last_attempt_at') == observed:
            resumed.append(key)
            continue
        attempted.append(key)
        entry.update(bootstrap_attempted=True, last_attempt_at=observed,
                     last_attempt_status=real[0]['attempt_status'], last_http_status=real[0].get('http_status'), last_run_id=run_id)
        successful = [c for c in real if c['status'] == 'valid_quote' and c.get('last_success_at')]
        # One accepted plan is not a complete five-plan price refresh.
        if len(successful) == ledger['plan_count']:
            entry['last_success_at'] = min((c['last_success_at'] for c in successful), key=parse_time)
    ledger['updated_at'] = completed_at
    if ledger['targets'] and all(e['bootstrap_attempted'] for e in ledger['targets'].values()) and not ledger['bootstrap']['completed_at']:
        ledger['bootstrap']['completed_at'] = completed_at
    metrics = window_metrics(ledger, policy, completed_at)
    observed_times = [c['last_attempt_at'] for c in cells if c.get('last_attempt_at')]
    run = {'schema_version': '1.0.0', 'run_id': run_id, 'policy_revision': policy['policy_revision'],
           'mode': selection['mode'], 'started_at': started_at, 'completed_at': completed_at,
           'selected_target_ids': [r['target_id'] for r in selection['targets']],
           'attempted_target_ids': attempted, 'not_started_target_ids': not_started, 'resumed_target_ids': resumed,
           'accepted_valid_cell_count': sum(c['status'] == 'valid_quote' for c in cells),
           'observation_started_at': min(observed_times, key=parse_time) if observed_times else None,
           'observation_completed_at': max(observed_times, key=parse_time) if observed_times else None,
           'status_counts': dict(collections.Counter(c['status'] for c in cells)), 'window': metrics}
    return ledger, run


def window_metrics(ledger, policy, now):
    clock = parse_time(now)
    cutoff = clock - timedelta(hours=policy['window_hours'])
    targets = ledger['targets']
    recent = [key for key, value in targets.items() if value.get('last_attempt_at') and parse_time(value['last_attempt_at']) >= cutoff]
    successes = [key for key, value in targets.items() if value.get('last_success_at') and parse_time(value['last_success_at']) >= cutoff]
    oldest = min((v['last_attempt_at'] for v in targets.values() if v.get('last_attempt_at')), default=None, key=parse_time)
    age = (clock - parse_time(oldest)).total_seconds() / 3600 if oldest else None
    return {'window_hours': policy['window_hours'], 'target_count': len(targets),
            'attempted_within_window_count': len(recent), 'successful_within_window_count': len(successes),
            'overdue_target_ids': sorted(set(targets) - set(recent)), 'overdue_target_count': len(targets) - len(recent),
            'oldest_attempt_at': oldest, 'oldest_attempt_age_hours': age,
            'bootstrap_remaining_count': sum(not v['bootstrap_attempted'] for v in targets.values()),
            'bootstrap_completed_at': ledger['bootstrap']['completed_at'],
            'attempt_window_complete': len(recent) == len(targets) and bool(targets),
            'successful_price_window_complete': len(successes) == len(targets) and bool(targets)}


def merge_selected(regions, plans, previous_quotes, previous_cells, result):
    """Patch selected results; do not relabel or freshen an untouched target."""
    quotes = {q['record_id']: copy.deepcopy(q) for q in previous_quotes}
    cells = {c['cell_id']: copy.deepcopy(c) for c in previous_cells}
    for cell in result['cells']:
        if cell['attempt_status'] == 'not_attempted' and cell['cell_id'] in cells:
            continue
        cells[cell['cell_id']] = copy.deepcopy(cell)
    for quote in result['quotes']:
        target_cell = next((c for c in result['cells'] if c['cell_id'] == quote['record_id']), None)
        if target_cell and target_cell['attempt_status'] != 'not_attempted':
            quotes[quote['record_id']] = copy.deepcopy(quote)
    # New/previously unknown targets are still represented in the full matrix.
    for region in regions:
        for plan in plans:
            key = f'{region["config_id"]}:web:{plan["plan_id"]}:month:standard'
            if key not in cells:
                cells[key] = {'cell_id': key, 'target_id': region['target_id'], 'country_code': region.get('country_code'),
                    'config_id': region['config_id'], 'region_kind': region['region_kind'], 'plan_id': plan['plan_id'],
                    'channel': 'web', 'billing_interval': 'month', 'offer_type': 'standard', 'status': 'not_attempted',
                    'attempt_status': 'not_attempted', 'quote_record_id': None, 'last_attempt_at': None,
                    'last_success_at': None, 'http_status': None, 'reason': 'Awaiting first acquisition'}
    valid_ids = {f'{r["config_id"]}:web:{p["plan_id"]}:month:standard' for r in regions for p in plans}
    merged_cells = [cells[key] for key in sorted(valid_ids)]
    merged_quotes = [quotes[key] for key in sorted(valid_ids) if key in quotes and cells[key].get('quote_record_id') == key]
    return merged_quotes, merged_cells


def matrix_summary(regions, cells, plan_count=5):
    counts = dict(collections.Counter(c['status'] for c in cells))
    attempted = {c['target_id'] for c in cells if c.get('last_attempt_at')}
    successful = {c['target_id'] for c in cells if c.get('http_status') == 200 and c['attempt_status'] in {'valid_quote', 'plan_field_missing', 'amount_invalid'}}
    complete = collections.Counter(c['target_id'] for c in cells if c['status'] == 'valid_quote')
    return {'target_count': len(regions), 'country_target_count': sum(bool(r.get('is_country_directory')) for r in regions),
            'auxiliary_target_count': sum(not bool(r.get('is_country_directory')) for r in regions),
            'expected_request_count': len(regions), 'attempted_request_count': len(attempted),
            'not_attempted_request_count': len(regions) - len(attempted), 'successful_response_count': len(successful),
            'expected_cell_count': len(cells), 'status_counts': counts, 'current_quote_cell_count': counts.get('valid_quote', 0),
            'historical_quote_cell_count': counts.get('stale_quote', 0),
            'complete_five_plan_target_count': sum(v == plan_count for v in complete.values())}


def selected_quality_failures(previous_cells, selected_result, selected_ids):
    current = {c['cell_id'] for c in selected_result['cells'] if c['status'] == 'valid_quote'}
    # Network failures retain their own old records. They must not veto correctly
    # verified prices from other targets; field loss in a parsed response can.
    parsed = {c['target_id'] for c in selected_result['cells']
              if c.get('http_status') == 200 and c['attempt_status'] in {'valid_quote', 'plan_field_missing', 'amount_invalid'}}
    previous = {c['cell_id'] for c in previous_cells
                if c['target_id'] in selected_ids & parsed and c['status'] == 'valid_quote'}
    failures = []
    if not current:
        failures.append('all_selected_sources_failed: no accepted current quote; valid published prices are preserved')
    if previous and len(current & previous) / len(previous) < 0.7:
        failures.append('selected_coverage_drop: <70% of previously valid selected cells refreshed')
    return failures
