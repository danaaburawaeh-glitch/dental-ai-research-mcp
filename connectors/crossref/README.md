# Crossref REST API Connector

See `../../docs/CROSSREF_CONNECTOR_SPEC.md` for the full specification, and
`../../docs/CONNECTOR_IMPLEMENTATION_DECISION.md` for exactly what has and hasn't been
live-verified.

## Role: metadata / citation verification, NOT full-text access, NOT primary evidence search

See `models.py`'s `CAPABILITY_LABEL_NOT_FULL_TEXT_NOTE`. Crossref supplements PubMed for
dual-source citation verification (Phase 5) — it does not replace it.

## Files

- `client.py` — `crossref_lookup_doi`, `crossref_search_bibliographic`. CLI entry point.
- `models.py` — Crossref type constants, capability-label constants.
- `parser.py` — `/works` JSON parsing into `EvidenceRecord`-shaped dicts.
- `rate_limit.py` — separate token buckets for single-record vs list/query requests; current
  (post-2025-12-01) limits: 5 req/s public / 10 req/s polite (via `CROSSREF_MAILTO`), NOT the
  outdated 50 req/s figure some documentation still shows.
- `errors.py` — failure-state taxonomy, including `IDENTIFIER_MISMATCH` for dual-verification
  disagreements.

## Usage (via Bash tool, from a skill)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/connectors/crossref/client.py" lookup-doi \
  --doi "10.1007/s00784-021-04289-6"

python3 "${CLAUDE_PLUGIN_ROOT}/connectors/crossref/client.py" search-bibliographic \
  --citation "Smielak Armata Bojar veneers survival 2021"
```

## Configuration

`CROSSREF_MAILTO` (recommended — raises rate limit and identifies the request per Crossref's
"polite pool" convention). No paid API key required or used in Phase A.

## Status

Not `CONNECTED`. See `../../skills/evidence-research/references/connector-capability-map.md`'s
Implementation Ledger.
