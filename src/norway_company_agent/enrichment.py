"""Conservative company-owned website claims; no additional network requests.

The website crawler retains small DOM/JSON-LD projections and raw response hashes.
This module only publishes those projections after the exact-entity identity gate.
Career links mean a career page exists, never that a vacancy is currently open.
Company news is company-reported activity, never independent coverage or sentiment.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from typing import Any, Iterator
from urllib.parse import unquote, urljoin, urlparse, urlunparse

from .evidence import evidence
from .identity import assess_website_identity, assess_social_identity
from .website import _registered_domain, normalize_social_url

CAREERS = re.compile(r"(?:karriere|career|ledig(?:e)?[-_ ]?stilling|vacanc|\bjobs\b|jobbe[-_ ]?hos|work[-_ ]?with[-_ ]?us)", re.I)
NEWS = re.compile(r"/(?:news|nyheter|aktuelt|press|pressemelding|blogg?|artikler)(?:/|$)", re.I)
EMAIL = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.I)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _stable(*parts: Any) -> str:
    return hashlib.sha256("|".join(_canonical(part) for part in parts).encode()).hexdigest()[:24]


def _name(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"\w+", normalized))


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _types(node: dict[str, Any]) -> set[str]:
    kinds = node.get("@type") or []
    if isinstance(kinds, str):
        kinds = [kinds]
    return {str(kind).rsplit("/", 1)[-1] for kind in kinds}


def _date(value: Any) -> str | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:T|\s|$)", str(value or "").strip())
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None


def _owned(url: str, homepage: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password and parsed.port in {None, 80, 443} and _registered_domain(url) == _registered_domain(homepage)
    except ValueError:
        return False


def _url(value: str) -> str:
    parsed = urlparse(value)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def _node_is_entity(node: Any, profile: dict[str, Any]) -> bool:
    if not isinstance(node, dict):
        return False
    target = str(profile.get("organisation_number") or "")
    identifiers = [node.get(key) for key in ("taxID", "vatID", "identifier", "iso6523Code")]
    explicit = set()
    for identifier in identifiers:
        for number in re.findall(r"(?<!\d)\d{3}[ .-]?\d{3}[ .-]?\d{3}(?!\d)", str(identifier or "")):
            explicit.add(re.sub(r"\D", "", number))
    if explicit:
        return explicit == {target}
    legal_name = _name(profile.get("name"))
    return bool(legal_name and any(_name(node.get(key)) == legal_name for key in ("legalName", "name")))


def build_website_claims(profile: dict[str, Any]) -> dict[str, Any]:
    """Return contract-ready claims/evidence from the bounded website capture.

    Every published field has a deterministic claim id and evidence reference.
    IDs exclude retrieval time, so unchanged refreshes do not manufacture changes.
    """
    record = (profile.get("evidence") or {}).get("website") or {}
    value = record.get("value") or {}
    assessment = assess_website_identity(profile) if record.get("status") == "available" else {"publishable": False, "status": "unavailable"}
    result: dict[str, Any] = {"claims": [], "evidence": [], "identity_assessment": assessment, "coverage": {}}
    if not assessment.get("publishable"):
        result["coverage"] = {"state": "ambiguous" if record.get("status") == "available" else record.get("status", "not_found"), "reason": "Exact company identity is not established; no website claims published."}
        return result
    homepage = str(value.get("final_url") or record.get("source_url") or "")
    org = str(profile["organisation_number"])
    claims: dict[str, dict[str, Any]] = {}
    references: dict[str, dict[str, Any]] = {}
    pages = value.get("pages") or []
    observed = _date(record.get("retrieved_at")) or date.today().isoformat()
    verified_socials = {
        (item.get("platform"), item.get("url")) for item in value.get("social_links") or []
        if assess_social_identity(profile, item).get("publishable")
    }

    def add(page: dict[str, Any], category: str, field: str, label: str, claim_value: Any, span: str, *, published_at: str | None = None, span_kind: str = "normalized_dom_text", confidence: float = 0.95) -> None:
        source = str(page.get("url") or "")
        digest = str(page.get("content_sha256") or "")
        retrieved = page.get("retrieved_at") or record.get("retrieved_at")
        if not source or not _owned(source, homepage) or not re.fullmatch(r"[a-f0-9]{64}", digest) or not retrieved or not span.strip():
            return
        claim_id = "website-claim-" + _stable(org, field, claim_value)
        evidence_id = "website-evidence-" + _stable(source, digest, span[:1600], span_kind)
        references[evidence_id] = {
            "id": evidence_id, "source_url": source, "source_class": "company_site",
            "source_module": "website", "retrieved_at": retrieved, "content_sha256": digest,
            "claim_span": span[:1600], "span_kind": span_kind,
            "published_at": published_at, "exact_entity": True,
            "identity_method": assessment.get("method"),
        }
        if claim_id in claims:
            if evidence_id not in claims[claim_id]["evidence_ids"]:
                claims[claim_id]["evidence_ids"].append(evidence_id)
            return
        claims[claim_id] = {
            "id": claim_id, "category": category, "field": field, "label": label,
            "value": claim_value, "availability": "available", "confidence": confidence,
            "evidence_ids": [evidence_id], "source_module": "website",
            "published_at": published_at, "claim_type": "company_reported",
        }

    for page in pages[:5]:
        if not _owned(str(page.get("url") or ""), homepage):
            continue
        links = page.get("links") or []
        for link in links:
            link_url = str(link.get("url") or "")
            label = str(link.get("text") or "").strip()
            context = str(link.get("context") or label)
            try:
                parsed = urlparse(link_url)
            except ValueError:
                continue
            social = normalize_social_url(link_url)
            if social and (social["platform"], social["url"]) in verified_socials:
                add(page, "profiles", "company_profiles", f"Company-linked {social['platform']} profile", social, f"{label} [{link_url}]", span_kind="dom_link")
            if parsed.scheme == "mailto":
                address = unquote(parsed.path).strip()
                # A company-owned site can list a supplier, recruiter or designer.
                # Only mailboxes under the verified company's own domain qualify.
                if EMAIL.fullmatch(address) and _registered_domain("https://" + address.rsplit("@", 1)[-1]) == _registered_domain(homepage):
                    add(page, "contact", "contact.email", "Company-published email", address.lower(), f"{label} [{link_url}]", span_kind="dom_link")
            elif parsed.scheme == "tel":
                telephone = unquote(parsed.path).strip()
                digits = re.sub(r"\D", "", telephone)
                # Restrict phone facts to contact surfaces, not staff/customer articles.
                if 8 <= len(digits) <= 15 and (re.search(r"kontakt|contact", urlparse(str(page.get("url"))).path, re.I) or telephone in str(page.get("identity_text_excerpt") or "")):
                    add(page, "contact", "contact.telephone", "Company-published telephone", telephone, f"{label} [{link_url}]", span_kind="dom_link")
            elif _owned(link_url, homepage) and CAREERS.search(parsed.path + " " + label):
                add(page, "careers", "careers.page", "Company careers page", {"url": _url(link_url), "title": "Company careers page"}, f"{label} [{link_url}]", span_kind="dom_link")
            # A dated link is published as a link, not as an independently verified
            # event, and never as a current job vacancy.
            if _owned(link_url, homepage) and NEWS.search(parsed.path) and label and len(label) >= 12:
                dates = re.findall(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", context)
                published = _date(dates[0]) if len(set(dates)) == 1 else None
                if published and published <= observed:
                    add(page, "activity", "activity.dated_link", "Company-published dated activity link", {"title": label, "url": _url(link_url), "published_at": published}, context, published_at=published)

        for link in page.get("dated_links") or []:
            target = str(link.get("url") or "")
            published = _date(link.get("datetime"))
            title = str(link.get("title") or "").strip()
            if not _owned(target, homepage) or not (NEWS.search(urlparse(target).path) or NEWS.search(urlparse(str(page.get("url"))).path)):
                continue
            if not title or not published or published > observed:
                continue
            span = f"{link.get('context') or title} [time datetime={link.get('datetime')}] [{target}]"
            add(page, "activity", "activity.dated_link", "Company-published dated activity link", {"title": title, "url": _url(target), "published_at": published}, span, published_at=published, span_kind="dom_article_card")

        for node in _walk(page.get("structured_data") or []):
            kinds = _types(node)
            if kinds & {"Organization", "Corporation", "LocalBusiness", "Store", "Restaurant"} and _node_is_entity(node, profile):
                telephone = str(node.get("telephone") or "").strip()
                if 8 <= len(re.sub(r"\D", "", telephone)) <= 15:
                    add(page, "contact", "contact.telephone", "Company-published telephone", telephone, _canonical({"name": node.get("legalName") or node.get("name"), "telephone": telephone}), span_kind="json_ld_projection")
                addresses = node.get("address") or []
                if isinstance(addresses, dict):
                    addresses = [addresses]
                for address in addresses if isinstance(addresses, list) else []:
                    if not isinstance(address, dict) or not address.get("streetAddress") or not address.get("postalCode"):
                        continue
                    projected = {key: address[key] for key in ("streetAddress", "postalCode", "addressLocality", "addressRegion", "addressCountry") if address.get(key)}
                    add(page, "location", "locations.company_reported", "Company-published address", projected, _canonical({"name": node.get("legalName") or node.get("name"), "address": address}), span_kind="json_ld_projection")
            if "JobPosting" in kinds:
                employer = node.get("hiringOrganization")
                posted, expires = _date(node.get("datePosted")), _date(node.get("validThrough"))
                title = str(node.get("title") or "").strip()
                # No expiry date means current availability is unknown; abstain.
                if not _node_is_entity(employer, profile) or not title or not posted or not expires or not posted <= observed <= expires:
                    continue
                source_url = urljoin(str(page.get("url")), str(node.get("url") or page.get("url")))
                if not _owned(source_url, homepage):
                    continue
                job = {"title": title, "url": _url(source_url), "date_posted": posted, "valid_through": expires}
                if node.get("employmentType"):
                    job["employment_type"] = node["employmentType"]
                add(page, "jobs", "jobs.opening", "Dated company job posting", job, _canonical({key: node[key] for key in ("title", "url", "datePosted", "validThrough", "hiringOrganization", "employmentType") if key in node}), published_at=posted, span_kind="json_ld_projection")
            if kinds & {"NewsArticle", "BlogPosting", "Article"}:
                publisher = node.get("publisher")
                if publisher and not _node_is_entity(publisher, profile):
                    continue
                title, published = str(node.get("headline") or node.get("name") or "").strip(), _date(node.get("datePublished"))
                source_url = urljoin(str(page.get("url")), str(node.get("url") or page.get("url")))
                if not title or not published or published > observed or not _owned(source_url, homepage):
                    continue
                add(page, "activity", "activity.article", "Company-published article", {"title": title, "url": _url(source_url), "published_at": published}, _canonical({key: node[key] for key in ("headline", "name", "url", "datePublished", "publisher") if key in node}), published_at=published, span_kind="json_ld_projection")

    # Bound output size without silently converting absence into zero counts.
    result["claims"] = sorted(claims.values(), key=lambda item: (item["field"], item["id"]))[:60]
    used = {ref for claim in result["claims"] for ref in claim["evidence_ids"]}
    result["evidence"] = sorted((item for key, item in references.items() if key in used), key=lambda item: item["id"])
    result["coverage"] = {
        "state": "available" if result["claims"] else "not_available", "pages_inspected": len(pages[:5]),
        "categories_found": sorted({item["category"] for item in result["claims"]}),
        "scope": "Bounded permitted company pages; missing facts do not prove absence. Social platforms were not fetched.",
    }
    return result


def enrich_website(profile: dict[str, Any], website_record: dict[str, Any]) -> dict[str, Any]:
    """Evidence-wrapper convenience API; build_website_claims is the contract API."""
    temporary = {**profile, "evidence": {**(profile.get("evidence") or {}), "website": website_record}}
    extracted = build_website_claims(temporary)
    status = "available" if extracted["claims"] else "blocked" if not extracted["identity_assessment"].get("publishable") else "not_found"
    return evidence("website_enrichment", status, "company_site", website_record.get("source_url") or "", value=extracted, note=extracted["coverage"].get("reason") or extracted["coverage"].get("scope"), retrieved_at=website_record.get("retrieved_at"), content_sha256=website_record.get("content_sha256"))
