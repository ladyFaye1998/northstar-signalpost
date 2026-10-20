# Northstar

Evidence-first company research for the Signalpost challenge. Give it Norwegian
organisation numbers: it returns sourced claims, an explicit record of missing or
blocked information, material changes, and a local evidence browser.

Northstar extends the organizer's reference agent with exact-entity checks,
bounded company-site extraction, auditable claim envelopes, conservative refresh,
request accounting, raw source snapshots, and a responsive company/evidence UI.
It needs no runtime model, API key, payment account, or hosted service.

## Reproduce

Python 3.12 and uv are required. Install the locked environment:

```sh
uv sync --frozen
```

The evaluator supplies its own organisation-number JSONL and frozen BRREG CSV
(plain or gzip). The command accepts any batch size; it does not use the smoke
companies to choose or answer official inputs:

```sh
uv run --frozen python scripts/run_northstar.py --organisations INPUT.jsonl --bulk FROZEN_BRREG.csv.gz --output-dir out/evaluation-001 --run-id evaluation-001
```

Each input yields exactly one envelope in `out/evaluation-001/envelopes.jsonl`, in
input order. Each envelope includes `run`, `claims`, `evidence`, `changes`, `errors`,
`operations`, and the reference-compatible `profile`, `state`, `modules`. Broken
sources and an interrupted company adapter produce terminal failure records.
Invalid input manifests fail before starting network work.

`run-report.json` records count/order/evidence checks, measured runtime and request
counts, coverage counts and availability states. Source responses are saved under
`source-snapshots/<sha256>.bin`. These are runtime audit artifacts, not precomputed
submission answers. No external claim cache is read by the evaluator command.

## Live 100-company smoke test

```sh
curl -L https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv -o brreg-enheter.csv.gz
curl -L https://builderr.ai/signalpost-company-universe-2025.jsonl.gz -o universe.jsonl.gz
uv run --frozen python select_entry_batch.py --universe universe.jsonl.gz --count 100 --seed 20261020 --output smoke-companies.jsonl
uv run --frozen python scripts/run_northstar.py --organisations smoke-companies.jsonl --bulk brreg-enheter.csv.gz --output-dir out/smoke-001 --run-id smoke-001 --expected-count 100
```

The universe archive SHA-256 is
`1c89710e5b01f8617e86d09fbdff4a52f2f8dbbba297e74f7164b5984f5a0384`.
`data/smoke-companies.jsonl` records the reproducible random local sample. The
submitted aggregate test report is in `reports/`; the evaluator must supply its own
fresh test inputs. A smoke run does not establish competition qualification,
precision or recall. Only the organizer's independent official evaluation can do
that.

## Inspect and refresh

```sh
uv run --frozen python scripts/serve_northstar.py --data out/smoke-001/envelopes.jsonl --port 8765
```

Open <http://127.0.0.1:8765>. Search a legal name or organisation number, inspect its
profile, and open a claim's evidence. `/companies/ORG` deep links are supported.
The browser is read-only: use the CLI for network collection and refresh.

```sh
uv run --frozen python scripts/run_northstar.py --organisations smoke-companies.jsonl --bulk brreg-enheter.csv.gz --previous out/smoke-001/envelopes.jsonl --output-dir out/refresh-002 --run-id refresh-002
```

Use a new output directory for every run. Failure to retrieve a source retains its
old supported values and original evidence as stale, with the current failure
visible. Retrieval time and harmless record ordering do not create business changes.
The prior output file is never overwritten. Supported available values, unknowns,
ambiguous matches and blocked collection stay distinguishable.

## Budgets and cost

Defaults: four company workers; official API starts paced to at most eight/second;
two attempts per official endpoint (ten-second socket timeout); homepage plus at
most four prioritized internal pages; bounded website time and byte budgets.
`--workers 1..8` and `--website-timeout 1..30` permit evaluator resource adjustments.
Socket timeouts are not a hard whole-process deadline. See the measured report for
wall time and extrapolate only with that limitation.

Third-party API cost is $0 for 100 or 1,000 companies using the included connectors.
This excludes host compute/network and the AI assistant used to develop the entry.
There is no automatic purchase or paid fallback. Low coverage is reported honestly.

## Tests and provenance

```sh
uv run --frozen --with pytest==8.4.2 pytest -q
```

Tests cover exact identity, same-name/group rejection, source handling, financial
paths, stale refresh, changed values, output order, failed adapters and UI APIs.
Dependencies are locked in `uv.lock`; installed licence metadata is in
`docs/dependency-licenses.json`. Read `docs/PROVENANCE.md` for the reference starter,
source licences, website collection policy and limitations. `README.reference.md`
is the original starter's guide. The optional legacy connectors are not part of
the Northstar evaluator command.

## Recorded validation (20 October 2026)

- 147 tests passed.
- Final live smoke: 100/100 envelopes, 90.156 seconds, 533 HTTP requests,
  zero silent drops and zero third-party API charges.
- 100 legal identities and role results; 99 companies with 2025 financial records.
- 8 registry-listed websites; 3 passed exact-entity publication checks. External
  coverage is limited: company contacts, one careers surface, outbound profiles,
  and one company with dated activity links. These counts are not precision/recall.
- Same-source refresh replay: 100 companies, zero material changes, zero missing
  evidence references, prior file preserved.
- Desktop and 390px mobile layouts, evidence drawer, search, source links and
  read-only API behavior checked.

See `reports/smoke-100.json`, `reports/refresh-replay.json`, `reports/tests.json`,
and `reports/source-audit.json` for the recorded scope and limitations.
