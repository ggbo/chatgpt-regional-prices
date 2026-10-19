import copy
import io
import json
import tempfile
import time
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

from scripts.collect import collect


PLANS = [
    {"plan_id": "go", "source_plan_key": "go", "billing_interval": "month"},
    {"plan_id": "plus", "source_plan_key": "plus", "billing_interval": "month"},
    {"plan_id": "pro_100", "source_plan_key": "prolite", "billing_interval": "month"},
    {"plan_id": "pro_200", "source_plan_key": "pro", "billing_interval": "month"},
    {"plan_id": "pro_500", "source_plan_key": "promax", "billing_interval": "month"},
]
REGION = {"target_id": "PH", "country_code": "PH", "config_id": "PH", "region_kind": "iso_3166_1", "is_country_directory": True}


class Headers(Message):
    pass


class Response:
    def __init__(self, body):
        self.body = body
        self.status = 200
        self.headers = Headers()
        self.headers["Content-Type"] = "application/json"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.body[:limit]


def body(amount=1100, psp=982.14, psp_tax="exclusive"):
    month = {"amount": amount, "tax": "inclusive"}
    if psp is not None:
        month["psp_override"] = {"amount": psp, "tax": psp_tax}
    return json.dumps({"country_code": "PH", "currency_config": {"symbol_code": "PHP", "plus": {"month": month}}}).encode()


