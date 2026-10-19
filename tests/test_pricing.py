import json
import unittest
from pathlib import Path

from scripts.pricing import normalize_config


PLANS = [
    {"plan_id": "go", "source_plan_key": "go", "billing_interval": "month"},
    {"plan_id": "plus", "source_plan_key": "plus", "billing_interval": "month"},
    {"plan_id": "pro_100", "source_plan_key": "prolite", "billing_interval": "month"},
    {"plan_id": "pro_200", "source_plan_key": "pro", "billing_interval": "month"},
    {"plan_id": "pro_500", "source_plan_key": "promax", "billing_interval": "month"},
]
FIXTURES = Path(__file__).parent / "fixtures"


def raw_config(country="PH", currency="PHP", **config):
    body = {"country_code": country, "currency_config": {"symbol_code": currency, **config}}
    return json.dumps(body, separators=(",", ":")).encode()


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.target = {"target_id": "PH", "country_code": "PH", "config_id": "PH", "region_kind": "iso_3166_1"}
        self.at = "2026-10-19T00:00:00Z"

    def test_ph_plus_preserves_display_and_psp(self):
        raw = (FIXTURES / "ph.json").read_bytes()
        result = normalize_config(raw, self.target, PLANS, self.at)
        quote = result["quotes"][0]
        self.assertEqual(quote["display_price"]["amount"], "1100.0")
        self.assertEqual(quote["psp_override"]["amount"], "982.14")
        self.assertEqual(quote["display_price"]["tax"], "inclusive")
        self.assertEqual(quote["psp_override"]["tax"], "exclusive")
        self.assertEqual(len(result["plan_results"]), 5)

    def test_jpy_psp_fraction_is_not_truncated(self):
        target = {**self.target, "target_id": "JP", "country_code": "JP", "config_id": "JP"}
        raw = (FIXTURES / "jp.json").read_bytes()
        quote = normalize_config(raw, target, PLANS, self.at)["quotes"][0]
        self.assertEqual(quote["psp_override"]["amount"], "15272.73")

    def test_cl_pro100_keeps_non_reconstructable_official_values(self):
        target = {**self.target, "target_id": "CL", "country_code": "CL", "config_id": "CL"}
        raw = (FIXTURES / "cl.json").read_bytes()
        quote = normalize_config(raw, target, PLANS, self.at)["quotes"][0]
        self.assertEqual(quote["display_price"]["amount"], "102990")
        self.assertEqual(quote["psp_override"]["amount"], "86364.78")

    def test_missing_plan_and_unknown_tax_are_explicit(self):
        raw = raw_config(plus={"month": {"amount": 1100, "tax": "unexpected"}})
        result = normalize_config(raw, self.target, PLANS, self.at)
        self.assertEqual(result["quotes"][0]["display_price"]["tax"], "unknown")
        self.assertEqual(result["plan_results"][0]["status"], "plan_field_missing")

    def test_invalid_metadata_does_not_discard_valid_plan(self):
        raw = raw_config(plus={"month": {"amount": 1100, "tax": "inclusive"}}, minor_unit_exponent=1.5, tax_percent=-1)
        result = normalize_config(raw, self.target, PLANS, self.at)
        self.assertEqual(len(result["quotes"]), 1)
        self.assertIsNone(result["quotes"][0]["minor_unit_exponent"])
        self.assertEqual(len([i for i in result["issues"] if "metadata_error" in i]), 2)
        self.assertEqual(len([i for i in result["quotes"][0]["issues"] if "metadata_error" in i]), 2)

    def test_nan_and_country_mismatch_rejected(self):
        invalid = b'{"country_code":"PH","currency_config":{"symbol_code":"PHP","plus":{"month":{"amount":NaN}}}}'
        with self.assertRaisesRegex(ValueError, "invalid JSON constant"):
            normalize_config(invalid, self.target, PLANS, self.at)
        with self.assertRaisesRegex(ValueError, "country_mismatch"):
            normalize_config((FIXTURES / "country-mismatch.json").read_bytes(), self.target, PLANS, self.at)


if __name__ == "__main__":
    unittest.main()
