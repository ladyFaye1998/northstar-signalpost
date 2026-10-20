import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("northstar_runner", ROOT / "scripts" / "run_northstar.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

from norway_company_agent.evidence import evidence
from norway_company_agent.official import normalize_entity


def official(org, employees=8, status="available"):
    raw = {"organisasjonsnummer": org, "navn": "Live Example AS", "organisasjonsform": {"kode": "AS"},
           "hjemmeside": "https://current.example.no", "antallAnsatte": employees}
    registry = evidence("registry_live", status, "official_registry_live", "https://data.brreg.no/" + org,
                        value=normalize_entity(raw) if status == "available" else None,
                        content_sha256="b" * 64)
    registry["raw_value"] = raw if status == "available" else None
    records = {"registry_live": registry}
    for module in ("financials", "roles", "locations", "group"):
        records[module] = evidence(module, "not_found" if status == "available" else status,
                                   "official", "https://data.brreg.no/" + org + "/" + module)
    return records, []


def no_website(url, **kwargs):
    return evidence("website", "not_found", "company_site", url or "", note="No website in fixture"), {
        "requests": 0, "bytes": 0, "latencies_ms": []}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bulk = self.root / "bulk.csv"
        self.bulk.write_text("organisasjonsnummer,navn,organisasjonsform.kode,antallAnsatte,hjemmeside\n"
                             "111111111,Frozen Example AS,AS,4,https://old.example.no\n"
                             "222222222,Second Example AS,AS,2,https://second.example.no\n", encoding="utf-8")
        self.inputs = self.root / "organisations.txt"

    def tearDown(self):
        self.temporary.cleanup()

    def args(self, orgs, name="run", previous=None):
        self.inputs.write_text("\n".join(orgs) + "\n", encoding="utf-8")
        return SimpleNamespace(organisations=str(self.inputs), bulk=str(self.bulk), output_dir=str(self.root / name),
                               run_id=name, previous=str(previous) if previous else None, workers=2,
                               expected_count=len(orgs), website_timeout=1)

    def execute(self, args, fetcher):
        with patch.object(runner, "fetch_official_modules", side_effect=fetcher), \
             patch.object(runner, "fetch_website", side_effect=no_website), \
             contextlib.redirect_stdout(io.StringIO()):
            report = runner.run(args)
        rows = [json.loads(line) for line in (Path(args.output_dir) / "envelopes.jsonl").read_text(encoding="utf-8").splitlines()]
        return report, rows

    def test_mixed_success_failure_has_one_ordered_terminal_envelope_per_input(self):
        args = self.args(["222222222", "111111111"])

        def fetch(org, modules):
            if org == "222222222":
                time.sleep(0.02)  # Complete the first input last.
                return official(org)
            raise RuntimeError("fixture transport failure")

        report, rows = self.execute(args, fetch)
        self.assertEqual([row["organisation_number"] for row in rows], ["222222222", "111111111"])
        self.assertEqual(report["emitted_envelopes"], 2)
        self.assertEqual(rows[1]["run"]["terminal_status"], "completed_with_errors")
        self.assertTrue(rows[1]["errors"])
        self.assertTrue(report["validation"]["checks"]["zero_silent_drops"])

    def test_missing_snapshot_org_survives_and_live_identity_is_separately_sourced(self):
        args = self.args(["333333333"])
        report, rows = self.execute(args, lambda org, modules: official(org))
        self.assertEqual(rows[0]["profile"]["evidence"]["registry"]["status"], "not_found")
        self.assertEqual(rows[0]["profile"]["name"], "Live Example AS")
        self.assertEqual(report["registry"]["matched"], 0)
        self.assertEqual(next(c["value"] for c in rows[0]["claims"] if c["field"] == "registry.name"), "Live Example AS")

    def test_refresh_failure_keeps_supported_previous_value(self):
        first_args = self.args(["111111111"], "first")
        _, first = self.execute(first_args, lambda org, modules: official(org, employees=8))
        previous = Path(first_args.output_dir) / "envelopes.jsonl"
        args = self.args(["111111111"], "second", previous=previous)
        _, rows = self.execute(args, lambda org, modules: official(org, status="source_error"))
        claim = next(c for c in rows[0]["claims"] if c["field"] == "registry.employees")
        self.assertEqual(claim["value"], 8)
        self.assertEqual(claim["freshness"], "stale")
        self.assertEqual(rows[0]["changes"], [])
        self.assertEqual(json.loads(previous.read_text(encoding="utf-8")), first[0])

    def test_adapter_failure_cannot_drop_rows_or_crash_null_metric_aggregation(self):
        args = self.args(["111111111", "222222222"])
        with patch("norway_company_agent.northstar_contract.make_envelope", side_effect=ValueError("bad adapter")):
            report, rows = self.execute(args, lambda org, modules: official(org))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["run"]["terminal_status"] == "failed" for row in rows))
        self.assertTrue(all(row["errors"][0]["module"] == "envelope" for row in rows))
        self.assertIsNone(report["operations"]["requests"])
        self.assertEqual(report["operations"]["entities_with_missing_request_telemetry"], 2)

    def test_live_url_and_name_drive_site_check_without_overwriting_frozen_identity(self):
        args = self.args(["111111111"])
        with patch.object(runner, "fetch_official_modules", side_effect=lambda org, modules: official(org)), \
             patch.object(runner, "fetch_website", side_effect=no_website) as fetch_site, \
             patch.object(runner, "apply_website_identity_gate", side_effect=lambda p, w: {"website": w}) as gate, \
             contextlib.redirect_stdout(io.StringIO()):
            runner.run(args)
        self.assertEqual(fetch_site.call_args.args[0], "https://current.example.no")
        self.assertEqual(gate.call_args.args[0]["name"], "Live Example AS")
        row = json.loads((Path(args.output_dir) / "envelopes.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(row["profile"]["name"], "Frozen Example AS")

    def test_completed_run_is_not_overwritten(self):
        args = self.args(["111111111"])
        self.execute(args, lambda org, modules: official(org))
        with self.assertRaisesRegex(ValueError, "completed run"):
            self.execute(args, lambda org, modules: official(org))


if __name__ == "__main__":
    unittest.main()
