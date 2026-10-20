"""Auditable claim envelopes and conservative refreshes; no network or model calls.

Availability describes the *current check*. A retained supported value after a
failed check keeps its original evidence and is explicitly marked stale.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .batch import evidence_terminal_state


AVAILABILITY = {
    "available": "available", "not_found": "not_available",
    "not_available": "not_available", "not_applicable": "not_applicable",
    "blocked": "blocked", "source_error": "failed", "failed": "failed",
    "not_fetched": "failed", "ambiguous": "ambiguous",
}
RETAIN_ON = {"blocked", "failed", "ambiguous"}
REGISTRY_PATHS = {
    "organisation_number": "/organisasjonsnummer", "name": "/navn",
    "legal_form": "/organisasjonsform/kode", "employees": "/antallAnsatte",
    "bankrupt": "/konkurs", "liquidating": "/underAvvikling",
    "website": "/hjemmeside", "industry": "/naeringskode1",
    "business_address": "/forretningsadresse", "postal_address": "/postadresse",
    "latest_submitted_accounts": "/sisteInnsendteAarsregnskap",
}
FINANCIAL_PATHS = {
    "record_id": "/id", "account_type": "/regnskapstype", "period": "/regnskapsperiode",
    "currency": "/valuta",
    "revenue": "/resultatregnskapResultat/driftsresultat/driftsinntekter/sumDriftsinntekter",
    "operating_result": "/resultatregnskapResultat/driftsresultat/driftsresultat",
    "profit_before_tax": "/resultatregnskapResultat/ordinaertResultatFoerSkattekostnad",
    "annual_result": "/resultatregnskapResultat/aarsresultat",
    "assets": "/eiendeler/sumEiendeler",
    "equity": "/egenkapitalGjeld/egenkapital/sumEgenkapital",
    "debt": "/egenkapitalGjeld/gjeldOversikt/sumGjeld",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _pointer(body: Any, path: str) -> tuple[bool, Any]:
    if path == "":
        return True, body
    value = body
    for part in path.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False, None
    return True, value


def _evidence(record: dict[str, Any], path: str, source_value: Any, *, raw: bool) -> dict[str, Any]:
    span = f"JSON pointer {path or '/'} = {_json(source_value)}"
    identity = [record.get("source_url"), record.get("content_sha256"), path, span,
                record.get("source_row_key")]
    return {
        "id": "ev-" + hashlib.sha256(_json(identity).encode()).hexdigest()[:24],
        "source_url": record.get("source_url") or "",
        "source_class": record.get("source_class") or record.get("source_type") or "unknown",
        "retrieved_at": record.get("retrieved_at"),
        "content_sha256": record.get("content_sha256"),
        "claim_span": span, "source_path": path, "source_value": deepcopy(source_value),
        "path_basis": "raw_response" if raw else "normalized_record",
        "source_row_key": record.get("source_row_key"),
        "effective_at": record.get("effective_at") or record.get("as_of"),
    }


def _claim(field: str, value: Any, record: dict[str, Any], module: str,
           path: str = "", *, raw_path: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    availability = AVAILABILITY.get(record.get("status"), "failed")
    raw = "raw_value" in record
    body = record.get("raw_value") if raw else record.get("value")
    selected_path = raw_path if raw and raw_path is not None else path
    found, source_value = _pointer(body, selected_path)
    if availability == "available" and (not found or value is None or value == ""):
        availability = "not_available"
    ev = _evidence(record, selected_path, source_value, raw=raw)
    claim = {
        "field": field, "value": deepcopy(value) if availability == "available" else None,
        "availability": availability, "confidence": 1.0 if availability == "available" else 0.0,
        "evidence_ids": [ev["id"]], "source_module": module, "freshness": "current",
    }
    if availability != "available":
        claim["note"] = record.get("note") or (
            "Field absent from the checked source." if record.get("status") == "available"
            else "Source did not provide a usable result; this does not establish a zero or absence."
        )
    if not raw:
        claim["audit_basis"] = "normalized_record; original response not retained"
    return claim, ev


def _official_claims(profile: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    claims: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    records = profile.get("evidence") or {}

    def add(field: str, value: Any, record: dict[str, Any], module: str,
            path: str = "", raw_path: str | None = None) -> None:
        claim, ev = _claim(field, value, record, module, path, raw_path=raw_path)
        claims.append(claim)
        evidence.append(ev)

    # A failed live request must not silently fall back to an older bulk row.
    registry_key = "registry_live" if "registry_live" in records else "registry"
    registry = records.get(registry_key)
    if registry:
        values = registry.get("value") or {}
        if registry_key == "registry":
            # Bulk evidence is the original CSV row; dotted headings are literal keys.
            for field, pointer in REGISTRY_PATHS.items():
                raw_key = pointer.lstrip("/").replace("/", ".")
                title_key = raw_key[:1].upper() + raw_key[1:]
                key = raw_key if raw_key in values else title_key
                value = profile.get(field)
                if field == "industry":
                    value = {"kode": profile.get("industry_code"), "beskrivelse": profile.get("industry_label")}
                if field in {"industry", "business_address", "postal_address"}:
                    # No fabricated nested object from missing CSV fields.
                    continue
                record = {**registry, "raw_value": values}
                add(f"registry.{field}", value, record, registry_key, raw_path="/" + key)
        else:
            for field, pointer in REGISTRY_PATHS.items():
                add(f"registry.{field}", values.get(field), registry, registry_key,
                    path="/" + field, raw_path=pointer)

    for module in ("financials", "financial_history", "roles", "locations", "group", "accounting_obligation"):
        record = records.get(module)
        if not record:
            continue
        values = record.get("value")
        if record.get("status") != "available":
            add(module, None, record, module)
            continue
        if module == "financials":
            rows = (values or {}).get("records", [])
            if not rows:
                add("financials.records", [], record, module, "/records", "")
            for index, row in enumerate(rows):
                period = row.get("period") or {}
                period_key = str(period.get("tilDato") or period.get("fraDato") or row.get("record_id") or index)
                for field, pointer in FINANCIAL_PATHS.items():
                    add(f"financials.{period_key}.{field}", row.get(field), record, module,
                        f"/records/{index}/{field}", f"/{index}{pointer}")
        elif module == "financial_history":
            add("financial_history.years", (values or {}).get("years", []), record, module, "/years", "")
        elif module == "roles":
            # Each source role is retained as evidence; normalization is documented.
            roles = (values or {}).get("roles", [])
            add("roles.roles", roles, record, module, "/roles", "/rollegrupper")
            claims[-1]["transformation"] = "official.normalize_roles: flatten rollegrupper[*].roller[*], preserve names, role/group codes and inactive flag"
        elif module == "locations":
            add("locations.locations", (values or {}).get("locations", []), record, module,
                "/locations", "/_embedded/underenheter")
            claims[-1]["transformation"] = "official.normalize_locations: select registered subunits, address, industry and employee count"
        elif module == "accounting_obligation":
            add("accounting_obligation.assessment", values, record, module)
            claims[-1]["claim_kind"] = "rule_interpretation"
            claims[-1]["confidence"] = 0.85
        else:
            add("group.structure", values, record, module)
    return claims, evidence


def _semantic(value: Any) -> Any:
    """Compare business facts, never retrieval times or unordered record order."""
    if isinstance(value, dict):
        return {key: _semantic(item) for key, item in sorted(value.items())
                if key not in {"retrieved_at", "last_checked_at", "fetched_at"}}
    if isinstance(value, list):
        values = [_semantic(item) for item in value]
        return sorted(values, key=_json) if all(isinstance(item, dict) for item in values) else values
    return value


def _module_record(records: dict[str, Any], module: str) -> dict[str, Any]:
    if module in {"registry", "registry_live"}:
        return records.get("registry_live") or records.get("registry") or {"status": "not_fetched"}
    return records.get(module) or {"status": "not_fetched"}


def _page_key(url: str) -> str:
    """Compare visited source pages without fragment or empty-root differences."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def _display_fact(value: Any, field: str) -> str:
    if field == "registry.industry" and isinstance(value, dict):
        return " ".join(str(part) for part in (value.get("beskrivelse"), f"({value['kode']})" if value.get("kode") else None) if part)
    if field == "registry.business_address" and isinstance(value, dict):
        streets = value.get("adresse") or []
        if isinstance(streets, str):
            streets = [streets]
        postal = " ".join(str(part) for part in (value.get("postnummer"), value.get("poststed") or value.get("kommune")) if part)
        return ", ".join(str(part) for part in [*streets, postal, value.get("land")] if part)
    if field.endswith(".period") and isinstance(value, dict):
        return " to ".join(str(part) for part in (value.get("fraDato"), value.get("tilDato")) if part)
    return str(value)


