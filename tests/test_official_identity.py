"""A successful HTTP response must not introduce another company's facts."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.http import FetchResult
from norway_company_agent.official import fetch_official_modules


class OfficialIdentityTests(unittest.TestCase):
    def fetch(self, body):
        return lambda url: FetchResult(url=url, status=200, elapsed_ms=1, bytes_received=100, body=body, content_sha256="a" * 64, retrieved_at="2026-10-20T00:00:00Z")

    def test_registry_conflicting_identifier_is_quarantined(self):
        records, metrics = fetch_official_modules("923609016", {"registry_live"}, self.fetch({"organisasjonsnummer": "988412406", "navn": "Different AS"}))
        self.assertEqual(records["registry_live"]["status"], "source_error")
        self.assertIsNone(records["registry_live"]["value"])
        self.assertEqual(records["registry_live"]["content_sha256"], "a" * 64)
        self.assertEqual(len(metrics), 1)

    def test_one_conflicting_financial_record_quarantines_whole_response(self):
        body = [{"virksomhet": {"organisasjonsnummer": number}, "id": i} for i, number in enumerate(("923609016", "988412406"))]
        records, _ = fetch_official_modules("923609016", {"financials"}, self.fetch(body))
        self.assertEqual(records["financials"]["status"], "source_error")
        self.assertIsNone(records["financials"]["value"])

    def test_exact_registry_identifier_preserves_source_and_name(self):
        body = {"organisasjonsnummer": "923609016", "navn": "Exact Company AS"}
        records, _ = fetch_official_modules("923609016", {"registry_live"}, self.fetch(body))
        self.assertEqual(records["registry_live"]["status"], "available")
        self.assertEqual(records["registry_live"]["value"]["organisation_number"], "923609016")
        self.assertEqual(records["registry_live"]["raw_value"], body)


if __name__ == "__main__":
    unittest.main()
