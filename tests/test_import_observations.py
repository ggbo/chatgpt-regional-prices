import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.import_observations import ALL_FAILED, ImportBatch, execute_imports, validate_imports
from scripts.pricing import normalize_config


ROOT = Path(__file__).resolve().parents[1]
PLANS_DOC = json.loads((ROOT / "config/plans.json").read_text(encoding="utf-8"))
REGION = {"target_id": "PH", "config_id": "PH", "country_code": "PH", "region_kind": "iso_3166_1"}
REGIONS_DOC = {"regions": [REGION]}


def raw_config(country="PH", plus_amount=1100):
    keys = {"go": 300, "plus": plus_amount, "prolite": 6490, "pro": 9990, "promax": 32490}
    return json.dumps({"country_code": country, "currency_config": {
        "symbol_code": "PHP", **{key: {"month": {"amount": amount, "tax": "inclusive"}} for key, amount in keys.items()}
    }}, separators=(",", ":")).encode()


def write_import(root: Path, raw=None, mutate=None, checkpoint_path="imports/supplement.json", normalize_region=None,
                 page_reader=False, receipt_mutate=None, omit_receipt=False):
    raw = raw or raw_config()
    observed = "2026-10-19T12:34:56Z"
    evidence_sha = hashlib.sha256(raw).hexdigest()
    evidence = root / "evidence" / f"{evidence_sha}.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(raw)
    normalized = normalize_config(raw, normalize_region or REGION, PLANS_DOC["plans"], observed)
    if mutate:
        mutate(normalized)
    attempt = {"kind": "success", "http_status": None if page_reader else 200, "normalized": normalized,
               "observed_at": observed, "evidence_sha256": evidence_sha}
    if page_reader and not omit_receipt:
        source_url = normalized["quotes"][0]["source"]["url"]
        receipt = {
            "kind": "official_page_reader", "captured_at": observed, "source_url": source_url,
            "body_sha256": evidence_sha, "http_status": None, "http_byte_exact": False,
            "representation": "extracted_json_text",
            "reader_output": f"reader result for {source_url}\n{raw.decode('utf-8')}",
        }
        if receipt_mutate:
            receipt_mutate(receipt)
        receipt_raw = json.dumps(receipt, separators=(",", ":"), ensure_ascii=False).encode()
        receipt_sha = hashlib.sha256(receipt_raw).hexdigest()
        (root / "evidence" / f"{receipt_sha}.json").write_bytes(receipt_raw)
        attempt["capture_receipt_sha256"] = receipt_sha
    checkpoint = {"attempts": {"PH": attempt}, "initial_fill_attempt_counts": {"PH": 3}}
    checkpoint_file = root / checkpoint_path
    checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(checkpoint, separators=(",", ":")).encode()
    checkpoint_file.write_bytes(payload)
    spec = {"batch_id": "test", "checkpoints": [{"path": checkpoint_path,
            "sha256": hashlib.sha256(payload).hexdigest(), "target_ids": ["PH"]}]}
    spec_file = root / "imports/spec.json"
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return observed


