"""Publication-contract regressions. Synthetic data is used only in these tests."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from pipeline import ROOT, prepare, validate, safe_path, publish_files, read, write, candidate_hashes, persist_progress, persist_collection, restore_progress, clear_completed_progress, current_head
from reconcile_reference import reconcile


def example():
    plans = {'schema_version': '1.0.0', 'plans': [{'plan_id': p, 'source_plan_key': k} for p, k in [('go', 'go'), ('plus', 'plus'), ('pro_100', 'prolite'), ('pro_200', 'pro'), ('pro_500', 'promax')]]}
    regions = {'schema_version': '1.0.0', 'catalog_version': 'test-only', 'regions': [{'target_id': 'US', 'config_id': 'US', 'country_code': 'US', 'is_country_directory': True}]}
    quotes, cells = [], []
    for p in plans['plans']:
        key = f'US:web:{p["plan_id"]}:month:standard'
        quotes.append({'record_id': key, 'target_id': 'US', 'config_id': 'US', 'plan_id': p['plan_id'], 'source_plan_key': p['source_plan_key'], 'country_code': 'US', 'currency': 'USD',
                       'display_price': {'amount': '8.0', 'tax': 'exclusive'},
                       'channel': 'web', 'billing_interval': 'month', 'offer_type': 'standard',
                       'availability': {'purchase_verified': False}, 'source': {'body_sha256': 'a' * 64},
                       'estimates': {'USD': None, 'CNY': None}})
        cells.append({'cell_id': key, 'quote_record_id': key, 'target_id': 'US', 'config_id': 'US',
                      'plan_id': p['plan_id'], 'status': 'valid_quote', 'attempt_status': 'valid_quote', 'http_status': 200, 'last_attempt_at': '2026-10-19T00:00:00Z'})
    latest = {'schema_version': '1.0.0', 'data_snapshot_id': 'test', 'generated_at': '2026-10-19T00:00:00Z', 'catalog_version': 'test-only', 'fx_snapshot_id': None, 'quotes': quotes}
    coverage = {'schema_version': '1.0.0', 'data_snapshot_id': 'test', 'catalog_version': 'test-only', 'cells': cells,
                'summary': {'target_count': 1, 'country_target_count': 1, 'auxiliary_target_count': 0, 'expected_request_count': 1, 'successful_response_count': 1, 'current_quote_cell_count': 5, 'historical_quote_cell_count': 0, 'complete_five_plan_target_count': 1, 'expected_cell_count': 5, 'status_counts': {'valid_quote': 5}, 'attempted_request_count': 1, 'not_attempted_request_count': 0}}
    fx = {'schema_version': '1.0.0', 'fx_snapshot_id': None, 'rates': []}
    return latest, coverage, fx, regions, plans


class PublicationTests(unittest.TestCase):
    def retry_fixture(self, root):
        latest, coverage, fx, regions, plans = example()
        regions['regions'][0]['region_kind'] = 'test'
        for plan in plans['plans']:
            plan['billing_interval'] = 'month'
        for quote in latest['quotes']:
            quote['display_price']['last_success_at'] = '2026-10-19T00:00:00Z'
            quote['last_success_at'] = '2026-10-19T00:00:00Z'
        for cell in coverage['cells']:
            cell['last_success_at'] = '2026-10-19T00:00:00Z'
        policy = read(ROOT / 'config/collection-policy.json')
        for name, value in [('regions', regions), ('plans', plans), ('collection-policy', policy)]:
            write(root / f'config/{name}.json', value)
        state = root / 'persisted'
        for name, value in [('latest', latest), ('coverage', coverage), ('fx', fx), ('plans', plans), ('status', {'schema_version': '1.0.0', 'data_snapshot_id': 'test'})]:
            write(state / f'state/{name}.json', value)
        ledger = {'schema_version': '1.0.0', 'plan_count': 5, 'bootstrap': {'started_at': latest['generated_at'], 'completed_at': latest['generated_at']},
                  'initial_fill': {'active': False, 'cohort_target_ids': ['US']},
                  'targets': {'US': {'config_id': 'US', 'bootstrap_attempted': True, 'last_attempt_at': latest['generated_at'],
                                     'last_success_at': latest['generated_at'], 'all_five_success_at': latest['generated_at'],
                                     'retry_pending': True, 'initial_fill_attempts': 10, 'initial_fill_done': True}}}
        write(state / 'collection/ledger.json', ledger)
        args = SimpleNamespace(mode='retry', state_dir=state, candidate_dir=root / 'candidate', public_dir=root / 'public',
                               resume=False, fx_seed=None, full_scan=False, targets=None, budget_seconds=None)
        selection = {'ledger': ledger, 'targets': regions['regions'], 'bootstrap': False, 'mode': 'retry'}
        return args, selection, latest, coverage, fx

    def test_retry_all_failure_preserves_publication_but_records_the_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, selection, latest, coverage, fx = self.retry_fixture(root)
            cells = copy.deepcopy(coverage['cells'])
            for cell in cells:
                cell.update(status='stale_quote', attempt_status='access_blocked', http_status=403, last_attempt_at='2026-10-19T01:00:00Z')
            result = {'quotes': latest['quotes'], 'cells': cells, 'source_status': {'all_source_failed': True}, 'change_reviews': []}
            write(root / 'public/manifest.json', {'keep': 'previous-deployment'})
            with patch('pipeline.ROOT', root), patch('scheduling.select_targets', return_value=selection), patch('collect.collect', return_value=result), patch('fx.fetch_fx', return_value=fx):
                self.assertEqual(prepare(args), 2)
            self.assertEqual(read(root / 'public/manifest.json'), {'keep': 'previous-deployment'})
            self.assertFalse((root / 'candidate/validated.json').exists())
            self.assertEqual(read(root / 'candidate/collection/ledger.json')['targets']['US']['last_attempt_at'], '2026-10-19T01:00:00Z')

    def test_retry_without_due_failures_does_not_fetch_healthy_prices_or_fx(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, selection, latest, coverage, fx = self.retry_fixture(root)
            selection['targets'] = []
            with patch('pipeline.ROOT', root), patch('scheduling.select_targets', return_value=selection), patch('collect.collect') as collect_mock, patch('fx.fetch_fx') as fx_mock:
                self.assertEqual(prepare(args), 0)
            collect_mock.assert_not_called()
            fx_mock.assert_not_called()
            self.assertEqual(read(root / 'public/manifest.json')['data_snapshot_id'], 'test')
            self.assertTrue((root / 'candidate/collection/ledger.json').is_file())

    def test_public_reference_comparison_only_contains_official_web(self):
        latest, coverage, fx, regions, plans = example()
        baseline = {'entries': [{'reference_id': f'US:{channel}:plus:month', 'target_id': 'US', 'channel': channel,
                                 'plan_id': 'plus', 'billing_interval': 'month', 'reference_status': 'listed'}
                                for channel in ('web', 'ios', 'google_play')]}
        report = reconcile(regions, plans, coverage['cells'], baseline)
        self.assertEqual(report['summary']['reference_identity_count'], 1)
        self.assertTrue(all(row['channel'] == 'web' for row in report['comparisons']))

    def test_no_fx_does_not_remove_original_currency_price(self):
        self.assertTrue(validate(*example()))

    def test_missing_matrix_cell_rejected(self):
        data = example()
        data[1]['cells'].pop()
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            validate(*data)

    def test_silent_country_fallback_rejected(self):
        data = example()
        data[0]['quotes'][0]['country_code'] = 'PH'
        with self.assertRaisesRegex(ValueError, 'country'):
            validate(*data)

    def test_mixed_fx_snapshot_rejected(self):
        data = example()
        data[0]['quotes'][0]['estimates']['CNY'] = {'amount': '56', 'fx_snapshot_id': 'another-run'}
        with self.assertRaisesRegex(ValueError, 'mixed FX'):
            validate(*data)

    def test_all_failure_is_diagnostic_only(self):
        data = example()
        data[0]['quotes'] = []
        for c in data[1]['cells']:
            c.update(status='access_blocked', attempt_status='access_blocked', quote_record_id=None)
        data[1]['summary']['status_counts'] = {'access_blocked': 5}
        data[1]['summary'].update(successful_response_count=0, current_quote_cell_count=0, complete_five_plan_target_count=0)
        with self.assertRaisesRegex(ValueError, 'empty publication'):
            validate(*data)
        self.assertTrue(validate(*data, production=False))

    def test_count_claim_must_match_results(self):
        data = example()
        data[1]['summary']['attempted_request_count'] = 0
        with self.assertRaisesRegex(ValueError, 'attempted_request_count'):
            validate(*data)

    def test_all_published_coverage_counters_are_recomputed(self):
        for field in ('country_target_count', 'auxiliary_target_count', 'expected_request_count', 'successful_response_count', 'current_quote_cell_count', 'historical_quote_cell_count', 'complete_five_plan_target_count'):
            data = example()
            data[1]['summary'][field] = 999
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                validate(*data)

    def test_manifest_pins_exact_bytes_and_changed_candidate_detected(self):
        data = example()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            for name, value in zip(('latest', 'coverage', 'fx', 'regions', 'plans'), data):
                write(state / f'{name}.json', value)
            for name in ('status', 'reference-comparison'):
                write(state / f'{name}.json', {})
            manifest = publish_files(state, root / 'public')
            self.assertEqual(manifest['data_snapshot_id'], 'test')
            self.assertNotEqual(manifest['entrypoints']['latest'], 'latest.json')
            before = candidate_hashes(root)
            write(state / 'latest.json', {})
            self.assertNotEqual(before, candidate_hashes(root))

    def test_path_traversal_never_published(self):
        for path in ('../secret', '/absolute', 'C:/secret', 'data\\escape'):
            with self.assertRaises(ValueError):
                safe_path(path)

    def test_progress_survives_new_runner_without_replacing_validated_state(self):
        def run(*args):
            subprocess.run(['git', *map(str, args)], capture_output=True, check=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, state, candidate = root / 'remote.git', root / 'state', root / 'candidate'
            run('init', '--bare', remote)
            run('init', '-b', 'data', state)
            run('-C', state, 'remote', 'add', 'origin', remote)
            write(state / 'state/latest.json', {'validated': 'keep-me'})
            run('-C', state, 'add', '.')
            run('-C', state, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'Protect the previously verified baseline')
            run('-C', state, 'push', 'origin', 'data')
            write(candidate / 'state/coverage.json', {'summary': {'not_attempted_request_count': 1}})
            write(candidate / 'collection-checkpoint.json', {'target_ids': ['US'], 'attempts': {}})
            write(candidate / 'prepare-receipt.json', {'state_head': current_head(state)})
            (candidate / 'evidence').mkdir()
            self.assertEqual(persist_progress(SimpleNamespace(candidate_dir=candidate, state_dir=state)), 0)
            fresh = root / 'new-runner'
            run('clone', '--branch', 'data', remote, fresh)
            self.assertEqual(read(fresh / 'state/latest.json'), {'validated': 'keep-me'})
            target = root / 'restored.json'
            self.assertTrue(restore_progress(fresh, target))
            self.assertEqual(read(target), {'target_ids': ['US'], 'attempts': {}})
            receipt = read(fresh / 'progress/receipt.json')
            receipt['collector_signature'] = 'old-parser'
            write(fresh / 'progress/receipt.json', receipt)
            self.assertFalse(restore_progress(fresh, target))
            clear_completed_progress(fresh)
            self.assertFalse(restore_progress(fresh, target))

    def test_failed_fill_accounting_survives_a_new_runner_without_replacing_prices(self):
        def run(*args):
            subprocess.run(['git', *map(str, args)], capture_output=True, check=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, state, candidate = root / 'remote.git', root / 'state', root / 'candidate'
            run('init', '--bare', remote)
            run('init', '-b', 'data', state)
            run('-C', state, 'remote', 'add', 'origin', remote)
            baseline = {'validated': 'keep-the-original-price'}
            write(state / 'state/latest.json', baseline)
            run('-C', state, 'add', '.')
            run('-C', state, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'Keep valid prices independent of retry accounting')
            run('-C', state, 'push', 'origin', 'data')
            ledger = {'targets': {'PH': {'retry_pending': True, 'initial_fill_attempts': 10}}}
            write(candidate / 'collection/ledger.json', ledger)
            write(candidate / 'collection/last-run.json', {'window': {'bootstrap_remaining_count': 0}})
            write(candidate / 'prepare-receipt.json', {'state_head': current_head(state), 'source_head': current_head(ROOT)})
            (candidate / 'evidence').mkdir(parents=True)
            self.assertEqual(persist_collection(SimpleNamespace(candidate_dir=candidate, state_dir=state)), 0)
            fresh = root / 'new-runner'
            run('clone', '--branch', 'data', remote, fresh)
            self.assertEqual(read(fresh / 'state/latest.json'), baseline)
            self.assertEqual(read(fresh / 'collection/ledger.json'), ledger)


if __name__ == '__main__':
    unittest.main()
