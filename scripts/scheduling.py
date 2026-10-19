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


def format_time(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _plan_signature(plans):
    return hashlib.sha256(json.dumps(
        [(p['plan_id'], p['source_plan_key'], p['billing_interval']) for p in plans],
        sort_keys=True,
    ).encode()).hexdigest()


def _coverage_by_target(previous_cells):
    grouped = collections.defaultdict(list)
    for cell in previous_cells or []:
        grouped[cell['target_id']].append(cell)
    return grouped


def _backfill_retry_state(entry, target_cells, plans):
    """Backfill v2 fields without inventing new attempt or success times."""
    outcomes = {}
    expected = {plan['plan_id'] for plan in plans}
    for cell in target_cells:
        outcomes[cell['plan_id']] = {
            'status': cell.get('status'),
            'attempt_status': cell.get('attempt_status'),
            'last_attempt_at': cell.get('last_attempt_at'),
            'last_success_at': cell.get('last_success_at'),
            'http_status': cell.get('http_status'),
            'reason': cell.get('reason'),
        }
    complete = set(outcomes) == expected and all(
        value['status'] == 'valid_quote'
        and value['attempt_status'] == 'valid_quote'
        and value['last_success_at']
        for value in outcomes.values()
    )
    entry['plan_outcomes'] = outcomes
    entry['retry_pending'] = not complete
    if complete:
        success = min((value['last_success_at'] for value in outcomes.values()), key=parse_time)
        entry['all_five_success_at'] = success
        entry['last_success_at'] = success
    else:
        entry['all_five_success_at'] = entry.get('all_five_success_at') or entry.get('last_success_at')


def _sync_initial_fill(ledger, policy):
    state = ledger.setdefault('initial_fill', {})
    cohort = set(state.get('cohort_target_ids', []))
    pending = sorted(
        key for key in cohort
        if key in ledger['targets'] and ledger['targets'][key].get('retry_pending')
        and not ledger['targets'][key].get('initial_fill_done')
    )
    eligible = list(pending)
    state.pop('consecutive_no_progress_runs', None)
    state.pop('blocked_no_progress', None)
    state['active'] = bool(eligible)
    state['pending_target_ids'] = pending
    state['pending_count'] = len(pending)
    state['eligible_target_ids'] = eligible
    state['eligible_count'] = len(eligible)
    state['exhausted_target_ids'] = []
    state['exhausted_count'] = 0
    return state


def _initialize_initial_fill(ledger, policy, now):
    state = ledger.setdefault('initial_fill', {})
    legacy_exhausted = set(state.get('exhausted_target_ids', []))
    if 'cohort_target_ids' not in state:
        state['cohort_target_ids'] = sorted(
            key for key, entry in ledger['targets'].items() if entry.get('retry_pending')
        )
    cohort = set(state['cohort_target_ids'])
    cutoff = parse_time(now) - timedelta(hours=policy.get('window_hours', 72))
    for key, entry in ledger['targets'].items():
        entry.setdefault('initial_fill_attempts', 0)
        counted = entry.setdefault('initial_fill_observation_times', [])
        if not isinstance(counted, list):
            entry['initial_fill_observation_times'] = []
        else:
            entry['initial_fill_observation_times'] = list(dict.fromkeys(
                value for value in counted
                if isinstance(value, str) and parse_time(value) and parse_time(value) >= cutoff
            ))
        entry.setdefault(
            'initial_fill_done',
            key not in cohort or not entry.get('retry_pending'),
        )
        if key in legacy_exhausted and entry.get('retry_pending'):
            entry['initial_fill_done'] = False
            entry.pop('initial_fill_done_reason', None)
        elif entry.get('initial_fill_done') and entry.get('all_five_success_at'):
            entry.setdefault('initial_fill_done_reason', 'success')


def policy_ledger(regions, plans, previous, policy, now, previous_cells=None):
    plans_signature = _plan_signature(plans)
    previous_revision = previous.get('policy_revision') if previous else None
    exact = previous and previous_revision == policy['policy_revision'] and previous.get('plans_signature') == plans_signature
    migratable = (
        previous
        and previous_revision in policy.get('compatible_policy_revisions', [])
        and previous.get('plans_signature') == plans_signature
    )
    compatible = exact or migratable
    ledger = copy.deepcopy(previous) if compatible else {
        'schema_version': '1.0.0', 'policy_revision': policy['policy_revision'],
        'plans_signature': plans_signature, 'bootstrap': {'started_at': now, 'completed_at': None}, 'targets': {}}
    if migratable:
        ledger['migrated_from_policy_revision'] = previous_revision
        ledger['policy_revision'] = policy['policy_revision']
    coverage = _coverage_by_target(previous_cells)
    seed_from_coverage = previous is None and bool(previous_cells)
    targets = {}
    for region in regions:
        old = ledger['targets'].get(region['target_id'], {})
        if old.get('config_id') == region['config_id']:
            entry = old
            if 'retry_pending' not in entry or 'plan_outcomes' not in entry:
                cells = coverage.get(region['target_id'], [])
                if cells:
                    _backfill_retry_state(entry, cells, plans)
                else:
                    # In v1 last_success_at was written only after all five plans succeeded.
                    entry['retry_pending'] = not bool(entry.get('last_success_at'))
                    entry['all_five_success_at'] = entry.get('last_success_at')
                    entry['plan_outcomes'] = {}
            if entry.get('retry_pending') and entry.get('last_attempt_at') and not entry.get('retry_due_at'):
                entry['retry_due_at'] = format_time(
                    parse_time(entry['last_attempt_at']) + timedelta(minutes=policy.get('retry_interval_minutes', 30))
                )
            if not entry.get('retry_pending'):
                entry['retry_due_at'] = None
        else:
            entry = {
                'config_id': region['config_id'], 'bootstrap_attempted': False,
                'last_attempt_at': None, 'last_success_at': None, 'all_five_success_at': None,
                'last_attempt_status': 'not_attempted', 'last_http_status': None, 'last_run_id': None,
                'retry_pending': True, 'retry_due_at': None, 'plan_outcomes': {},
                'initial_fill_attempts': 0,
                'initial_fill_done': False,
            }
            if seed_from_coverage:
                cells = coverage.get(region['target_id'], [])
                real = [cell for cell in cells if cell.get('last_attempt_at') and cell.get('attempt_status') != 'not_attempted']
                if real:
                    observed = max((cell['last_attempt_at'] for cell in real), key=parse_time)
                    statuses = {cell.get('attempt_status') for cell in real}
                    http_statuses = {cell.get('http_status') for cell in real}
                    entry.update(
                        bootstrap_attempted=True,
                        last_attempt_at=observed,
                        last_attempt_status=next(iter(statuses)) if len(statuses) == 1 else 'incomplete_five_plan',
                        last_http_status=next(iter(http_statuses)) if len(http_statuses) == 1 else None,
                    )
                    _backfill_retry_state(entry, cells, plans)
                    if entry['retry_pending']:
                        entry['retry_due_at'] = format_time(
                            parse_time(observed) + timedelta(minutes=policy.get('retry_interval_minutes', 30))
                        )
        targets[region['target_id']] = entry
    ledger['targets'] = targets
    ledger['catalog_version'] = regions[0].get('catalog_version') if regions else None
    ledger['updated_at'] = now
    ledger['target_count'] = len(targets)
    ledger['plan_count'] = len(plans)
    if any(not value['bootstrap_attempted'] for value in targets.values()):
        ledger['bootstrap']['completed_at'] = None
    elif targets and not ledger['bootstrap'].get('completed_at'):
        observed = [value['last_attempt_at'] for value in targets.values() if value.get('last_attempt_at')]
        ledger['bootstrap']['completed_at'] = max(observed, key=parse_time) if observed else now
        if seed_from_coverage and observed:
            ledger['bootstrap']['started_at'] = min(observed, key=parse_time)
    _initialize_initial_fill(ledger, policy, now)
    _sync_initial_fill(ledger, policy)
    return ledger


def _retry_due(entry, now, interval_minutes):
    if not entry.get('retry_pending'):
        return False
    last_attempt = parse_time(entry.get('last_attempt_at'))
    return last_attempt is None or parse_time(now) >= last_attempt + timedelta(minutes=interval_minutes)


def select_targets(
    regions, plans, previous, policy, now, force_full=False, retry_only=False,
    previous_cells=None, fill_missing=False,
):
    ledger = policy_ledger(regions, plans, previous, policy, now, previous_cells=previous_cells)
    pending = [r for r in regions if not ledger['targets'][r['target_id']]['bootstrap_attempted']]
    bootstrap = bool(pending)
    retry_interval = policy.get('retry_interval_minutes', 30)
    first = datetime.min.replace(tzinfo=timezone.utc)
    due = sorted(
        (r for r in regions if _retry_due(ledger['targets'][r['target_id']], now, retry_interval)),
        key=lambda r: (
            ledger['targets'][r['target_id']].get('last_attempt_at') is not None,
            parse_time(ledger['targets'][r['target_id']].get('last_attempt_at')) or first,
            r['target_id'],
        ),
    )
    size = max(1, math.ceil(len(regions) * policy['schedule_interval_hours'] / policy['target_cycle_hours']))
    if force_full:
        selected = list(regions)
    elif bootstrap:
        selected = list(regions)  # same catalog signature lets an interrupted bootstrap restore its checkpoint
    elif fill_missing:
        pending_retry = sorted(
            (
                r for r in regions
                if r['target_id'] in set(ledger['initial_fill'].get('cohort_target_ids', []))
                and ledger['targets'][r['target_id']].get('retry_pending')
                and not ledger['targets'][r['target_id']].get('initial_fill_done')
            ),
            key=lambda r: (
                ledger['targets'][r['target_id']].get('last_attempt_at') is not None,
                parse_time(ledger['targets'][r['target_id']].get('last_attempt_at')) or first,
                r['target_id'],
            ),
        )
        immediate_ids = {r['target_id'] for r in pending_retry}
        timed_retry = [r for r in due if r['target_id'] not in immediate_ids]
        cutoff = parse_time(now) - timedelta(hours=policy['target_cycle_hours'])
        at_risk_healthy = sorted(
            (
                r for r in regions
                if not ledger['targets'][r['target_id']].get('retry_pending')
                and (
                    not ledger['targets'][r['target_id']].get('last_attempt_at')
                    or parse_time(ledger['targets'][r['target_id']]['last_attempt_at']) <= cutoff
                )
            ),
            key=lambda r: (parse_time(ledger['targets'][r['target_id']].get('last_attempt_at')) or first, r['target_id']),
        ) if pending_retry else []
        retry_union = sorted(
            {r['target_id']: r for r in pending_retry + timed_retry}.values(),
            key=lambda r: (
                ledger['targets'][r['target_id']].get('last_attempt_at') is not None,
                parse_time(ledger['targets'][r['target_id']].get('last_attempt_at')) or first,
                r['target_id'],
            ),
        )
        selected = at_risk_healthy + retry_union
        ledger['initial_fill']['active'] = bool(pending_retry)
    elif retry_only:
        selected = due
    else:
        normal = sorted(
            (r for r in regions if not ledger['targets'][r['target_id']].get('retry_pending')),
            key=lambda r: (parse_time(ledger['targets'][r['target_id']].get('last_attempt_at')) or first, r['target_id']),
        )[:size]
        due_ids = {r['target_id'] for r in due}
        # Protect the 72-hour healthy rotation from a large retry backlog. The
        # half-hour retry-only run handles the same due queue without normals.
        selected = [r for r in normal if r['target_id'] not in due_ids] + due
    selected_ids = {r['target_id'] for r in selected}
    initial_cohort = set(ledger['initial_fill'].get('cohort_target_ids', []))
    initial_fill_target_ids = [
        r['target_id'] for r in regions
        if fill_missing and r['target_id'] in selected_ids
        and r['target_id'] in initial_cohort
        and ledger['targets'][r['target_id']].get('retry_pending')
        and not ledger['targets'][r['target_id']].get('initial_fill_done')
    ]
    return {'ledger': ledger, 'targets': selected, 'bootstrap': bootstrap,
            'normal_batch_size': size, 'retry_due_count': len(due),
            'initial_fill_target_ids': initial_fill_target_ids,
            'mode': ('bootstrap' if bootstrap else 'full' if force_full else 'fill' if fill_missing
                     else 'retry' if retry_only else 'rotation')}


def record_attempts(
    selection, cells, policy, started_at, completed_at, run_id,
    observed_fill_counts=None,
):
    ledger = copy.deepcopy(selection['ledger'])
    by_target = collections.defaultdict(list)
    for cell in cells:
        by_target[cell['target_id']].append(cell)
    attempted, not_started, resumed, out_of_order = [], [], [], []
    observations = {}
    complete_observations = set()
    initial_target_ids = set(selection.get('initial_fill_target_ids', (
        region['target_id'] for region in selection['targets']
        if region['target_id'] in set(ledger.get('initial_fill', {}).get('cohort_target_ids', []))
        and ledger['targets'][region['target_id']].get('retry_pending')
        and not ledger['targets'][region['target_id']].get('initial_fill_done')
    )))
    selected_pending_before = set(initial_target_ids)
    for region in selection['targets']:
        key = region['target_id']
        target_cells = by_target.get(key, [])
        real = [c for c in target_cells if c.get('last_attempt_at') and c['attempt_status'] != 'not_attempted']
        if not real:
            not_started.append(key)
            continue
        entry = ledger['targets'][key]
        observed = max((c['last_attempt_at'] for c in real), key=parse_time)
        observations[key] = observed
        if entry.get('last_attempt_at') == observed:
            resumed.append(key)
            continue
        attempted.append(key)
        outcomes = {
            cell['plan_id']: {
                'status': cell.get('status'), 'attempt_status': cell.get('attempt_status'),
                'last_attempt_at': cell.get('last_attempt_at'), 'last_success_at': cell.get('last_success_at'),
                'http_status': cell.get('http_status'), 'reason': cell.get('reason'),
            }
            for cell in real
        }
        successful = [
            c for c in real
            if c['status'] == 'valid_quote'
            and c.get('attempt_status') == 'valid_quote'
            and c.get('last_success_at')
        ]
        complete = len(outcomes) == ledger['plan_count'] and len(successful) == ledger['plan_count']
        if complete:
            complete_observations.add(key)
        attempt_statuses = {cell.get('attempt_status') for cell in real}
        representative = 'all_five_valid' if complete else next(iter(attempt_statuses)) if len(attempt_statuses) == 1 else 'incomplete_five_plan'
        http_statuses = {cell.get('http_status') for cell in real}
        is_newest = not entry.get('last_attempt_at') or parse_time(observed) > parse_time(entry['last_attempt_at'])
        if is_newest:
            entry.update(
                bootstrap_attempted=True, last_attempt_at=observed, last_attempt_status=representative,
                last_http_status=next(iter(http_statuses)) if len(http_statuses) == 1 else None,
                last_run_id=run_id, plan_outcomes=outcomes, retry_pending=not complete,
                retry_due_at=None if complete else format_time(
                    parse_time(observed) + timedelta(minutes=policy.get('retry_interval_minutes', 30))
                ),
            )
        else:
            out_of_order.append(key)
        if complete:
            success_at = min((c['last_success_at'] for c in successful), key=parse_time)
            for field in ('last_success_at', 'all_five_success_at'):
                if not entry.get(field) or parse_time(success_at) > parse_time(entry[field]):
                    entry[field] = success_at
    fill_counts = {} if observed_fill_counts is None else observed_fill_counts
    if not isinstance(fill_counts, dict):
        raise ValueError('observed_fill_counts must be an object')
    for key, count in fill_counts.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f'invalid observed fill count for {key}')
    if selection['mode'] == 'fill':
        cohort = set(ledger['initial_fill'].get('cohort_target_ids', []))
        for key, observed in observations.items():
            if key not in initial_target_ids:
                continue
            entry = ledger['targets'][key]
            imported = fill_counts.get(key, 0)
            counted = entry.setdefault('initial_fill_observation_times', [])
            already_current = entry.get('last_attempt_at') == observed and key in resumed
            if observed not in counted:
                if not already_current:
                    entry['initial_fill_attempts'] = entry.get('initial_fill_attempts', 0) + 1
                counted.append(observed)
            entry['initial_fill_attempts'] = max(entry.get('initial_fill_attempts', 0), imported)
            if not entry.get('retry_pending'):
                entry['initial_fill_done'] = True
                entry['initial_fill_done_reason'] = 'success'
        ledger['initial_fill']['cohort_target_ids'] = sorted(cohort)
    cohort = set(ledger['initial_fill'].get('cohort_target_ids', []))
    for key in attempted:
        if key in cohort and (key in complete_observations or not ledger['targets'][key].get('retry_pending')):
            ledger['targets'][key]['initial_fill_done'] = True
            ledger['targets'][key]['initial_fill_done_reason'] = 'success'
    progress = sum(not ledger['targets'][key].get('retry_pending') for key in selected_pending_before)
    initial_fill = _sync_initial_fill(ledger, policy)
    immediate_continuation = bool(selection['mode'] == 'fill' and initial_fill['eligible_count'])
    ledger['updated_at'] = completed_at
    if ledger['targets'] and all(e['bootstrap_attempted'] for e in ledger['targets'].values()) and not ledger['bootstrap']['completed_at']:
        ledger['bootstrap']['completed_at'] = completed_at
    metrics = window_metrics(ledger, policy, completed_at)
    observed_times = [c['last_attempt_at'] for c in cells if c.get('last_attempt_at')]
    run = {'schema_version': '1.0.0', 'run_id': run_id, 'policy_revision': policy['policy_revision'],
           'mode': selection['mode'], 'started_at': started_at, 'completed_at': completed_at,
           'selected_target_ids': [r['target_id'] for r in selection['targets']],
           'attempted_target_ids': attempted, 'not_started_target_ids': not_started, 'resumed_target_ids': resumed,
           'out_of_order_target_ids': out_of_order,
           'accepted_valid_cell_count': sum(c['status'] == 'valid_quote' for c in cells),
           'initial_fill_progress_count': progress,
           'initial_fill_pending_count': initial_fill['pending_count'],
           'initial_fill_eligible_count': initial_fill['eligible_count'],
           'initial_fill_exhausted_count': initial_fill['exhausted_count'],
           'initial_fill_exhausted_target_ids': initial_fill['exhausted_target_ids'],
           'immediate_continuation': immediate_continuation,
           'observation_started_at': min(observed_times, key=parse_time) if observed_times else None,
           'observation_completed_at': max(observed_times, key=parse_time) if observed_times else None,
           'status_counts': dict(collections.Counter(c['status'] for c in cells)), 'window': metrics}
    return ledger, run


