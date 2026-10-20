# Sources, rights, and reproducibility

Northstar is a competition entry built on Builderr's provided Signalpost reference
agent, downloaded on 20 October 2026 from
<https://builderr.ai/signalpost-starter-kit.tar.gz>. The original guide is preserved
as `README.reference.md`. Original files and the reference tests remain attributed
to that starter. The archive did not include a standalone LICENSE file. We do not
relicense the reference code or assert a broader commercial redistribution grant;
it is included as the organizer-provided foundation for its requested competition
submission. Northstar's original additions are licensed separately in
`LICENSE.northstar`.

New entry components: `scripts/run_northstar.py`, `scripts/serve_northstar.py`,
`src/norway_company_agent/northstar_contract.py`,
`src/norway_company_agent/enrichment.py`, `web/index.html`, and corresponding new
tests. Improvements also modify the reference HTTP, identity, website, official,
sampling and operations modules.

## Runtime sources

- Brønnøysundregistrene Enhetsregisteret: frozen bulk CSV; live entity, roles,
  group structure and subunit endpoints. Source attribution and exact URLs travel
  with each claim. [Official documentation](https://data.brreg.no/enhetsregisteret/api/dokumentasjon/en/index.html)
  identifies NLOD 2.0. Only unauthenticated open endpoints are used.
- Regnskapsregisteret open annual accounts endpoint: current public account records,
  with currency and period retained, including the available prior records returned
  by that endpoint. No invented financial values and no private account APIs.
  [BRREG dataset policy](https://www.brreg.no/bruke-data-fra-bronnoysundregistrene/datasett-og-api/)
  states NLOD for its datasets.
- Registry-listed company websites: bounded public HTML requests, with robots
  checks and source excerpts. These are company assertions, not independently
  verified government facts. A missing robots file is not a general content licence.
  Known disallow rules and failed policy checks block collection. There is no claim
  of a blanket redistribution licence for website text. Raw snapshots stay local
  to the evaluation workspace; the public repository contains code and aggregate
  reports, not a redistributed website corpus.
- Social profiles are outbound links observed on a verified company site. No
  LinkedIn, Meta, Indeed or other social-platform pages are scraped. Legacy starter
  connector examples are not invoked by the Northstar run command.

No models, inference APIs, credentials, browser login, or paid search services are
required at runtime. Development used an AI coding assistant; its usage and host
compute/network costs are excluded from the reported $0 third-party API cost.
`uv.lock` pins runtime dependencies; `docs/dependency-licenses.json` records their
installed metadata. Evaluation uses Python 3.12.

## Audit and refresh

Each run has a separate directory with per-company JSON, an ordered JSONL output,
a machine-readable report, and content-addressed source snapshots. A completed run
cannot be silently overwritten. Raw responses and full registry downloads are
excluded from Git. Public professional-role facts are extracted for company
research; raw source fields are not assumed necessary for display.

Pass the previous run's `envelopes.jsonl` to `--previous` to refetch all current
sources. Failed/blocked checks retain supported old values with their original
evidence and an explicit stale label. Source failures are not deletions. Repeated
unchanged values do not create material-change events just because hashes or
retrieval dates changed. Old and new evidence references accompany real changes.

## Limits

The crawler is deliberately bounded. It does not solve JavaScript-only sites,
paywalls, all unlisted websites, historical PDF extraction, ratings or news outside
company sites. Registry links and source numbers are stronger than name matching;
unresolved exact identity is reported as ambiguous. Structured job posts are
different from a careers link. Coverage in a random sample may be low. Precision
and recall require independent labels and are not manufactured from self-checks.
Local tests are not an official competition score or qualification.
