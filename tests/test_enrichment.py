from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bs4 import BeautifulSoup
from norway_company_agent.enrichment import build_website_claims
from norway_company_agent.identity import assess_website_identity
from norway_company_agent.website import (
    _page_record, _priority_links, _robots_allowed, _ROBOTS_CACHE,
    WebsiteRedirectHandler, assert_public_url,
)


class EnrichmentTests(unittest.TestCase):
    def profile(self):
        structured = [
            {"@type": "Organization", "name": "Exact Company AS", "taxID": "NO 923609016 MVA", "address": {"streetAddress": "Examplegata 3", "postalCode": "0123", "addressLocality": "Oslo"}},
            {"@type": "JobPosting", "title": "Engineer", "datePosted": "2026-10-01", "validThrough": "2026-11-01", "url": "/jobs/engineer", "hiringOrganization": {"@type": "Organization", "name": "Exact Company AS"}},
            {"@type": "NewsArticle", "headline": "Company opens a new workshop", "datePublished": "2026-10-02", "url": "/news/workshop", "publisher": {"@type": "Organization", "name": "Exact Company AS"}},
        ]
        html = f'''<html><head><title>Exact Company AS</title><script type="application/ld+json">{json.dumps(structured)}</script></head>
        <body><main>We provide industrial services from Oslo with a local engineering team.</main>
        <a href="/careers">Careers at Exact Company</a><a href="mailto:hello@exact.no">Contact email</a>
        <a href="mailto:designer@vendor.no">Web designer</a><footer>Exact Company AS — Org.nr. 923 609 016. Examplegata 3, 0123 Oslo.</footer></body></html>'''.encode()
        page, _ = _page_record(html, "https://exact.no/")
        page["retrieved_at"] = "2026-10-20T12:00:00Z"
        return {"organisation_number": "923609016", "name": "Exact Company AS", "evidence": {"website": {
            "status": "available", "source_url": "https://exact.no/", "retrieved_at": page["retrieved_at"], "content_sha256": page["content_sha256"],
            "value": {"final_url": "https://exact.no/", "title": page["title"], "identity_text_excerpt": page["identity_text_excerpt"], "pages": [page]},
        }}}

    def test_claims_are_evidenced_and_not_vendor_contacts(self):
        result = build_website_claims(self.profile())
        fields = {claim["field"] for claim in result["claims"]}
        self.assertEqual(fields, {"contact.email", "careers.page", "jobs.opening", "activity.article", "locations.company_reported"})
        self.assertNotIn("vendor.no", json.dumps(result["claims"]))
        references = {item["id"]: item for item in result["evidence"]}
        for claim in result["claims"]:
            self.assertEqual(claim["availability"], "available")
            for key in claim["evidence_ids"]:
                ref = references[key]
                self.assertTrue(ref["claim_span"])
                self.assertEqual(len(ref["content_sha256"]), 64)
                self.assertEqual(ref["source_url"], "https://exact.no/")

    def test_timestamps_do_not_change_claim_or_evidence_ids(self):
        profile = self.profile()
        first = build_website_claims(profile)
        profile["evidence"]["website"]["retrieved_at"] = "2026-10-21T12:00:00Z"
        profile["evidence"]["website"]["value"]["pages"][0]["retrieved_at"] = "2026-10-21T12:00:00Z"
        second = build_website_claims(profile)
        self.assertEqual(first["claims"], second["claims"])
        self.assertEqual([x["id"] for x in first["evidence"]], [x["id"] for x in second["evidence"]])

    def test_expired_undated_and_other_employer_jobs_are_not_openings(self):
        for mutation in ({"validThrough": "2026-09-01"}, {"validThrough": None}, {"hiringOrganization": {"name": "Parent Company AS"}}):
            with self.subTest(mutation=mutation):
                profile = self.profile()
                profile["evidence"]["website"]["value"]["pages"][0]["structured_data"][0][1].update(mutation)
                result = build_website_claims(profile)
                self.assertNotIn("jobs.opening", {claim["field"] for claim in result["claims"]})
                self.assertIn("careers.page", {claim["field"] for claim in result["claims"]})

    def test_wrong_entity_and_group_homepages_are_quarantined(self):
        for footer in ("Other AS 988412406", "Parent AS 988412406; Exact Company AS 923609016"):
            profile = self.profile()
            profile["evidence"]["website"]["value"]["identity_text_excerpt"] = footer
            self.assertEqual(build_website_claims(profile)["claims"], [])

    def test_name_alone_and_concatenated_numbers_are_not_identity(self):
        profile = self.profile()
        value = profile["evidence"]["website"]["value"]
        for text in ("Exact Company AS", "Number 923 — price 609 — code 016", "Account 19236090160"):
            value["identity_text_excerpt"] = text
            self.assertFalse(assess_website_identity(profile)["publishable"])

    def test_registry_street_and_postcode_corroborate_name(self):
        profile = self.profile()
        profile["business_address"] = {"adresse": ["Examplegata 3"], "postnummer": "0123"}
        profile["evidence"]["website"]["value"]["identity_text_excerpt"] = "Exact Company AS, Examplegata 3, 0123 Oslo"
        self.assertTrue(assess_website_identity(profile)["publishable"])
        profile["business_address"]["postnummer"] = "9999"
        self.assertFalse(assess_website_identity(profile)["publishable"])

    def test_hash_and_date_are_required_for_publication(self):
        profile = self.profile()
        profile["evidence"]["website"]["value"]["pages"][0]["content_sha256"] = ""
        self.assertEqual(build_website_claims(profile)["claims"], [])

    def test_future_news_is_not_published(self):
        profile = self.profile()
        profile["evidence"]["website"]["value"]["pages"][0]["structured_data"][0][2]["datePublished"] = "2099-01-01"
        self.assertNotIn("activity.article", {claim["field"] for claim in build_website_claims(profile)["claims"]})

    def test_social_profile_requires_identity_gated_link_and_observed_dom(self):
        profile = self.profile()
        value = profile["evidence"]["website"]["value"]
        value["social_links"] = [
            {"platform": "linkedin", "url": "https://linkedin.com/company/exact-company"},
            {"platform": "facebook", "url": "https://facebook.com/other-company"},
            {"platform": "instagram", "url": "https://instagram.com/exact-company"},
        ]
        value["pages"][0]["links"].extend([
            {"url": "https://www.linkedin.com/company/exact-company/", "text": "LinkedIn"},
            {"url": "https://facebook.com/other-company", "text": "Parent Facebook"},
        ])
        claims = [item for item in build_website_claims(profile)["claims"] if item["field"] == "company_profiles"]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["value"], {"platform": "linkedin", "url": "https://linkedin.com/company/exact-company"})

    def test_careers_navigation_labels_do_not_duplicate_the_page(self):
        profile = self.profile()
        profile["evidence"]["website"]["value"]["pages"][0]["links"].append({"url": "https://exact.no/careers", "text": "View jobs"})
        claims = [item for item in build_website_claims(profile)["claims"] if item["field"] == "careers.page"]
        self.assertEqual(len(claims), 1)
        self.assertEqual(len(claims[0]["evidence_ids"]), 2)

    def test_dated_article_cards_require_unique_local_title_and_date(self):
        html = b'''<html><body><time datetime="2026-10-20">Global updated date</time>
        <article><h3><a href="/news/good">A new service for customers</a></h3><time datetime="2026-10-01T12:00:00Z">1. oktober 2026</time></article>
        <article><h3><a href="/news/ambiguous">Ambiguous dates</a></h3><time datetime="2026-10-01">One</time><time datetime="2026-10-02">Two</time></article>
        <article><h3><a href="/news/a">First headline</a></h3><h3><a href="/news/b">Second headline</a></h3><time datetime="2026-10-01">One</time></article>
        <article><h3><a href="/news/updated">Modified date only</a></h3><time itemprop="dateModified" datetime="2026-10-01">One</time></article>
        <h3><a href="/news/unassociated">No local date</a></h3></body></html>'''
        page, _ = _page_record(html, "https://exact.no/blogg/")
        page["retrieved_at"] = "2026-10-20T12:00:00Z"
        profile = self.profile()
        profile["evidence"]["website"]["value"]["pages"].append(page)
        claims = [item for item in build_website_claims(profile)["claims"] if item["field"] == "activity.dated_link"]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["value"]["url"], "https://exact.no/news/good")
        self.assertEqual(claims[0]["value"]["published_at"], "2026-10-01")