def window_metrics(ledger, policy, now):
    clock = parse_time(now)
    cutoff = clock - timedelta(hours=policy['window_hours'])
    targets = ledger['targets']
    recent = [key for key, value in targets.items() if value.get('last_attempt_at') and parse_time(value['last_attempt_at']) >= cutoff]
    successes = [key for key, value in targets.items() if value.get('all_five_success_at') and parse_time(value['all_five_success_at']) >= cutoff]
    oldest = min((v['last_attempt_at'] for v in targets.values() if v.get('last_attempt_at')), default=None, key=parse_time)
    age = (clock - parse_time(oldest)).total_seconds() / 3600 if oldest else None
    return {'window_hours': policy['window_hours'], 'target_count': len(targets),
            'attempted_within_window_count': len(recent), 'successful_within_window_count': len(successes),
            'overdue_target_ids': sorted(set(targets) - set(recent)), 'overdue_target_count': len(targets) - len(recent),
            'oldest_attempt_at': oldest, 'oldest_attempt_age_hours': age,
            'bootstrap_remaining_count': sum(not v['bootstrap_attempted'] for v in targets.values()),
            'bootstrap_completed_at': ledger['bootstrap']['completed_at'],
            'retry_pending_count': sum(bool(v.get('retry_pending')) for v in targets.values()),
            'retry_due_count': sum(_retry_due(v, now, policy.get('retry_interval_minutes', 30)) for v in targets.values()),
            'attempt_window_complete': len(recent) == len(targets) and bool(targets),
            'successful_price_window_complete': len(successes) == len(targets) and bool(targets)}


def merge_selected(regions, plans, previous_quotes, previous_cells, result):
    """Patch selected results; do not relabel or freshen an untouched target."""
    quotes = {q['record_id']: copy.deepcopy(q) for q in previous_quotes}
    cells = {c['cell_id']: copy.deepcopy(c) for c in previous_cells}
    accepted_cells = set()
    for cell in result['cells']:
        if cell['attempt_status'] == 'not_attempted' and cell['cell_id'] in cells:
            continue
        previous = cells.get(cell['cell_id'])
        previous_at = parse_time(previous.get('last_attempt_at')) if previous else None
        candidate_at = parse_time(cell.get('last_attempt_at'))
        if previous_at and (candidate_at is None or candidate_at < previous_at):
            continue
        cells[cell['cell_id']] = copy.deepcopy(cell)
        accepted_cells.add(cell['cell_id'])
    for quote in result['quotes']:
        target_cell = next((c for c in result['cells'] if c['cell_id'] == quote['record_id']), None)
        if (target_cell and quote['record_id'] in accepted_cells
                and target_cell['attempt_status'] != 'not_attempted'):
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