def _synthesis(claims: list[dict[str, Any]]) -> dict[str, Any]:
    by_field = {claim["field"]: claim for claim in claims}
    facts = []

    def add(text: str, selected: list[dict[str, Any]]) -> None:
        supported = [claim for claim in selected if claim.get("value") is not None and claim.get("evidence_ids")]
        if not supported or len(supported) != len(selected):
            return
        facts.append({"text": text, "claim_fields": [claim["field"] for claim in supported],
                      "evidence_ids": sorted({ref for claim in supported for ref in claim["evidence_ids"]}),
                      "freshness": "stale" if any(claim.get("freshness") == "stale" for claim in supported) else "current"})

    for field, label in (("registry.name", "Registered name"), ("registry.legal_form", "Legal form"),
                         ("registry.employees", "Registered employee count"),
                         ("registry.industry", "Registered primary industry"),
                         ("registry.business_address", "Registered business address")):
        claim = by_field.get(field)
        if claim and claim.get("value") is not None and claim.get("evidence_ids"):
            value = _display_fact(claim["value"], field)
            add(f"{label}: {value}", [claim])

    # Reporting-period keys come from the accounts source, never a guessed current year.
    periods = sorted({field.rsplit(".", 1)[0] for field in by_field
                      if field.startswith("financials.") and field.endswith(".period")
                      and by_field[field].get("value")}, reverse=True)
    if periods:
        prefix = periods[0]
        period = by_field[prefix + ".period"]
        currency = by_field.get(prefix + ".currency")
        for metric, label in (("revenue", "Reported operating revenue"), ("annual_result", "Reported annual result")):
            value = by_field.get(prefix + "." + metric)
            if value and value.get("value") is not None and currency and currency.get("value"):
                add(f"{label}: {value['value']} {currency['value']}; reporting period {_display_fact(period['value'], prefix + '.period')}.",
                    [value, currency, period])
    roles = by_field.get("roles.roles")
    if roles and isinstance(roles.get("value"), list):
        inactive = sum(bool(item.get("inactive")) for item in roles["value"])
        add(f"Registry lists {len(roles['value'])} role records, including {inactive} marked inactive; these are role records, not a unique-person headcount.", [roles])
    website = by_field.get("official_website")
    if website and website.get("value"):
        add(f"Exact-entity company website observed: {website['value']}.", [website])
    careers = by_field.get("careers.page")
    if careers and careers.get("value"):
        add("A company careers page was observed. A careers page alone does not establish active vacancies.", [careers])
    jobs = by_field.get("jobs.opening")
    if jobs and jobs.get("value"):
        postings = jobs["value"] if isinstance(jobs["value"], list) else [jobs["value"]]
        add(f"Observed {len(postings)} company-published job posting(s) with explicit posting/expiry dates covering the retrieval date; bounded collection is not a complete vacancy count.", [jobs])
    return {"method": "deterministic_sourced_facts", "facts": facts,
            "note": "Company descriptions and financial trends are not inferred from missing data."}


