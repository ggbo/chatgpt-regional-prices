"""Attempt accounting is independent of price freshness and publication."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from scheduling import select_targets, record_attempts, merge_selected, selected_quality_failures
from pipeline import ROOT, current_head, persist_collection, read, write

POLICY = {'policy_revision': 'test-serial-v1', 'window_hours': 72,
          'target_cycle_hours': 48, 'schedule_interval_hours': 1}
PLANS = [{'plan_id': key, 'source_plan_key': source, 'billing_interval': 'month'}
         for key, source in [('go', 'go'), ('plus', 'plus'), ('pro_100', 'prolite'), ('pro_200', 'pro'), ('pro_500', 'promax')]]
NOW = '2026-10-19T00:00:00Z'


def regions(count=252):
    return [{'target_id': f'R{i:03}', 'config_id': f'R{i:03}', 'country_code': f'R{i:03}',
             'region_kind': 'test', 'is_country_directory': True} for i in range(count)]


def cells_for(targets, at=NOW, status='access_blocked'):
    return [{'cell_id': f'{r["config_id"]}:web:{p["plan_id"]}:month:standard',
             'target_id': r['target_id'], 'config_id': r['config_id'], 'plan_id': p['plan_id'],
             'status': status, 'attempt_status': status, 'http_status': 200 if status == 'valid_quote' else 403,
             'last_attempt_at': at if status != 'not_attempted' else None,
             'last_success_at': at if status == 'valid_quote' else None, 'quote_record_id': None}
            for r in targets for p in PLANS]


def bootstrapped(targets):
    selected = select_targets(targets, PLANS, None, POLICY, NOW)
    return record_attempts(selected, cells_for(targets), POLICY, NOW, NOW, 'bootstrap')[0]


class RotationTests(unittest.TestCase):
    def test_first_scan_and_changed_policy_require_the_entire_catalog(self):
        targets = regions()
        selection = select_targets(targets, PLANS, None, POLICY, NOW)
        self.assertEqual(len(selection['targets']), 252)
        ledger = bootstrapped(targets)
        self.assertEqual(len(select_targets(targets, PLANS, ledger, POLICY, NOW)['targets']), 6)
        changed = dict(POLICY, policy_revision='changed')
        self.assertTrue(select_targets(targets, PLANS, ledger, changed, NOW)['bootstrap'])

    def test_403_failures_rotate_all_targets_in_42_hourly_batches(self):
        targets = regions()
        ledger = bootstrapped(targets)
        seen = []
        for hour in range(1, 43):
            at = (datetime(2026, 10, 19, tzinfo=timezone.utc) + timedelta(hours=hour)).isoformat().replace('+00:00', 'Z')
            selection = select_targets(targets, PLANS, ledger, POLICY, at)
            self.assertEqual(len(selection['targets']), 6)
            seen.extend(r['target_id'] for r in selection['targets'])
            ledger, run = record_attempts(selection, cells_for(selection['targets'], at), POLICY, at, at, str(hour))
        self.assertEqual(len(set(seen)), 252)
        self.assertEqual(run['window']['attempted_within_window_count'], 252)
        self.assertEqual(run['window']['successful_within_window_count'], 0)
        self.assertFalse(run['window']['successful_price_window_complete'])

    def test_unattempted_and_replayed_checkpoint_do_not_freshen_ledger(self):
        targets = regions(2)
        selection = select_targets(targets, PLANS, None, POLICY, NOW)
        ledger, run = record_attempts(selection, cells_for(targets, status='not_attempted'), POLICY, NOW, NOW, 'zero')
        self.assertEqual(run['window']['bootstrap_remaining_count'], 2)
        self.assertTrue(all(v['last_attempt_at'] is None for v in ledger['targets'].values()))
        selection = select_targets(targets, PLANS, ledger, POLICY, NOW)
        ledger, _ = record_attempts(selection, cells_for(targets[:1]), POLICY, NOW, NOW, 'one')
        replay = select_targets(targets, PLANS, ledger, POLICY, '2026-10-19T01:00:00Z')
        replayed, run = record_attempts(replay, cells_for(targets[:1]), POLICY, NOW, '2026-10-19T01:00:00Z', 'replay')
        self.assertEqual(replayed['targets']['R000']['last_run_id'], 'one')
        self.assertEqual(run['resumed_target_ids'], ['R000'])
        self.assertEqual(run['window']['bootstrap_remaining_count'], 1)

    def test_one_valid_plan_does_not_claim_all_five_prices_refreshed(self):
        targets = regions(1)
        selection = select_targets(targets, PLANS, None, POLICY, NOW)
        partial = cells_for(targets)
        partial[0].update(status='valid_quote', attempt_status='valid_quote', http_status=200, last_success_at=NOW)
        ledger, run = record_attempts(selection, partial, POLICY, NOW, NOW, 'partial')
        self.assertIsNone(ledger['targets']['R000']['last_success_at'])
        self.assertEqual(run['window']['attempted_within_window_count'], 1)
        self.assertEqual(run['window']['successful_within_window_count'], 0)

    def test_only_selected_target_is_patched_and_failed_attempt_keeps_success_time(self):
        targets = regions(2)
        old = cells_for(targets, status='valid_quote')
        old_quotes = []
        for cell in old:
            cell['quote_record_id'] = cell['cell_id']
            old_quotes.append({'record_id': cell['cell_id'], 'last_success_at': NOW, 'display_price': {'amount': '20', 'last_success_at': NOW}})
        failures = cells_for(targets[:1], '2026-10-20T00:00:00Z')
        for cell in failures:
            cell.update(status='stale_quote', quote_record_id=cell['cell_id'], last_success_at=NOW)
        result = {'cells': failures, 'quotes': old_quotes[:5]}
        quotes, merged = merge_selected(targets, PLANS, old_quotes, old, result)
        self.assertEqual([c for c in merged if c['target_id'] == 'R001'], old[5:])
        self.assertEqual(quotes, sorted(old_quotes, key=lambda q: q['record_id']))
        self.assertTrue(all(c['last_success_at'] == NOW for c in merged))

    def test_quality_checks_selected_targets_and_accepts_network_partial_progress(self):
        targets = regions(2)
        old = cells_for(targets, status='valid_quote')
        failures = cells_for(targets[:1])
        self.assertIn('all_selected_sources_failed', selected_quality_failures(old, {'cells': failures}, {'R000'})[0])
        mixed = failures + cells_for(targets[1:], '2026-10-19T01:00:00Z', 'valid_quote')
        self.assertEqual(selected_quality_failures(old, {'cells': mixed}, {'R000', 'R001'}), [])
        structural = copy.deepcopy(mixed)
        for cell in structural[:5]:
            cell.update(http_status=200, attempt_status='plan_field_missing', status='stale_quote')
        self.assertTrue(any('selected_coverage_drop' in e for e in selected_quality_failures(old, {'cells': structural}, {'R000', 'R001'})))

    def test_rejected_run_persists_attempts_without_replacing_valid_price_state(self):
        def git(*args):
            subprocess.run(['git', *map(str, args)], capture_output=True, check=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, state, candidate = root / 'remote.git', root / 'state', root / 'candidate'
            git('init', '--bare', remote)
            git('init', '-b', 'data', state)
            git('-C', state, 'remote', 'add', 'origin', remote)
            write(state / 'state/latest.json', {'verified_prices': 'unchanged'})
            git('-C', state, 'add', '.')
            git('-C', state, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'Preserve verified prices')
            git('-C', state, 'push', 'origin', 'data')
            selection = select_targets(regions(), PLANS, None, POLICY, NOW)
            ledger, run = record_attempts(selection, cells_for(regions()), POLICY, NOW, NOW, 'all-403')
            write(candidate / 'collection/ledger.json', ledger)
            write(candidate / 'collection/last-run.json', run)
            write(candidate / 'prepare-receipt.json', {'state_head': current_head(state), 'source_head': current_head(ROOT)})
            (candidate / 'evidence').mkdir()
            self.assertEqual(persist_collection(SimpleNamespace(candidate_dir=candidate, state_dir=state)), 0)
            fresh = root / 'new-runner'
            git('clone', '--branch', 'data', remote, fresh)
            self.assertEqual(read(fresh / 'state/latest.json'), {'verified_prices': 'unchanged'})
            saved = read(fresh / 'collection/ledger.json')
            self.assertEqual(len(select_targets(regions(), PLANS, saved, POLICY, NOW)['targets']), 6)


if __name__ == '__main__':
    unittest.main()
