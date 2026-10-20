#!/usr/bin/env python3
"""Northstar evaluator: bounded live collection, auditable claims, immutable runs."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import read_organisation_inputs, validate_envelopes
from norway_company_agent.evidence import evidence, utc_now
from norway_company_agent.http import start_trace, fetch_trace
from norway_company_agent.identity import apply_website_identity_gate
from norway_company_agent.official import fetch_official_modules, accounting_obligation_assessment
from norway_company_agent.operations import percentile, peak_rss_bytes
from norway_company_agent.sampling import iter_bulk
from norway_company_agent.website import fetch_website

MODULES = ("registry", "registry_live", "financials", "roles", "group", "locations", "website")

def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)

def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)

def load_snapshot(path: Path, orgs: list[str]) -> tuple[dict, dict]:
    wanted, found = set(orgs), {}
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    timestamp = utc_now()
    for profile in iter_bulk(path):
        org = profile["organisation_number"]
        if org not in wanted:
            continue
        raw = profile.pop("raw")
        profile["evidence"] = {"registry": evidence(
            "registry", "available", "official_registry_bulk",
            "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
            value=raw, content_sha256=digest, retrieved_at=timestamp, source_row_key=org)}
        profile["evidence"]["registry"]["extraction_method"] = "frozen-csv-row-v1"
        found[org] = profile
        if len(found) == len(wanted):
            break
    for org in orgs:
        if org not in found:
            found[org] = {"organisation_number": org, "name": "", "evidence": {
                "registry": evidence("registry", "not_found", "official_registry_bulk",
                    "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                    note="Organisation absent from evaluator-supplied frozen snapshot", content_sha256=digest)}}
    return found, {"sha256": digest, "path_name": path.name, "matched": sum(bool(p.get("name")) for p in found.values()), "cached_external_claims_used": False}


def emergency_envelope(profile, run_id, started_at, completed_at, exc, previous=None):
    """Last-resort terminal output, independent of the adapter that just failed."""
    prior = previous or {}
    claims = copy.deepcopy(prior.get("claims", []))
    for claim in claims:
        claim.update(availability="failed", freshness="stale", last_checked_at=completed_at,
                     note="Previous supported value retained; this run could not construct a profile.")
    return {
        "organisation_number": profile["organisation_number"], "run_id": run_id,
        "started_at": started_at, "completed_at": completed_at, "state": "submission_error",
        "run": {"run_id": run_id, "started_at": started_at, "completed_at": completed_at,
                "terminal_status": "failed"},
        "claims": claims, "evidence": copy.deepcopy(prior.get("evidence", [])), "changes": [],
        "errors": [{"module": "envelope", "code": type(exc).__name__, "message": str(exc)[:240]}],
        "operations": {"requests": None, "runtime_ms": None, "third_party_cost_usd": 0,
                       "measurement_note": "Run failed before complete telemetry was returned."},
        "profile": copy.deepcopy(profile),
        "modules": {module: {"state": "source_error", "retry_count": 0,
                             "final_timestamp": completed_at} for module in MODULES},
    }

def run(args) -> dict:
    from norway_company_agent.northstar_contract import make_envelope
    started_at, start = utc_now(), time.monotonic()
    inputs = read_organisation_inputs(args.organisations)
    orgs = [r["organisation_number"] for r in inputs]
    if args.expected_count is not None and len(orgs) != args.expected_count:
        raise ValueError(f"Expected {args.expected_count} inputs, received {len(orgs)}")
    output_dir = Path(args.output_dir)
    # Never silently replace a completed run or its immutable previous-snapshot input.
    if (output_dir / "run-report.json").exists():
        raise ValueError("Output directory contains a completed run. Choose a new --output-dir.")
    output_dir.mkdir(parents=True, exist_ok=True)
    previous = {}
    if args.previous:
        for line in Path(args.previous).read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                previous[row["organisation_number"]] = row
    profiles, snapshot = load_snapshot(Path(args.bulk), orgs)
    state = {}

    def enrich(org):
        profile = copy.deepcopy(profiles[org])
        company_start, company_at = time.monotonic(), utc_now()
        start_trace(output_dir / "source-snapshots")
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        errors = []
        try:
            records, _ = fetch_official_modules(org, {"registry_live", "financials", "roles", "group", "locations"})
            profile["evidence"].update(records)
            live = records.get("registry_live", {})
            if not profile.get("name") and live.get("status") == "available":
                # Missing frozen identities remain explicitly marked; live data is a separate source.
                profile.update({k:v for k,v in (live.get("value") or {}).items() if v is not None})
            live_values = live.get("value") or {}
            # A current registry URL/name supersedes the frozen discovery row for
            # website fetching and exact-entity checks; the frozen row stays intact.
            identity_profile = {**profile, **{k: v for k, v in live_values.items() if v is not None}} if live.get("status") == "available" else profile
            website_url = live_values.get("website") if live.get("status") == "available" else profile.get("website")
            website, website_metrics = fetch_website(website_url, timeout=args.website_timeout)
            profile["evidence"]["website"] = apply_website_identity_gate(identity_profile, website)["website"]
        except Exception as exc:
            errors.append({"type": type(exc).__name__, "message": str(exc)[:240]})
        for module in MODULES:
            if module not in profile["evidence"]:
                profile["evidence"][module] = evidence(module, "source_error", "runtime", "",
                                                        note="Module interrupted before completion")
        trace = fetch_trace()
        profile["run_metrics"] = {
            "requests": len(trace) + website_metrics["requests"],
            "official_requests": len(trace), "website_requests": website_metrics["requests"],
            "bytes": sum(r["bytes"] for r in trace) + website_metrics["bytes"],
            "runtime_ms": round((time.monotonic() - company_start) * 1000),
            "third_party_cost_usd": 0,
            "official_request_log": trace,
        }
        profile["runtime_errors"] = errors
        return make_envelope(profile, args.run_id, company_at, utc_now(), previous=previous.get(org))

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, org): org for org in orgs}
        for index, future in enumerate(as_completed(futures), 1):
            org = futures[future]
            try:
                envelope = future.result()
            except Exception as exc:
                profile = copy.deepcopy(profiles[org])
                profile["evidence"] = {m: evidence(m, "source_error", "runtime", "", note=str(exc)[:240]) for m in MODULES}
                profile["runtime_errors"] = [{"type":type(exc).__name__, "message":str(exc)[:240]}]
                try:
                    envelope = make_envelope(profile, args.run_id, started_at, utc_now(), previous=previous.get(org))
                except Exception as adapter_error:
                    envelope = emergency_envelope(profile, args.run_id, started_at, utc_now(), adapter_error,
                                                  previous=previous.get(org))
            state[org] = envelope
            write_json(output_dir / "companies" / (org + ".json"), envelope)
            if index % 10 == 0 or index == len(orgs):
                write_jsonl(output_dir / "envelopes.jsonl", [state[o] for o in orgs if o in state])
                print(f"Completed {index}/{len(orgs)} companies", flush=True)
    envelopes = [state[org] for org in orgs]
    write_jsonl(output_dir / "envelopes.jsonl", envelopes)
    metrics = [e.get("operations", {}) for e in envelopes]
    claims = [c for e in envelopes for c in e.get("claims", [])]
    evidence_index_valid = all(all(i in {v["id"] for v in e.get("evidence", [])} for i in c.get("evidence_ids", []))
                               for e in envelopes for c in e.get("claims", []))
    available_supported = all(c.get("evidence_ids") for c in claims if c.get("availability") == "available")
    coverage = Counter(c["field"] for c in claims if c.get("availability") == "available")
    validation = validate_envelopes(envelopes, len(orgs))
    validation["checks"].update({"all_evidence_references_resolve": evidence_index_valid, "available_claims_have_evidence": available_supported,
                                "input_order_preserved": [e["organisation_number"] for e in envelopes] == orgs})
    validation["passed"] = all(validation["checks"].values())
    report = {
        "agent": "Northstar", "version": "1.0", "run_id": args.run_id,
        "started_at": started_at, "completed_at": utc_now(), "input_count": len(orgs), "emitted_envelopes":len(envelopes),
        "input_sha256":hashlib.sha256(Path(args.organisations).read_bytes()).hexdigest(),
        "registry": snapshot, "validation": validation,
        "operations": {"wall_runtime_ms": round((time.monotonic()-start)*1000), "workers":args.workers,
            "requests":sum(m["requests"] for m in metrics) if all(m.get("requests") is not None for m in metrics) else None,
            "measured_requests_total":sum(m.get("requests") or 0 for m in metrics),
            "entities_with_missing_request_telemetry":sum(m.get("requests") is None for m in metrics),
            "bytes":sum(m.get("bytes") or 0 for m in metrics),
            "company_p50_ms":percentile([m["runtime_ms"] for m in metrics if m.get("runtime_ms") is not None], .5),
            "company_p95_ms":percentile([m["runtime_ms"] for m in metrics if m.get("runtime_ms") is not None], .95),
            "peak_rss_bytes":peak_rss_bytes(), "third_party_cost_usd":0,
            "cost_exclusions":["host compute/network", "development assistant usage"]},
        "availability_counts": dict(Counter(c.get("availability") for c in claims)),
        "available_claim_counts_by_field": dict(sorted(coverage.items())),
        "source_module_states":dict(Counter(s["state"] for e in envelopes for s in e.get("modules",{}).values())),
        "ground_truth_metrics":{"precision":None,"recall":None,"wrong_company_rate":None,
            "note":"Not measured: requires independently labeled reference set. Contract validation is not competition qualification."},
        "previous_snapshot_used":bool(args.previous),
        "source_policy":"Official registry APIs and bounded robots-permitted company pages. No social platform scraping or paid connectors.",
    }
    write_json(output_dir / "run-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organisations", required=True)
    parser.add_argument("--bulk", required=True, help="Evaluator frozen BRREG CSV, plain or gzip")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id", default="northstar-live")
    parser.add_argument("--previous", help="Previous immutable envelopes JSONL; always refetches current sources")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--website-timeout", type=float, default=8.0)
    args = parser.parse_args()
    if not 1 <= args.workers <= 8 or not 1 <= args.website_timeout <= 30:
        parser.error("workers must be 1..8 and website-timeout 1..30 seconds")
    report = run(args)
    raise SystemExit(0 if report["validation"]["passed"] else 1)

if __name__ == "__main__":
    main()