class CollectTests(unittest.TestCase):
    def test_initial_fill_counts_real_passes_and_resume_never_adds_a_pass(self):
        def blocked(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, 'blocked', {}, io.BytesIO())
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = Path(temp) / 'fill.json'
            first = collect([REGION], PLANS, [], temp, opener=blocked, initial_fill=True, initial_fill_counts={'PH': 2}, checkpoint_path=checkpoint)
            self.assertEqual(first['initial_fill_attempt_counts'], {'PH': 3})
            replay = collect([REGION], PLANS, [], temp, opener=lambda *_a, **_k: self.fail('must replay'), initial_fill=True, checkpoint_path=checkpoint)
            self.assertEqual(replay['initial_fill_attempt_counts'], {'PH': 3})
            beyond_ten = collect([REGION], PLANS, [], temp, opener=blocked, initial_fill=True, initial_fill_counts={'PH': 12})
            self.assertEqual(beyond_ten['summary']['attempted_request_count'], 1)
            self.assertEqual(beyond_ten['initial_fill_attempt_counts'], {'PH': 13})

    def test_serial_retry_preserves_scheduler_order_instead_of_alphabetic_order(self):
        targets = [{**REGION, 'target_id': code, 'config_id': code, 'country_code': code} for code in ('ZW', 'US', 'AD')]
        calls = []
        def blocked(request, timeout):
            calls.append(request.full_url.rsplit('/', 1)[-1])
            raise urllib.error.HTTPError(request.full_url, 403, 'blocked', {}, io.BytesIO())
        with tempfile.TemporaryDirectory() as temp:
            collect(targets, PLANS, [], temp, concurrency=1, probe_first=False, opener=blocked)
        self.assertEqual(calls, ['ZW', 'US', 'AD'])

    def test_request_interval_spaces_concurrent_request_starts(self):
        regions = [
            {**REGION, "target_id": code, "country_code": code, "config_id": code}
            for code in ("AI", "HU", "NC")
        ]
        starts = []
        def timed_response(request, timeout):
            starts.append(time.monotonic())
            target = request.full_url.rsplit("/", 1)[-1]
            payload = json.dumps({"country_code": target, "currency_config": {"symbol_code": "USD", "plus": {"month": {"amount": 20, "tax": "exclusive"}}}}).encode()
            return Response(payload)
        with tempfile.TemporaryDirectory() as temp:
            result = collect(regions, PLANS, [], temp, concurrency=2, min_request_interval_seconds=0.03, opener=timed_response)
        self.assertEqual(result["summary"]["attempted_request_count"], 3)
        ordered = sorted(starts)
        self.assertTrue(all(later - earlier >= 0.025 for earlier, later in zip(ordered, ordered[1:])))

    def test_request_interval_budget_marks_unscheduled_target_not_attempted(self):
        regions = [
            {**REGION, "target_id": code, "country_code": code, "config_id": code}
            for code in ("AI", "HU")
        ]
        calls = []
        def response(request, timeout):
            target = request.full_url.rsplit("/", 1)[-1]
            calls.append(target)
            payload = json.dumps({"country_code": target, "currency_config": {"symbol_code": "USD", "plus": {"month": {"amount": 20, "tax": "exclusive"}}}}).encode()
            return Response(payload)
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = Path(temp) / "checkpoint.json"
            result = collect(regions, PLANS, [], temp, budget_seconds=0.02, concurrency=1, min_request_interval_seconds=0.1, checkpoint_path=checkpoint, opener=response)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["summary"]["attempted_request_count"], 1)
            self.assertEqual(result["summary"]["not_attempted_request_count"], 1)
            resumed = collect(regions, PLANS, [], temp, budget_seconds=1, concurrency=1, min_request_interval_seconds=0, checkpoint_path=checkpoint, opener=response)
        self.assertEqual(len(calls), 2)
        self.assertEqual(resumed["summary"]["attempted_request_count"], 2)
        self.assertEqual(resumed["summary"]["not_attempted_request_count"], 0)

    def test_shared_structural_probe_failure_stops_remaining_requests_explicitly(self):
        regions = [
            {**REGION, "target_id": "US", "country_code": "US", "config_id": "US"},
            REGION,
            {**REGION, "target_id": "AI", "country_code": "AI", "config_id": "AI"},
        ]
        calls = []
        def structurally_invalid(request, timeout):
            target = request.full_url.rsplit("/", 1)[-1]
            calls.append(target)
            return Response(json.dumps({"country_code": target, "unexpected": {}}).encode())
        with tempfile.TemporaryDirectory() as temp:
            result = collect(regions, PLANS, [], temp, opener=structurally_invalid)
        self.assertCountEqual(calls, ["US", "PH"])
        self.assertTrue(result["source_status"]["structural_failure"])
        self.assertEqual(result["summary"]["not_attempted_request_count"], 1)
        ai_cells = [cell for cell in result["cells"] if cell["target_id"] == "AI"]
        self.assertTrue(all(cell["status"] == "not_attempted" for cell in ai_cells))
        self.assertTrue(all("structural probe failure" in cell["reason"] for cell in ai_cells))

    def test_partial_failure_retains_only_old_affected_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            first = collect([REGION], PLANS, [], temp, opener=lambda *_a, **_k: Response(body()))
            old = first["quotes"]
            second_body = json.dumps({"country_code": "PH", "currency_config": {"symbol_code": "PHP", "go": {"month": {"amount": 300, "tax": "inclusive"}}}}).encode()
            second = collect([REGION], PLANS, old, temp, opener=lambda *_a, **_k: Response(second_body))
            plus = next(c for c in second["cells"] if c["plan_id"] == "plus")
            go = next(c for c in second["cells"] if c["plan_id"] == "go")
            self.assertEqual(plus["status"], "stale_quote")
            self.assertEqual(plus["attempt_status"], "plan_field_missing")
            self.assertEqual(go["status"], "valid_quote")

    def test_invalid_psp_retains_its_own_old_value_and_time(self):
        with tempfile.TemporaryDirectory() as temp:
            first = collect([REGION], PLANS, [], temp, opener=lambda *_a, **_k: Response(body()))
            old_psp = copy.deepcopy(first["quotes"][0]["psp_override"])
            invalid = body(amount=1100, psp=-1)
            second = collect([REGION], PLANS, first["quotes"], temp, opener=lambda *_a, **_k: Response(invalid))
            quote = second["quotes"][0]
            self.assertEqual(quote["psp_override_status"], "stale")
            self.assertEqual(quote["psp_override"], old_psp)

    def test_large_change_quarantines_until_hash_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            first = collect([REGION], PLANS, [], temp, opener=lambda *_a, **_k: Response(body()))
            second = collect([REGION], PLANS, first["quotes"], temp, opener=lambda *_a, **_k: Response(body(amount=2000)))
            self.assertEqual(second["cells"][1]["status"], "stale_quote")
            fingerprint = second["change_reviews"][0]["change_hash"]
            accepted = collect([REGION], PLANS, first["quotes"], temp, accepted_change_hashes={fingerprint}, opener=lambda *_a, **_k: Response(body(amount=2000)))
            quote = next(q for q in accepted["quotes"] if q["plan_id"] == "plus")
            self.assertEqual(quote["display_price"]["amount"], "2000")

    def test_display_quarantine_still_accepts_independent_psp_update(self):
        with tempfile.TemporaryDirectory() as temp:
            first = collect([REGION], PLANS, [], temp, opener=lambda *_a, **_k: Response(body()))
            old = first["quotes"][0]
            second = collect([REGION], PLANS, first["quotes"], temp, opener=lambda *_a, **_k: Response(body(amount=2000, psp=990)))
            quote = second["quotes"][0]
            self.assertEqual(second["cells"][1]["status"], "stale_quote")
            self.assertEqual(quote["display_price"]["amount"], "1100")
            self.assertEqual(quote["source"], old["source"])
            self.assertEqual(quote["psp_override"]["amount"], "990")
            self.assertNotEqual(quote["psp_override"]["source"], old["psp_override"]["source"])

    def test_all_failed_preserves_old_and_marks_source(self):
        with tempfile.TemporaryDirectory() as temp:
            first = collect([REGION], PLANS, [], temp, opener=lambda *_a, **_k: Response(body()))
            def blocked(request, timeout):
                raise urllib.error.HTTPError(request.full_url, 403, "blocked", {}, io.BytesIO())
            failed = collect([REGION], PLANS, first["quotes"], temp, opener=blocked)
            self.assertTrue(failed["source_status"]["all_source_failed"])
            plus = next(c for c in failed["cells"] if c["plan_id"] == "plus")
            self.assertEqual(plus["status"], "stale_quote")
            self.assertEqual(plus["attempt_status"], "access_blocked")
            self.assertEqual(failed["summary"]["expected_cell_count"], 5)

    def test_checkpoint_is_resumed(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            checkpoint = Path(temp) / "checkpoint.json"
            first = collect([REGION], PLANS, [], temp, checkpoint_path=checkpoint, opener=lambda *_a, **_k: (calls.append(1) or Response(body())))
            second = collect([REGION], PLANS, [], temp, checkpoint_path=checkpoint, opener=lambda *_a, **_k: (calls.append(1) or Response(body())))
            self.assertEqual(len(calls), 1)
            self.assertEqual(first["summary"], second["summary"])

    def test_legacy_checkpoint_is_migrated_when_catalog_and_plan_mapping_match(self):
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = Path(temp) / "checkpoint.json"
            collect([REGION], PLANS, [], temp, checkpoint_path=checkpoint, opener=lambda *_a, **_k: Response(body()))
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            saved.pop("signature")
            saved["target_ids"] = ["PH"]
            checkpoint.write_text(json.dumps(saved), encoding="utf-8")
            result = collect([REGION], PLANS, [], temp, checkpoint_path=checkpoint, opener=lambda *_a, **_k: self.fail("compatible checkpoint should resume"))
            migrated = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertIn("signature", migrated)
            self.assertEqual(result["summary"]["successful_response_count"], 1)

    def test_zero_budget_is_not_counted_as_attempted(self):
        with tempfile.TemporaryDirectory() as temp:
            result = collect([REGION], PLANS, [], temp, budget_seconds=0, opener=lambda *_a, **_k: self.fail("network should not be called"))
        self.assertEqual(result["summary"]["attempted_request_count"], 0)
        self.assertEqual(result["summary"]["not_attempted_request_count"], 1)
        self.assertTrue(all(cell["last_attempt_at"] is None for cell in result["cells"]))

    def test_accepted_currency_change_never_carries_old_psp(self):
        with tempfile.TemporaryDirectory() as temp:
            first = collect([REGION], PLANS, [], temp, opener=lambda *_a, **_k: Response(body()))
            changed_body = json.dumps({"country_code": "PH", "currency_config": {"symbol_code": "USD", "plus": {"month": {"amount": 20, "tax": "exclusive", "psp_override": {"amount": -1}}}}}).encode()
            review = collect([REGION], PLANS, first["quotes"], temp, opener=lambda *_a, **_k: Response(changed_body))
            fingerprint = review["change_reviews"][0]["change_hash"]
            accepted = collect([REGION], PLANS, first["quotes"], temp, accepted_change_hashes={fingerprint}, opener=lambda *_a, **_k: Response(changed_body))
            quote = accepted["quotes"][0]
            self.assertEqual(quote["currency"], "USD")
            self.assertIsNone(quote["psp_override"])
            self.assertEqual(quote["psp_override_status"], "invalid")


if __name__ == "__main__":
    unittest.main()
