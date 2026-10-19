"""Retry scheduling is durable and independent from verified price freshness."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scheduling import merge_selected, policy_ledger, record_attempts, select_targets, selected_quality_failures

POLICY = {
    "policy_revision": "serial-72h-v3-fill10",
    "compatible_policy_revisions": ["serial-72h-v1", "serial-72h-v2-retry30m"],
    "window_hours": 72, "target_cycle_hours": 48, "schedule_interval_hours": 1,
    "retry_interval_minutes": 30, "initial_fill_max_attempts_per_target": 10,
}
PLANS = [
    {"plan_id": key, "source_plan_key": source, "billing_interval": "month"}
    for key, source in [("go", "go"), ("plus", "plus"), ("pro_100", "prolite"), ("pro_200", "pro"), ("pro_500", "promax")]
]
NOW = "2026-10-19T00:00:00Z"


def at(minutes=0, hours=0):
    value = datetime(2026, 10, 19, tzinfo=timezone.utc) + timedelta(hours=hours, minutes=minutes)
    return value.isoformat().replace("+00:00", "Z")


def regions(count=252):
    return [{"target_id": f"R{i:03}", "config_id": f"R{i:03}", "country_code": f"R{i:03}",
             "region_kind": "test", "is_country_directory": True} for i in range(count)]


def cells_for(targets, observed=NOW, status="access_blocked"):
    return [{"cell_id": f'{region["config_id"]}:web:{plan["plan_id"]}:month:standard',
             "target_id": region["target_id"], "config_id": region["config_id"], "plan_id": plan["plan_id"],
             "status": status, "attempt_status": status, "http_status": 200 if status == "valid_quote" else 403,
             "last_attempt_at": observed if status != "not_attempted" else None,
             "last_success_at": observed if status == "valid_quote" else None,
             "quote_record_id": None, "reason": None}
            for region in targets for plan in PLANS]


def recorded(targets, status, observed=NOW):
    selection = select_targets(targets, PLANS, None, POLICY, observed)
    return record_attempts(selection, cells_for(targets, observed, status), POLICY, observed, observed, status)[0]


class RetrySchedulingTests(unittest.TestCase):
    def test_retry_becomes_due_at_30_minutes_not_29(self):
        targets = regions(2)
        ledger = recorded(targets, "access_blocked")
        self.assertEqual(select_targets(targets, PLANS, ledger, POLICY, at(minutes=29), retry_only=True)["targets"], [])
        due = select_targets(targets, PLANS, ledger, POLICY, at(minutes=30), retry_only=True)
        self.assertEqual([item["target_id"] for item in due["targets"]], ["R000", "R001"])

    def test_all_five_successes_leave_retry_queue(self):
        targets = regions()
        ledger = recorded(targets, "valid_quote")
        retry = select_targets(targets, PLANS, ledger, POLICY, at(minutes=30), retry_only=True)
        normal = select_targets(targets, PLANS, ledger, POLICY, at(hours=1))
        self.assertEqual(retry["targets"], [])
        self.assertEqual(len(normal["targets"]), 6)
        self.assertTrue(all(not entry["retry_pending"] for entry in normal["ledger"]["targets"].values()))

    def test_partial_five_plan_result_stays_in_retry_queue(self):
        targets = regions(1)
        selection = select_targets(targets, PLANS, None, POLICY, NOW)
        cells = cells_for(targets, status="plan_field_missing")
        cells[0].update(status="valid_quote", attempt_status="valid_quote", http_status=200, last_success_at=NOW)
        ledger, _ = record_attempts(selection, cells, POLICY, NOW, NOW, "partial")
        entry = ledger["targets"]["R000"]
        self.assertTrue(entry["retry_pending"])
        self.assertIsNone(entry["all_five_success_at"])
        self.assertEqual(len(entry["plan_outcomes"]), 5)
        self.assertEqual(len(select_targets(targets, PLANS, ledger, POLICY, at(minutes=30), retry_only=True)["targets"]), 1)

    def test_budget_aborted_retry_targets_keep_old_priority(self):
        targets = regions(3)
        ledger = recorded(targets, "access_blocked")
        due = select_targets(targets, PLANS, ledger, POLICY, at(minutes=30), retry_only=True)
        ledger, run = record_attempts(due, cells_for(targets[1:2], at(minutes=30)), POLICY, at(minutes=30), at(minutes=30), "limited")
        self.assertCountEqual(run["not_started_target_ids"], ["R000", "R002"])
        next_run = select_targets(targets, PLANS, ledger, POLICY, at(minutes=31), retry_only=True)
        self.assertEqual([item["target_id"] for item in next_run["targets"]], ["R000", "R002"])

    def test_failed_attempt_preserves_historical_all_five_success_time(self):
        targets = regions(1)
        ledger = recorded(targets, "valid_quote")
        historical = ledger["targets"]["R000"]["all_five_success_at"]
        selection = select_targets(targets, PLANS, ledger, POLICY, at(hours=1))
        ledger, _ = record_attempts(selection, cells_for(targets, at(hours=1), "access_blocked"), POLICY, at(hours=1), at(hours=1), "failed")
        entry = ledger["targets"]["R000"]
        self.assertEqual(entry["all_five_success_at"], historical)
        self.assertEqual(entry["last_success_at"], historical)
        self.assertTrue(entry["retry_pending"])

    def test_v1_migration_preserves_completed_bootstrap_and_uses_coverage(self):
        targets = regions(2)
        old = recorded(targets, "valid_quote")
        old["policy_revision"] = "serial-72h-v1"
        old["bootstrap"]["completed_at"] = NOW
        for entry in old["targets"].values():
            for field in ("retry_pending", "retry_due_at", "all_five_success_at", "plan_outcomes"):
                entry.pop(field, None)
            entry["last_attempt_status"] = "valid_quote"
        coverage = cells_for(targets, status="valid_quote")
        for cell in coverage[5:]:
            cell.update(status="stale_quote", attempt_status="access_blocked", http_status=403, last_attempt_at=NOW)
        migrated = select_targets(targets, PLANS, old, POLICY, at(minutes=30), retry_only=True, previous_cells=coverage)
        self.assertFalse(migrated["bootstrap"])
        self.assertEqual(migrated["ledger"]["migrated_from_policy_revision"], "serial-72h-v1")
        self.assertFalse(migrated["ledger"]["targets"]["R000"]["retry_pending"])
        self.assertTrue(migrated["ledger"]["targets"]["R001"]["retry_pending"])
        self.assertEqual([item["target_id"] for item in migrated["targets"]], ["R001"])
        unknown = dict(old, policy_revision="unknown")
        self.assertTrue(select_targets(targets, PLANS, unknown, POLICY, NOW, previous_cells=coverage)["bootstrap"])

    def test_v2_migration_preserves_initial_fill_attempt_count(self):
        targets = regions(1)
        old = recorded(targets, "access_blocked")
        old["policy_revision"] = "serial-72h-v2-retry30m"
        old["targets"]["R000"]["initial_fill_attempts"] = 6
        migrated = select_targets(targets, PLANS, old, POLICY, at(minutes=1), fill_missing=True)
        self.assertEqual(migrated["ledger"]["migrated_from_policy_revision"], "serial-72h-v2-retry30m")
        self.assertEqual(migrated["ledger"]["targets"]["R000"]["initial_fill_attempts"], 6)

    def test_missing_ledger_is_seeded_from_full_historical_coverage(self):
        targets = regions(2)
        coverage = cells_for(targets, observed=at(hours=1), status="valid_quote")
        for cell in coverage[5:]:
            cell.update(status="stale_quote", attempt_status="access_blocked", http_status=403, last_success_at=None)
        selection = select_targets(targets, PLANS, None, POLICY, at(hours=1, minutes=29), retry_only=True, previous_cells=coverage)
        self.assertFalse(selection["bootstrap"])
        self.assertEqual(selection["ledger"]["bootstrap"]["completed_at"], at(hours=1))
        self.assertEqual(selection["targets"], [])
        due = select_targets(targets, PLANS, selection["ledger"], POLICY, at(hours=1, minutes=30), retry_only=True)
        self.assertEqual([item["target_id"] for item in due["targets"]], ["R001"])

    def test_hourly_union_includes_due_retries_and_six_normal_targets(self):
        targets = regions()
        ledger = recorded(targets, "valid_quote")
        failed_selection = {"ledger": ledger, "targets": targets[:2], "bootstrap": False, "mode": "rotation"}
        ledger, _ = record_attempts(failed_selection, cells_for(targets[:2], at(hours=1), "access_blocked"), POLICY, at(hours=1), at(hours=1), "failed")
        retry = select_targets(targets, PLANS, ledger, POLICY, at(hours=1, minutes=30), retry_only=True)
        hourly = select_targets(targets, PLANS, ledger, POLICY, at(hours=1, minutes=30))
        self.assertEqual([item["target_id"] for item in retry["targets"]], ["R000", "R001"])
        self.assertEqual(len(hourly["targets"]), 8)
        self.assertEqual(hourly["normal_batch_size"], 6)
        self.assertEqual([item["target_id"] for item in hourly["targets"][:6]], [f"R{i:03}" for i in range(2, 8)])
        self.assertEqual([item["target_id"] for item in hourly["targets"][6:]], ["R000", "R001"])

    def test_not_attempted_cells_do_not_advance_ledger(self):
        targets = regions(1)
        selection = select_targets(targets, PLANS, None, POLICY, NOW)
        ledger, run = record_attempts(selection, cells_for(targets, status="not_attempted"), POLICY, NOW, NOW, "empty")
        self.assertIsNone(ledger["targets"]["R000"]["last_attempt_at"])
        self.assertEqual(run["not_started_target_ids"], ["R000"])
        self.assertEqual(run["window"]["bootstrap_remaining_count"], 1)

    def test_initial_fill_ignores_30_minute_delay_and_only_adds_at_risk_healthy(self):
        targets = regions(3)
        ledger = recorded(targets, "access_blocked")
        recovered = {"ledger": ledger, "targets": targets[1:], "bootstrap": False, "mode": "retry"}
        ledger, _ = record_attempts(
            recovered, cells_for(targets[1:], at(minutes=30), "valid_quote"), POLICY,
            at(minutes=30), at(minutes=30), "recovered",
        )
        immediate = select_targets(targets, PLANS, ledger, POLICY, at(hours=1, minutes=1), fill_missing=True)
        self.assertEqual([item["target_id"] for item in immediate["targets"]], ["R000"])
        self.assertEqual(immediate["mode"], "fill")
        old = at(hours=1)
        for key in ("R001", "R002"):
            ledger["targets"][key]["last_attempt_at"] = old
        at_risk = select_targets(targets, PLANS, ledger, POLICY, at(hours=49), fill_missing=True)
        self.assertEqual([item["target_id"] for item in at_risk["targets"]], ["R000", "R001", "R002"])

    def test_successful_initial_target_does_not_reenter_fill_after_rotation_failure(self):
        targets = regions(1)
        ledger = recorded(targets, "valid_quote")
        self.assertTrue(ledger["targets"]["R000"]["initial_fill_done"])
        failed = {"ledger": ledger, "targets": targets, "bootstrap": False, "mode": "rotation"}
        ledger, _ = record_attempts(
            failed, cells_for(targets, at(hours=1), "access_blocked"), POLICY,
            at(hours=1), at(hours=1), "later-failure",
        )
        self.assertEqual(select_targets(targets, PLANS, ledger, POLICY, at(hours=1, minutes=1), fill_missing=True)["targets"], [])
        self.assertEqual(select_targets(targets, PLANS, ledger, POLICY, at(hours=1, minutes=29), retry_only=True)["targets"], [])
        self.assertEqual(
            [item["target_id"] for item in select_targets(
                targets, PLANS, ledger, POLICY, at(hours=1, minutes=30), retry_only=True,
            )["targets"]],
            ["R000"],
        )

    def test_healthy_target_outside_initial_cohort_only_uses_timed_retry(self):
        targets = regions(2)
        old = recorded(targets, "valid_quote")
        old.pop("initial_fill", None)
        old["policy_revision"] = "serial-72h-v2-retry30m"
        old["targets"]["R000"].update(retry_pending=True, initial_fill_done=False)
        migrated = select_targets(targets, PLANS, old, POLICY, at(minutes=1), fill_missing=True)
        self.assertEqual(migrated["ledger"]["initial_fill"]["cohort_target_ids"], ["R000"])
        failed = {"ledger": migrated["ledger"], "targets": targets[1:], "bootstrap": False, "mode": "rotation"}
        ledger, _ = record_attempts(
            failed, cells_for(targets[1:], at(minutes=2), "access_blocked"), POLICY,
            at(minutes=2), at(minutes=2), "outside-failure",
        )
        fill = select_targets(targets, PLANS, ledger, POLICY, at(minutes=3), fill_missing=True)
        self.assertEqual([item["target_id"] for item in fill["targets"]], ["R000"])
        self.assertNotIn("R001", fill["ledger"]["initial_fill"]["cohort_target_ids"])

    def test_initial_fill_progress_and_budget_abort_request_immediate_continuation(self):
        targets = regions(2)
        ledger = recorded(targets, "access_blocked")
        fill = select_targets(targets, PLANS, ledger, POLICY, at(minutes=1), fill_missing=True)
        ledger, run = record_attempts(fill, cells_for(targets[:1], at(minutes=1), "valid_quote"), POLICY, at(minutes=1), at(minutes=1), "fill-1")
        self.assertEqual(run["initial_fill_progress_count"], 1)
        self.assertEqual(run["not_started_target_ids"], ["R001"])
        self.assertTrue(run["immediate_continuation"])
        self.assertEqual(ledger["initial_fill"]["pending_target_ids"], ["R001"])
        self.assertEqual(ledger["targets"]["R000"]["initial_fill_attempts"], 1)
        self.assertEqual(ledger["targets"]["R001"]["initial_fill_attempts"], 0)
        final = select_targets(targets, PLANS, ledger, POLICY, at(minutes=2), fill_missing=True)
        ledger, run = record_attempts(final, cells_for(targets[1:], at(minutes=2), "valid_quote"), POLICY, at(minutes=2), at(minutes=2), "fill-2")
        self.assertEqual(run["initial_fill_pending_count"], 0)
        self.assertFalse(run["immediate_continuation"])
        self.assertFalse(ledger["initial_fill"]["active"])
        self.assertEqual(ledger["targets"]["R001"]["initial_fill_attempts"], 1)

    def test_initial_fill_caps_each_failed_target_at_ten_attempts(self):
        targets = regions(1)
        ledger = recorded(targets, "access_blocked")
        last_run = None
        for number in range(1, 11):
            fill = select_targets(targets, PLANS, ledger, POLICY, at(minutes=number), fill_missing=True)
            self.assertEqual([item["target_id"] for item in fill["targets"]], ["R000"])
            ledger, last_run = record_attempts(
                fill, cells_for(targets, at(minutes=number), "access_blocked"), POLICY,
                at(minutes=number), at(minutes=number), f"fill-{number}",
            )
            self.assertEqual(ledger["targets"]["R000"]["initial_fill_attempts"], number)
        self.assertEqual(last_run["initial_fill_exhausted_target_ids"], ["R000"])
        self.assertEqual(last_run["initial_fill_exhausted_count"], 1)
        self.assertEqual(last_run["initial_fill_eligible_count"], 0)
        self.assertFalse(last_run["immediate_continuation"])
        self.assertTrue(ledger["targets"]["R000"]["retry_pending"])
        self.assertEqual(select_targets(targets, PLANS, ledger, POLICY, at(minutes=11), fill_missing=True)["targets"], [])

    def test_replayed_fill_observation_does_not_increment_attempt_count(self):
        targets = regions(1)
        ledger = recorded(targets, "access_blocked")
        fill = select_targets(targets, PLANS, ledger, POLICY, at(minutes=1), fill_missing=True)
        ledger, _ = record_attempts(
            fill, cells_for(targets, at(minutes=1), "access_blocked"), POLICY,
            at(minutes=1), at(minutes=1), "fill-1",
        )
        replay = select_targets(targets, PLANS, ledger, POLICY, at(minutes=2), fill_missing=True)
        ledger, run = record_attempts(
            replay, cells_for(targets, at(minutes=1), "access_blocked"), POLICY,
            at(minutes=2), at(minutes=2), "replay", observed_fill_counts={"R000": 7},
        )
        self.assertEqual(run["resumed_target_ids"], ["R000"])
        self.assertEqual(ledger["targets"]["R000"]["initial_fill_attempts"], 1)

    def test_imported_fill_counts_never_decrease_and_include_current_attempt(self):
        targets = regions(1)
        ledger = recorded(targets, "access_blocked")
        fill = select_targets(targets, PLANS, ledger, POLICY, at(minutes=1), fill_missing=True)
        ledger, _ = record_attempts(
            fill, cells_for(targets, at(minutes=1), "access_blocked"), POLICY,
            at(minutes=1), at(minutes=1), "import-3", observed_fill_counts={"R000": 3},
        )
        self.assertEqual(ledger["targets"]["R000"]["initial_fill_attempts"], 3)
        ledger["targets"]["R000"]["initial_fill_attempts"] = 4
        next_fill = select_targets(targets, PLANS, ledger, POLICY, at(minutes=2), fill_missing=True)
        ledger, _ = record_attempts(
            next_fill, cells_for(targets, at(minutes=2), "access_blocked"), POLICY,
            at(minutes=2), at(minutes=2), "import-2", observed_fill_counts={"R000": 2},
        )
        self.assertEqual(ledger["targets"]["R000"]["initial_fill_attempts"], 5)

    def test_imported_fill_count_enrolls_actual_target_outside_initial_cohort(self):
        targets = regions(2)
        old = recorded(targets, "valid_quote")
        old.pop("initial_fill", None)
        old["policy_revision"] = "serial-72h-v2-retry30m"
        old["targets"]["R000"].update(retry_pending=True, initial_fill_done=False)
        old["targets"]["R001"]["last_attempt_at"] = at(hours=-49)
        fill = select_targets(targets, PLANS, old, POLICY, NOW, fill_missing=True)
        self.assertEqual([item["target_id"] for item in fill["targets"]], ["R000", "R001"])
        ledger, _ = record_attempts(
            fill, cells_for(targets[1:], at(minutes=1), "access_blocked"), POLICY,
            at(minutes=1), at(minutes=1), "import-outside", observed_fill_counts={"R001": 3},
        )
        self.assertIn("R001", ledger["initial_fill"]["cohort_target_ids"])
        self.assertEqual(ledger["targets"]["R001"]["initial_fill_attempts"], 3)
        self.assertFalse(ledger["targets"]["R001"]["initial_fill_done"])

    def test_imported_fill_count_must_be_bounded_integer(self):
        targets = regions(1)
        ledger = recorded(targets, "access_blocked")
        fill = select_targets(targets, PLANS, ledger, POLICY, at(minutes=1), fill_missing=True)
        for invalid in (True, -1, 11, 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                record_attempts(
                    fill, cells_for(targets, at(minutes=1), "access_blocked"), POLICY,
                    at(minutes=1), at(minutes=1), "invalid", observed_fill_counts={"R000": invalid},
                )

    def test_exhausted_fill_target_remains_eligible_for_half_hour_retry(self):
        targets = regions(1)
        ledger = recorded(targets, "access_blocked")
        ledger["targets"]["R000"]["initial_fill_attempts"] = 10
        ledger["targets"]["R000"]["last_attempt_at"] = at(minutes=10)
        ledger["targets"]["R000"]["retry_due_at"] = at(minutes=40)
        self.assertEqual(select_targets(targets, PLANS, ledger, POLICY, at(minutes=39), retry_only=True)["targets"], [])
        due = select_targets(targets, PLANS, ledger, POLICY, at(minutes=40), retry_only=True)
        self.assertEqual([item["target_id"] for item in due["targets"]], ["R000"])
        ledger, _ = record_attempts(
            due, cells_for(targets, at(minutes=40), "valid_quote"), POLICY,
            at(minutes=40), at(minutes=40), "retry-success",
        )
        self.assertEqual(ledger["targets"]["R000"]["initial_fill_attempts"], 10)
        self.assertFalse(ledger["targets"]["R000"]["retry_pending"])

    def test_exhausted_pending_target_does_not_pull_healthy_targets_into_fill(self):
        targets = regions(2)
        ledger = recorded(targets, "valid_quote")
        ledger["targets"]["R000"].update(retry_pending=True, initial_fill_attempts=10)
        ledger["targets"]["R001"]["last_attempt_at"] = at(hours=-49)
        selection = select_targets(targets, PLANS, ledger, POLICY, NOW, fill_missing=True)
        self.assertEqual(selection["targets"], [])
        self.assertEqual(selection["ledger"]["initial_fill"]["exhausted_target_ids"], ["R000"])


class MergeTests(unittest.TestCase):
    def test_failed_attempt_keeps_existing_price_success_time(self):
        targets = regions(1)
        old_cells = cells_for(targets, status="valid_quote")
        old_quotes = []
        for cell in old_cells:
            cell["quote_record_id"] = cell["cell_id"]
            old_quotes.append({"record_id": cell["cell_id"], "last_success_at": NOW})
        failed = cells_for(targets, at(hours=1), "access_blocked")
        for cell in failed:
            cell.update(status="stale_quote", quote_record_id=cell["cell_id"], last_success_at=NOW)
        quotes, merged = merge_selected(targets, PLANS, old_quotes, old_cells, {"cells": failed, "quotes": old_quotes})
        self.assertEqual(len(quotes), 5)
        self.assertTrue(all(cell["last_success_at"] == NOW for cell in merged))

    def test_quality_failure_is_limited_to_selected_parsed_targets(self):
        targets = regions(1)
        old = cells_for(targets, status="valid_quote")
        failed = cells_for(targets, status="access_blocked")
        self.assertIn("all_selected_sources_failed", selected_quality_failures(old, {"cells": failed}, {"R000"})[0])


if __name__ == "__main__":
    unittest.main()
