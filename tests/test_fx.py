import json
import tempfile
import unittest

from scripts.fx import apply_estimates, fetch_fx
from tests.test_collect import Headers, Response


class FxTests(unittest.TestCase):
    def test_fetch_and_decimal_estimates(self):
        payload = json.dumps([
            {"date": "2026-10-16", "base": "USD", "quote": "CNY", "rate": 7.1, "providers": [{"name": "ECB"}]},
            {"date": "2026-10-16", "base": "USD", "quote": "PHP", "rate": 58.0, "providers": [{"name": "BSP"}]},
        ]).encode()
        with tempfile.TemporaryDirectory() as temp:
            fx = fetch_fx(None, temp, opener=lambda *_a, **_k: Response(payload), now=lambda: "2026-10-19T00:00:00Z")
            self.assertFalse(fx["stale"])
            self.assertEqual(next(r for r in fx["rates"] if r["quote"] == "USD")["rate"], "1")
            quote = {"currency": "PHP", "display_price": {"amount": "1160"}}
            estimated = apply_estimates([quote], fx)[0]["estimates"]
            self.assertEqual(estimated["USD"]["amount"], "20")
            self.assertEqual(estimated["CNY"]["amount"], "142")

    def test_missing_currency_does_not_default_to_one(self):
        fx = {"fx_snapshot_id": "x", "rates": [{"quote": "USD", "rate": "1"}]}
        quote = {"currency": "ZZZ", "display_price": {"amount": "8"}}
        self.assertIsNone(apply_estimates([quote], fx)[0]["estimates"]["USD"])

    def test_failed_refresh_retains_entire_previous_snapshot(self):
        previous = {"schema_version": "1.0.0", "fx_snapshot_id": "old", "generated_at": "old", "fetched_at": "old", "base": "USD", "stale": False, "source": {}, "rates": [{"quote": "USD", "rate": "1", "rate_date": "2026-10-16", "providers": []}]}
        with tempfile.TemporaryDirectory() as temp:
            failed = fetch_fx(previous, temp, opener=lambda *_a, **_k: (_ for _ in ()).throw(OSError("offline")), now=lambda: "2026-10-19T00:00:00Z")
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["fx_snapshot_id"], "old")
        self.assertEqual(failed["rates"], previous["rates"])


if __name__ == "__main__":
    unittest.main()