class ValidationTests(unittest.TestCase):
    def test_path_escape_and_checkpoint_tampering_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_import(root)
            spec = json.loads((root / "imports/spec.json").read_text())
            spec["checkpoints"][0]["path"] = "../outside.json"
            (root / "imports/spec.json").write_text(json.dumps(spec))
            with self.assertRaisesRegex(ValueError, "escaped"):
                validate_imports(root, REGIONS_DOC, PLANS_DOC)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_import(root)
            (root / "imports/supplement.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "SHA256"):
                validate_imports(root, REGIONS_DOC, PLANS_DOC)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_import(root)
            evidence = next((root / "evidence").iterdir())
            evidence.write_bytes(evidence.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "evidence SHA256"):
                validate_imports(root, REGIONS_DOC, PLANS_DOC)

    def test_wrong_country_and_tampered_amount_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_import(root, raw=raw_config(country="US"), normalize_region={**REGION, "country_code": "US", "config_id": "US"})
            with self.assertRaisesRegex(ValueError, "country_mismatch"):
                validate_imports(root, REGIONS_DOC, PLANS_DOC)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_import(root, mutate=lambda normalized: normalized["quotes"][1]["display_price"].update(amount="1"))
            with self.assertRaisesRegex(ValueError, "price evidence mismatch"):
                validate_imports(root, REGIONS_DOC, PLANS_DOC)

    def test_success_keeps_original_observation_time(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            observed = write_import(root)
            batch = validate_imports(root, REGIONS_DOC, PLANS_DOC)[0]
            attempt = batch.checkpoint["attempts"]["PH"]
            self.assertEqual(attempt["observed_at"], observed)
            self.assertTrue(all(quote["last_success_at"] == observed for quote in attempt["normalized"]["quotes"]))

    def test_page_reader_success_keeps_null_http_status_and_adds_audit_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            body = json.loads(raw_config())
            body["currency_config"]["plus"]["month"]["psp_override"] = {
                "amount": 982.14, "tax": "exclusive",
            }
            observed = write_import(
                root, raw=json.dumps(body, separators=(",", ":")).encode(), page_reader=True,
            )
            batch = validate_imports(root, REGIONS_DOC, PLANS_DOC)[0]
            attempt = batch.checkpoint["attempts"]["PH"]
            self.assertIsNone(attempt["http_status"])
            self.assertEqual(attempt["observed_at"], observed)
            receipt_sha = attempt["capture_receipt_sha256"]
            self.assertIn(receipt_sha, batch.evidence)
            for quote in attempt["normalized"]["quotes"]:
                for source in (quote["source"], quote["display_price"]["source"]):
                    self.assertEqual(source["retrieval_method"], "official_page_reader")
                    self.assertEqual(source["evidence_representation"], "extracted_json_text")
                    self.assertIs(source["http_byte_exact"], False)
                    self.assertEqual(source["capture_receipt_sha256"], receipt_sha)
            plus = next(quote for quote in attempt["normalized"]["quotes"] if quote["plan_id"] == "plus")
            self.assertEqual(plus["psp_override"]["source"]["retrieval_method"], "official_page_reader")

    def test_page_reader_success_rejects_missing_or_mismatched_receipts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_import(root, page_reader=True, omit_receipt=True)
            with self.assertRaisesRegex(ValueError, "requires a capture receipt"):
                validate_imports(root, REGIONS_DOC, PLANS_DOC)
        mutations = {
            "time": lambda receipt: receipt.update(captured_at="2026-10-19T12:35:00Z"),
            "url": lambda receipt: receipt.update(source_url="https://chatgpt.com/wrong"),
            "hash": lambda receipt: receipt.update(body_sha256="0" * 64),
        }
        for label, mutate_receipt in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                write_import(root, page_reader=True, receipt_mutate=mutate_receipt)
                with self.assertRaisesRegex(ValueError, "receipt metadata mismatch"):
                    validate_imports(root, REGIONS_DOC, PLANS_DOC)

    def test_unobserved_extra_checkpoint_entries_are_ignored_but_observed_extras_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_import(root)
            checkpoint_path = root / "imports/supplement.json"
            checkpoint = json.loads(checkpoint_path.read_text())
            checkpoint["attempts"]["ZZ"] = {"kind": "not_attempted", "observed_at": None, "reason": "budget"}
            checkpoint["initial_fill_attempt_counts"]["ZZ"] = 0
            payload = json.dumps(checkpoint, separators=(",", ":")).encode()
            checkpoint_path.write_bytes(payload)
            spec_path = root / "imports/spec.json"
            spec = json.loads(spec_path.read_text())
            spec["checkpoints"][0]["sha256"] = hashlib.sha256(payload).hexdigest()
            spec_path.write_text(json.dumps(spec))
            batch = validate_imports(root, REGIONS_DOC, PLANS_DOC)[0]
            self.assertEqual(set(batch.checkpoint["attempts"]), {"PH"})
            checkpoint["attempts"]["ZZ"] = {"kind": "request_failed", "observed_at": "2026-10-19T12:00:00Z", "reason": "timeout"}
            payload = json.dumps(checkpoint, separators=(",", ":")).encode()
            checkpoint_path.write_bytes(payload)
            spec["checkpoints"][0]["sha256"] = hashlib.sha256(payload).hexdigest()
            spec_path.write_text(json.dumps(spec))
            with self.assertRaisesRegex(ValueError, "observed attempt outside"):
                validate_imports(root, REGIONS_DOC, PLANS_DOC)


class ExecutionTests(unittest.TestCase):
    def test_failed_stage_persists_accounting_and_later_batches_continue(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = root / "evidence.json"
            evidence.write_text("{}")
            batches = [
                ImportBatch("failed", ["PH"], {"attempts": {}, "signature": "x"}, {}),
                ImportBatch("success", ["PH"], {"attempts": {}, "signature": "x"}, {}),
            ]
            calls = []

            def runner(command):
                calls.append(command)
                if "prepare" in command:
                    candidate = Path(command[command.index("--candidate-dir") + 1])
                    if "01-failed" in str(candidate):
                        (candidate / "state").mkdir(parents=True, exist_ok=True)
                        (candidate / "collection").mkdir(parents=True, exist_ok=True)
                        (candidate / "state/status.json").write_text(json.dumps({"failures": [ALL_FAILED]}))
                        (candidate / "collection/ledger.json").write_text(json.dumps({"targets": {"PH": {}}}))
                        (candidate / "collection/last-run.json").write_text(json.dumps({"run_id": "test"}))
                        return subprocess.CompletedProcess(command, 1, "rejected", "")
                return subprocess.CompletedProcess(command, 0, "ok", "")

            execute_imports(batches, root / "state", root / "candidate", root / "public", runner=runner)
            rendered = [" ".join(command) for command in calls]
            self.assertTrue(any("persist-collection" in command and "01-failed" in command for command in rendered))
            self.assertTrue(any("--targets PH" in command and "02-success" in command for command in rendered))
            self.assertTrue(any("--mode reuse" in command and "final-reuse" in command for command in rendered))


if __name__ == "__main__":
    unittest.main()