def make_envelope(profile: dict[str, Any], run_id: str, started_at: str, completed_at: str,
                  previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Adapt one profile to OUTPUT_CONTRACT; preserve history through fetch failures.

    ``previous`` may be a previous envelope or a legacy profile. Request telemetry
    is read from ``profile['run_metrics']``. Neither input is mutated.
    """
    org = str(profile.get("organisation_number") or "")
    if not org:
        raise ValueError("organisation_number is required")
    if previous and str(previous.get("organisation_number") or "") != org:
        raise ValueError("Refresh requires the same exact organisation number")
    if previous and "claims" not in previous:
        previous = make_envelope(previous.get("profile", previous), "legacy", started_at, started_at)
    claims, evidence = _official_claims(profile)
    records = deepcopy(profile.get("evidence") or {})
    incomplete_modules: set[str] = set()
    if "website" in records:
        from .enrichment import build_website_claims
        live = records.get("registry_live", {})
        # Use current registry identity for website rechecks, while preserving the
        # original discovery profile unchanged in the returned envelope.
        website_profile = {**profile, **{key: value for key, value in (live.get("value") or {}).items()
                                        if value is not None}} if live.get("status") == "available" else profile
        website_result = build_website_claims(website_profile)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for claim in website_result.get("claims", []):
            grouped.setdefault(claim["field"], []).append(claim)
        # Stable field keys preserve every observed contact/job, rather than
        # overwriting all but the final item sharing a field.
        for field, items in grouped.items():
            claims.append({"field": field, "value": sorted([deepcopy(item["value"]) for item in items], key=_json),
                           "availability": "available", "confidence": min(item.get("confidence", 0) for item in items),
                           "evidence_ids": sorted({ref for item in items for ref in item["evidence_ids"]}),
                           "source_module": "website", "category": items[0].get("category"),
                           "freshness": "current", "claim_kind": "company_reported",
                           "observation_count": len(items)})
        evidence.extend(deepcopy(website_result.get("evidence", [])))
        website = records["website"]
        if website_result.get("identity_assessment", {}).get("publishable"):
            value = website.get("value") or {}
            url = value.get("final_url") or website.get("source_url")
            span = value.get("main_text_excerpt") or value.get("title") or ""
            ev = _evidence(website, "/main_text_excerpt", span, raw=False)
            ev["claim_span"] = str(span)
            ev["source_class"] = "company_site"
            claims.append({"field": "official_website", "value": url, "availability": "available",
                           "confidence": 0.95, "evidence_ids": [ev["id"]], "source_module": "website",
                           "freshness": "current", "identity_assessment": website_result["identity_assessment"]})
            evidence.append(ev)
        elif website.get("status") == "available":
            records["website"] = {**website, "status": "ambiguous", "note": "Exact company identity was not established."}
        if (website.get("value") or {}).get("crawl_errors"):
            incomplete_modules.add("website")
        website_status = AVAILABILITY.get(records["website"].get("status"), "failed")
        present_fields = {claim["field"] for claim in claims}
        for field in ("official_website", "company_profiles", "careers.page", "jobs.opening", "activity.article"):
            if field in present_fields:
                continue
            availability = ("failed" if "website" in incomplete_modules else "not_available") if website_status == "available" else website_status
            ev = _evidence(website, "", website.get("note") or (website.get("value") or {}).get("main_text_excerpt"), raw=False)
            evidence.append(ev)
            claims.append({"field": field, "value": None, "availability": availability,
                           "confidence": 0.0, "evidence_ids": [ev["id"]], "source_module": "website",
                           "freshness": "current", "note": "Not established within the bounded, identity-gated pages checked. This is not proof of absence or a zero count. Social profiles are outbound-link observations only."})
        for claim in claims:
            claim.setdefault("freshness", "current")

    errors = []
    modules = {}
    for module, record in records.items():
        availability = AVAILABILITY.get(record.get("status"), "failed")
        modules[module] = {"state": evidence_terminal_state(record),
                           "retry_count": int(record.get("retry_count") or 0),
                           "final_timestamp": record.get("retrieved_at") or completed_at}
        if availability in RETAIN_ON:
            errors.append({"module": module, "code": record.get("status") or "unknown_status",
                           "message": record.get("note") or "Source check did not produce a usable result.",
                           "source_url": record.get("source_url"), "retryable": availability == "failed"})
        if module in incomplete_modules:
            errors.append({"module": module, "code": "partial_crawl", "message": "Some permitted pages failed; missing previous website observations are retained as stale.",
                           "source_url": record.get("source_url"), "retryable": True})

    current = {claim["field"]: claim for claim in claims}
    old = {claim["field"]: claim for claim in (previous or {}).get("claims", [])}
    evidence_by_id = {item["id"]: item for item in evidence}
    old_evidence = {item["id"]: item for item in (previous or {}).get("evidence", [])}
    website_record = records.get("website") or {}
    website_value = website_record.get("value") or {}
    visited_urls = {_page_key(str(page["url"])) for page in website_value.get("pages", []) if page.get("url")}
    if website_record.get("status") == "available" and website_value.get("final_url"):
        visited_urls.add(_page_key(website_value["final_url"]))
    changes = []
    for field, old_claim in old.items():
        module = old_claim.get("source_module") or field.split(".", 1)[0]
        record = _module_record(records, module)
        availability = AVAILABILITY.get(record.get("status"), "failed")
        candidate = current.get(field)
        if module in incomplete_modules and (candidate is None or candidate.get("availability") == "not_available"):
            availability = "failed"
        if candidate and candidate.get("availability") in RETAIN_ON:
            availability = candidate["availability"]
        elif candidate and candidate.get("availability") == "available":
            availability = "available"
        retain_reason = None
        if module == "website" and record.get("status") == "available" and old_claim.get("value") is not None:
            old_value, new_value = old_claim["value"], (candidate or {}).get("value")
            if isinstance(old_value, list):
                current_items = {_json(_semantic(item)) for item in new_value} if isinstance(new_value, list) else set()
                missing_observation = any(_json(_semantic(item)) not in current_items for item in old_value)
            else:
                missing_observation = candidate is None or candidate.get("availability") != "available"
            prior_urls = {_page_key(str(old_evidence[ref]["source_url"])) for ref in old_claim.get("evidence_ids", [])
                          if ref in old_evidence and old_evidence[ref].get("source_url")}
            unrevisited = sorted(prior_urls - visited_urls)
            if missing_observation and (module in incomplete_modules or unrevisited):
                # A bounded or interrupted crawl cannot establish deletion of a
                # member from an aggregated contact/job/activity list. Retain the
                # whole prior supported claim; current observations remain auditable.
                availability = "failed" if module in incomplete_modules else "ambiguous"
                retain_reason = "Partial crawl did not reverify every previous observation." if module in incomplete_modules else "Previous supporting page was not revisited in this bounded crawl."
                if unrevisited and module not in incomplete_modules:
                    errors.append({"module": module, "field": field, "code": "source_not_revisited",
                                   "message": retain_reason, "source_urls": unrevisited, "retryable": True})
        if availability in RETAIN_ON and old_claim.get("value") is not None and old_claim.get("evidence_ids"):
            retained = deepcopy(old_claim)
            retained.update(availability=availability, freshness="stale", last_checked_at=completed_at,
                            note=retain_reason or "Last supported value retained; current source check did not verify it.")
            if retain_reason and candidate:
                retained["current_check"] = {"observed_value": deepcopy(candidate.get("value")),
                                             "availability": candidate.get("availability"),
                                             "evidence_ids": list(candidate.get("evidence_ids", []))}
            retained.setdefault("last_supported_at", (previous or {}).get("run", {}).get("completed_at"))
            current[field] = retained
            for evidence_id in retained["evidence_ids"]:
                if evidence_id in old_evidence:
                    evidence_by_id[evidence_id] = deepcopy(old_evidence[evidence_id])
        elif candidate is None and availability not in RETAIN_ON:
            # Successful current check removes a formerly observed item, not an error.
            current[field] = {"field": field, "value": None, "availability": "not_available",
                              "confidence": 0.0, "evidence_ids": [], "source_module": module,
                              "freshness": "current", "note": "No longer present in the checked source."}
            _, ev = _claim(field, None, record, module)
            evidence_by_id[ev["id"]] = ev
            current[field]["evidence_ids"] = [ev["id"]]

    for field in sorted(set(old) | set(current)):
        old_claim, new_claim = old.get(field), current.get(field)
        if not previous or not new_claim or new_claim.get("freshness") == "stale":
            continue
        old_value = old_claim.get("value") if old_claim else None
        new_value = new_claim.get("value")
        if _semantic(old_value) == _semantic(new_value):
            continue
        # Only a successful checked source can establish a material business change.
        if new_claim["availability"] not in {"available", "not_available", "not_applicable"}:
            continue
        old_ids = (old_claim or {}).get("evidence_ids", [])
        for evidence_id in old_ids:
            if evidence_id in old_evidence:
                evidence_by_id.setdefault(evidence_id, deepcopy(old_evidence[evidence_id]))
        changes.append({"field": field, "old_value": old_value, "new_value": new_value,
                        "old_evidence_ids": old_ids, "new_evidence_ids": new_claim.get("evidence_ids", []),
                        "detected_at": completed_at, "kind": "added" if old_value is None else "removed" if new_value is None else "changed"})

    metrics = deepcopy(profile.get("run_metrics") or {})
    try:
        runtime = round((datetime.fromisoformat(completed_at.replace("Z", "+00:00")) -
                         datetime.fromisoformat(started_at.replace("Z", "+00:00"))).total_seconds() * 1000)
    except (ValueError, TypeError):
        runtime = None
    operations = {"requests": metrics.get("requests"), "runtime_ms": metrics.get("runtime_ms", runtime),
                  "third_party_cost_usd": metrics.get("third_party_cost_usd", 0), **metrics}
    if operations["requests"] is None:
        operations["measurement_note"] = "Request count unavailable; null is not zero requests."
    final_claims = [current[field] for field in sorted(current)]
    state = "submission_error" if any(item["state"] == "submission_error" for item in modules.values()) else "complete"
    return {
        "organisation_number": org,
        "run": {"run_id": run_id, "started_at": started_at, "completed_at": completed_at,
                "terminal_status": "completed_with_errors" if errors else "completed"},
        "claims": final_claims, "evidence": list(evidence_by_id.values()), "changes": changes,
        "errors": errors, "operations": operations, "synthesis": _synthesis(final_claims),
        "profile": deepcopy(profile), "state": state, "modules": modules,
        "run_id": run_id, "started_at": started_at, "completed_at": completed_at,
    }
