import unittest
from scripts.continue_fill import should_continue


class ContinuationTests(unittest.TestCase):
    def test_only_the_persisted_fill_run_can_continue(self):
        ledger = {'targets': {'PH': {'retry_pending': True, 'initial_fill_attempts': 2}}}
        run = {'run_id': '123', 'mode': 'fill', 'immediate_continuation': True}
        self.assertTrue(should_continue(ledger, run, '123'))
        self.assertFalse(should_continue(ledger, run, '124'))
        self.assertFalse(should_continue(ledger, dict(run, mode='retry'), '123'))

    def test_success_and_tenth_failure_stop_immediate_continuations(self):
        run = {'run_id': '123', 'mode': 'fill', 'immediate_continuation': True}
        for entry in ({'retry_pending': False, 'initial_fill_attempts': 1}, {'retry_pending': True, 'initial_fill_attempts': 10}):
            ledger = {'targets': {'PH': entry}}
            self.assertFalse(should_continue(ledger, run, '123', start=True))

    def test_explicit_start_does_not_require_a_previous_fill_run(self):
        ledger = {'targets': {'PH': {'retry_pending': True, 'initial_fill_attempts': 0}}}
        self.assertTrue(should_continue(ledger, None, '123', start=True))

    def test_completed_cohort_does_not_restart_for_later_rotation_failures(self):
        ledger = {'initial_fill': {'cohort_target_ids': ['PH']}, 'targets': {
            'PH': {'retry_pending': True, 'initial_fill_attempts': 1, 'initial_fill_done': True},
            'CL': {'retry_pending': True, 'initial_fill_attempts': 0},
        }}
        self.assertFalse(should_continue(ledger, None, '123', start=True))
