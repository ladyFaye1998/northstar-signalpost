from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.northstar_contract import make_envelope
from norway_company_agent.official import normalize_entity, normalize_financials


START = "2026-10-20T10:00:00Z"
END = "2026-10-20T10:00:02Z"


def official_record(raw, module="registry_live", status="available"):
    return {
        "field": module, "status": status,
        "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/123456789",
        "source_class": "official_registry_live", "retrieved_at": END,
        "content_sha256": "a" * 64, "raw_value": raw,
        "value": normalize_entity(raw) if module == "registry_live" else normalize_financials(raw),
    }


def profile(employees=0):
    raw = {"organisasjonsnummer": "123456789", "navn": "Example AS",
           "organisasjonsform": {"kode": "AS"}, "antallAnsatte": employees,
           "konkurs": False, "underAvvikling": False, "hjemmeside": "example.no"}
    return {"organisation_number": "123456789", "evidence": {"registry_live": official_record(raw)},
            "run_metrics": {"requests": 1, "runtime_ms": 2000, "third_party_cost_usd": 0}}


def claims(envelope):
    return {claim["field"]: claim for claim in envelope["claims"]}


class NorthstarContractTests(unittest.TestCase):
    def website_profile(self):
        current = profile()
        current["evidence"]["website"] = {
            "status": "available", "source_url": "https://example.no/", "source_class": "company_site",
            "retrieved_at": END, "content_sha256": "c" * 64,
            "value": {"final_url": "https://example.no/", "main_text_excerpt": "Example AS 123 456 789",
                      "identity_assessment": {"publishable": True}, "crawl_errors": []}}
        return current

    def website_bundle(self, entries=(), publishable=True):
        return {"identity_assessment": {"publishable": publishable}, "coverage": {},
                "claims": [{"field": field, "value": value, "availability": "available", "confidence": .95,
                            "source_module": "website", "evidence_ids": [f"site-{index}"], "category": field.split(".")[0]}
                           for index, (field, value) in enumerate(entries)],
                "evidence": [{"id": f"site-{index}", "source_url": "https://example.no/", "source_class": "company_site",
                             "retrieved_at": END, "content_sha256": "c" * 64, "claim_span": str(value)}
                             for index, (_, value) in enumerate(entries)]}

    def test_audited_raw_path_retains_false_and_zero(self):
        result = make_envelope(profile(), "run-1", START, END)
        by_field = claims(result)
        self.assertEqual(by_field["registry.employees"]["value"], 0)
        self.assertEqual(by_field["registry.employees"]["availability"], "available")
        self.assertIs(by_field["registry.bankrupt"]["value"], False)
        ev = {item["id"]: item for item in result["evidence"]}[by_field["registry.employees"]["evidence_ids"][0]]
        self.assertEqual(ev["source_path"], "/antallAnsatte")
        self.assertEqual(ev["source_value"], 0)
        self.assertEqual(ev["path_basis"], "raw_response")
        self.assertEqual(result["operations"]["requests"], 1)

    def test_failing_refresh_preserves_original_evidence_and_no_material_change(self):
        first = make_envelope(profile(8), "run-1", START, END)
        failed = profile()
        failed["evidence"]["registry_live"].update(status="source_error", value=None, raw_value=None,
                                                    content_sha256=None, note="HTTP 503")
        refreshed = make_envelope(failed, "run-2", START, "2026-10-21T10:00:02Z", previous=first)
        retained = claims(refreshed)["registry.employees"]
        self.assertEqual(retained["value"], 8)
        self.assertEqual(retained["freshness"], "stale")
        self.assertEqual(retained["availability"], "failed")
        self.assertEqual(retained["evidence_ids"], claims(first)["registry.employees"]["evidence_ids"])
        originals = {ev["id"]: ev for ev in first["evidence"]}
        new = {ev["id"]: ev for ev in refreshed["evidence"]}
        self.assertEqual(new[retained["evidence_ids"][0]], originals[retained["evidence_ids"][0]])
        self.assertEqual(refreshed["changes"], [])
        self.assertTrue(refreshed["errors"])

    def test_retrieval_timestamp_and_hash_alone_do_not_report_business_change(self):
        first_profile = profile(8)
        first = make_envelope(first_profile, "run-1", START, END)
        newer = deepcopy(first_profile)
        newer["evidence"]["registry_live"].update(retrieved_at="2026-10-21T10:00:02Z", content_sha256="b" * 64)
        result = make_envelope(newer, "run-2", START, "2026-10-21T10:00:02Z", previous=first)
        self.assertEqual(result["changes"], [])

    def test_change_has_resolvable_old_and_new_evidence(self):
        first = make_envelope(profile(8), "run-1", START, END)
        second_profile = profile(9)
        second_profile["evidence"]["registry_live"]["content_sha256"] = "b" * 64
        second = make_envelope(second_profile, "run-2", START, END, previous=first)
        self.assertEqual(len(second["changes"]), 1)
        change = second["changes"][0]
        self.assertEqual((change["field"], change["old_value"], change["new_value"]), ("registry.employees", 8, 9))
        ids = {ev["id"] for ev in second["evidence"]}
        self.assertTrue(set(change["old_evidence_ids"] + change["new_evidence_ids"]) <= ids)
        repeated = make_envelope(second_profile, "run-3", START, END, previous=second)
        self.assertEqual(repeated["changes"], [])

    def test_checked_empty_roles_is_available_empty_not_unchecked(self):
        current = profile()
        current["evidence"]["roles"] = {**official_record({"rollegrupper": []}), "field": "roles",
                                          "value": {"roles": []}}
        result = make_envelope(current, "run-1", START, END)
        self.assertEqual(claims(result)["roles.roles"]["value"], [])
        self.assertEqual(claims(result)["roles.roles"]["availability"], "available")
        self.assertFalse(any(key.startswith("jobs") for key in claims(result)))

    def test_financial_path_points_to_exact_raw_metric(self):
        current = profile()
        raw = [{"id": 14, "regnskapsperiode": {"tilDato": "2025-12-31"},
                "valuta": "NOK", "resultatregnskapResultat": {
                    "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 1200000}}}}]
        current["evidence"]["financials"] = official_record(raw, "financials")
        result = make_envelope(current, "run-1", START, END)
        claim = claims(result)["financials.2025-12-31.revenue"]
        ev = {item["id"]: item for item in result["evidence"]}[claim["evidence_ids"][0]]
        self.assertEqual(claim["value"], 1200000)
        self.assertEqual(ev["source_path"], "/0/resultatregnskapResultat/driftsresultat/driftsinntekter/sumDriftsinntekter")

    def test_failed_live_does_not_replace_prior_supported_live_with_bulk(self):
        previous = make_envelope(profile(8), "run-1", START, END)
        current = profile(0)
        current["evidence"]["registry_live"].update(status="blocked", note="Request blocked")
        current["evidence"]["registry"] = official_record({"antallAnsatte": 0})
        refreshed = make_envelope(current, "run-2", START, END, previous=previous)
        self.assertEqual(claims(refreshed)["registry.employees"]["value"], 8)
        self.assertEqual(claims(refreshed)["registry.employees"]["freshness"], "stale")

    def test_verified_disappearance_is_change_and_failure_is_not(self):
        previous = make_envelope(profile(8), "run-1", START, END)
        current = profile(8)
        current["evidence"]["registry_live"].update(status="not_found", raw_value=None, value=None)
        refreshed = make_envelope(current, "run-2", START, END, previous=previous)
        employee = claims(refreshed)["registry.employees"]
        self.assertIsNone(employee["value"])
        self.assertEqual(employee["availability"], "not_available")
        self.assertTrue(any(change["field"] == "registry.employees" and change["kind"] == "removed"
                            for change in refreshed["changes"]))

    def test_does_not_mutate_input_and_rejects_identity_switch(self):
        current = profile()
        original = deepcopy(current)
        first = make_envelope(current, "run-1", START, END)
        self.assertEqual(current, original)
        original_first = deepcopy(first)
        make_envelope(current, "run-2", START, END, previous=first)
        self.assertEqual(first, original_first)
        with self.assertRaises(ValueError):
            make_envelope(current, "run-2", START, END, previous={"organisation_number": "999999999"})

    def test_multiple_website_observations_survive_same_field_and_missing_jobs_are_not_zero(self):
        current = self.website_profile()
        bundle = self.website_bundle([("contact.email", "a@example.no"), ("contact.email", "b@example.no"),
                                      ("careers.page", {"url": "https://example.no/careers", "title": "Careers"})])
        with patch("norway_company_agent.enrichment.build_website_claims", return_value=bundle):
            result = make_envelope(current, "run-1", START, END)
        by_field = claims(result)
        self.assertEqual(by_field["contact.email"]["value"], ["a@example.no", "b@example.no"])
        self.assertEqual(set(by_field["contact.email"]["evidence_ids"]), {"site-0", "site-1"})
        self.assertEqual(by_field["jobs.opening"]["availability"], "not_available")
        self.assertIsNone(by_field["jobs.opening"]["value"])
        brief = " ".join(fact["text"] for fact in result["synthesis"]["facts"])
        self.assertIn("does not establish active vacancies", brief)
        self.assertNotIn("0 job", brief)

    def test_partial_crawl_keeps_missing_observations_stale_but_current_site_current(self):
        current = self.website_profile()
        with patch("norway_company_agent.enrichment.build_website_claims", return_value=self.website_bundle([
            ("careers.page", {"url": "https://example.no/careers", "title": "Careers"})])):
            previous = make_envelope(current, "run-1", START, END)
        current["evidence"]["website"]["value"]["crawl_errors"] = [{"url": "https://example.no/careers", "error": "timeout"}]
        with patch("norway_company_agent.enrichment.build_website_claims", return_value=self.website_bundle()):
            result = make_envelope(current, "run-2", START, END, previous=previous)
        self.assertEqual(claims(result)["careers.page"]["freshness"], "stale")
        self.assertEqual(claims(result)["official_website"]["freshness"], "current")
        self.assertEqual(result["changes"], [])
        self.assertTrue(any(error["code"] == "partial_crawl" for error in result["errors"]))

    def test_ambiguous_identity_retains_previous_site_without_publishing_new_company(self):
        current = self.website_profile()
        with patch("norway_company_agent.enrichment.build_website_claims", return_value=self.website_bundle()):
            previous = make_envelope(current, "run-1", START, END)
        current["evidence"]["website"]["value"]["final_url"] = "https://unrelated.no/"
        with patch("norway_company_agent.enrichment.build_website_claims", return_value=self.website_bundle(publishable=False)):
            result = make_envelope(current, "run-2", START, END, previous=previous)
        website = claims(result)["official_website"]
        self.assertEqual(website["value"], "https://example.no/")
        self.assertEqual(website["availability"], "ambiguous")
        self.assertEqual(website["freshness"], "stale")
        self.assertEqual(result["changes"], [])

    def test_partial_crawl_does_not_remove_one_member_of_supported_contact_array(self):
        current = self.website_profile()
        with patch("norway_company_agent.enrichment.build_website_claims", return_value=self.website_bundle([
            ("contact.email", "a@example.no"), ("contact.email", "b@example.no")])):
            previous = make_envelope(current, "run-1", START, END)
        current["evidence"]["website"]["value"]["crawl_errors"] = [{"url": "https://example.no/contact", "error": "timeout"}]
        with patch("norway_company_agent.enrichment.build_website_claims", return_value=self.website_bundle([
            ("contact.email", "a@example.no")])):
            result = make_envelope(current, "run-2", START, END, previous=previous)
        contact = claims(result)["contact.email"]
        self.assertEqual(contact["value"], ["a@example.no", "b@example.no"])
        self.assertEqual(contact["freshness"], "stale")
        self.assertEqual(contact["current_check"]["observed_value"], ["a@example.no"])
        self.assertEqual(result["changes"], [])
        self.assertTrue(set(contact["evidence_ids"] + contact["current_check"]["evidence_ids"]) <= {ev["id"] for ev in result["evidence"]})

    def test_unvisited_old_source_retains_observation_until_successfully_rechecked(self):
        current = self.website_profile()
        first_bundle = self.website_bundle([("contact.email", "a@example.no"), ("contact.email", "b@example.no")])
        first_bundle["evidence"][1]["source_url"] = "https://example.no/contact"
        with patch("norway_company_agent.enrichment.build_website_claims", return_value=first_bundle):
            previous = make_envelope(current, "run-1", START, END)
        partial_bundle = self.website_bundle([("contact.email", "a@example.no")])
        with patch("norway_company_agent.enrichment.build_website_claims", return_value=partial_bundle):
            result = make_envelope(current, "run-2", START, END, previous=previous)
        self.assertEqual(claims(result)["contact.email"]["value"], ["a@example.no", "b@example.no"])
        self.assertEqual(claims(result)["contact.email"]["freshness"], "stale")
        self.assertTrue(any(error["code"] == "source_not_revisited" for error in result["errors"]))
        self.assertEqual(result["changes"], [])
        # Once that exact supporting page was actually read successfully, a
        # disappearance within the checked source is a justified observation change.
        current["evidence"]["website"]["value"]["pages"] = [{"url": "https://example.no/contact"}]
        with patch("norway_company_agent.enrichment.build_website_claims", return_value=partial_bundle):
            rechecked = make_envelope(current, "run-3", START, END, previous=result)
        self.assertEqual(claims(rechecked)["contact.email"]["value"], ["a@example.no"])
        self.assertEqual(claims(rechecked)["contact.email"]["freshness"], "current")
        self.assertTrue(any(change["field"] == "contact.email" for change in rechecked["changes"]))

    def test_wholly_missing_claim_with_unvisited_old_page_is_not_removed(self):
        current = self.website_profile()
        bundle = self.website_bundle([("careers.page", {"url": "https://example.no/careers", "title": "Careers"})])
        bundle["evidence"][0]["source_url"] = "https://example.no/about"
        with patch("norway_company_agent.enrichment.build_website_claims", return_value=bundle):
            previous = make_envelope(current, "run-1", START, END)
        with patch("norway_company_agent.enrichment.build_website_claims", return_value=self.website_bundle()):
            result = make_envelope(current, "run-2", START, END, previous=previous)
        self.assertEqual(claims(result)["careers.page"]["freshness"], "stale")
        self.assertEqual(claims(result)["careers.page"]["value"], claims(previous)["careers.page"]["value"])
        self.assertEqual(result["changes"], [])

    def test_synthesis_financials_have_period_currency_and_resolvable_support(self):
        current = profile()
        raw = [{"id": 14, "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
                "valuta": "NOK", "resultatregnskapResultat": {"aarsresultat": -300,
                    "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 1200000}}}}]
        current["evidence"]["financials"] = official_record(raw, "financials")
        result = make_envelope(current, "run-1", START, END)
        facts = result["synthesis"]["facts"]
        financial = [fact for fact in facts if "Reported" in fact["text"]]
        self.assertEqual(len(financial), 2)
        self.assertTrue(all("NOK" in fact["text"] and "2025-12-31" in fact["text"] for fact in financial))
        by_field = claims(result)
        for fact in facts:
            self.assertTrue(set(fact["evidence_ids"]) <= {ref for field in fact["claim_fields"] for ref in by_field[field]["evidence_ids"]})
        self.assertNotIn("growth", " ".join(fact["text"] for fact in facts).lower())
        self.assertTrue(all("{" not in fact["text"] for fact in financial))

    def test_brief_formats_industry_address_as_readable_sourced_text(self):
        current = profile()
        current["evidence"]["registry_live"]["raw_value"].update(
            naeringskode1={"kode": "62.100", "beskrivelse": "Computer programming"},
            forretningsadresse={"adresse": ["Examplegata 2"], "postnummer": "0001", "poststed": "OSLO", "land": "Norge"})
        current["evidence"]["registry_live"]["value"] = normalize_entity(current["evidence"]["registry_live"]["raw_value"])
        result = make_envelope(current, "run-1", START, END)
        brief = " ".join(fact["text"] for fact in result["synthesis"]["facts"])
        self.assertIn("Computer programming (62.100)", brief)
        self.assertIn("Examplegata 2, 0001 OSLO, Norge", brief)
        self.assertNotIn("{", brief)


if __name__ == "__main__":
    unittest.main()
