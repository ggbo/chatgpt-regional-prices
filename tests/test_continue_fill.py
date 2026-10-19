import unittest
from scripts.continue_fill import should_continue


class ContinuationTests(unittest.TestCase):
    def test_only_the_persisted_fill_run_can_continue(self):
        ledger = {'targets': {'PH': {'retry_pending': True, 'initial_fill_attempts': 2}}}
        run = {'run_id': '123', 'mode': 'fill', 'immediate_continuation': True}
        self.assertTrue(should_continue(ledger, run, '123'))
        self.assertFalse(should_continue(ledger, run, '124'))
        self.assertFalse(should_continue(ledger, dict(run, mode='retry'), '123'))

    def test_success_stops_but_failures_beyond_ten_continue(self):
        run = {'run_id': '123', 'mode': 'fill', 'immediate_continuation': True}
        self.assertFalse(should_continue(
            {'targets': {'PH': {'retry_pending': False, 'initial_fill_attempts': 1}}}, run, '123', start=True,
        ))
        self.assertTrue(should_continue(
            {'targets': {'PH': {'retry_pending': True, 'initial_fill_attempts': 12}}}, run, '123', start=True,
        ))

    def test_legacy_exhausted_failure_restarts_but_success_does_not(self):
        failed = {'initial_fill': {'cohort_target_ids': ['PH'], 'exhausted_target_ids': ['PH']}, 'targets': {
            'PH': {'retry_pending': True, 'initial_fill_attempts': 10, 'initial_fill_done': True,
                   'all_five_success_at': '2026-10-01T00:00:00Z'},
        }}
        successful = {'initial_fill': {'cohort_target_ids': ['PH']}, 'targets': {
            'PH': {'retry_pending': True, 'initial_fill_attempts': 10, 'initial_fill_done': True,
                   'all_five_success_at': '2026-10-19T00:00:00Z'},
        }}
        self.assertTrue(should_continue(failed, None, '123', start=True))
        self.assertFalse(should_continue(successful, None, '123', start=True))

    def test_explicit_start_does_not_require_a_previous_fill_run(self):
        ledger = {'targets': {'PH': {'retry_pending': True, 'initial_fill_attempts': 0}}}
        self.assertTrue(should_continue(ledger, None, '123', start=True))

    def test_completed_cohort_does_not_restart_for_later_rotation_failures(self):
        ledger = {'initial_fill': {'cohort_target_ids': ['PH']}, 'targets': {
            'PH': {'retry_pending': True, 'initial_fill_attempts': 1, 'initial_fill_done': True,
                   'all_five_success_at': '2026-10-19T00:00:00Z'},
            'CL': {'retry_pending': True, 'initial_fill_attempts': 0},
        }}
        self.assertFalse(should_continue(ledger, None, '123', start=True))