class CrawlPolicyTests(unittest.TestCase):
    def setUp(self):
        _ROBOTS_CACHE.clear()

    def response(self, body: bytes, content_type="text/plain"):
        class Response(io.BytesIO):
            headers = {"content-type": content_type}
        return Response(body)

    def test_robots_fail_closed_except_explicit_missing_file(self):
        for status, expected in ((404, True), (410, True), (403, False), (429, False), (503, False)):
            with self.subTest(status=status):
                _ROBOTS_CACHE.clear()
                error = urllib.error.HTTPError("https://exact.no/robots.txt", status, "test", {}, None)
                with patch("norway_company_agent.website.assert_public_url"), patch("norway_company_agent.website.SAFE_OPENER.open", side_effect=error):
                    self.assertEqual(_robots_allowed("https://exact.no/", 1), expected)
        _ROBOTS_CACHE.clear()
        with patch("norway_company_agent.website.assert_public_url"), patch("norway_company_agent.website.SAFE_OPENER.open", side_effect=TimeoutError()):
            self.assertFalse(_robots_allowed("https://exact.no/", 1))

    def test_cached_robots_still_evaluates_each_path(self):
        body = b"User-agent: *\nDisallow: /private\n"
        with patch("norway_company_agent.website.assert_public_url"), patch("norway_company_agent.website.SAFE_OPENER.open", return_value=self.response(body)) as opener:
            self.assertTrue(_robots_allowed("https://exact.no/", 1))
            self.assertFalse(_robots_allowed("https://exact.no/private/team", 1))
            self.assertTrue(_robots_allowed("https://exact.no/contact", 1))
            self.assertEqual(opener.call_count, 1)

    def test_html_soft_404_is_not_a_robots_permission(self):
        with patch("norway_company_agent.website.assert_public_url"), patch("norway_company_agent.website.SAFE_OPENER.open", return_value=self.response(b"<html>Home</html>", "text/html")):
            self.assertFalse(_robots_allowed("https://exact.no/", 1))

    def test_cross_domain_redirect_is_not_followed(self):
        handler = WebsiteRedirectHandler("exact.no", 1)
        with patch("norway_company_agent.website.assert_public_url"), self.assertRaises(ValueError):
            handler.redirect_request(None, None, 302, "Found", {}, "https://unrelated.no/")

    def test_url_credentials_and_nonstandard_ports_rejected(self):
        for url in ("https://user:password@exact.no/", "http://exact.no:8080/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                assert_public_url(url)

    def test_priority_budget_covers_information_types(self):
        soup = BeautifulSoup('<a href="/contact">Contact</a><a href="/contact/oslo">Contact Oslo</a><a href="/careers">Careers</a><a href="/news">News</a><a href="/about">About</a><a href="https://facebook.com/exact">Careers</a>', "html.parser")
        self.assertEqual(_priority_links("https://exact.no/", soup), ["https://exact.no/contact", "https://exact.no/careers", "https://exact.no/news", "https://exact.no/about"])


if __name__ == "__main__":
    unittest.main()
