# PubMed / NCBI E-utilities Connector

See `../../docs/PUBMED_CONNECTOR_SPEC.md` for the full specification, and
`../../docs/CONNECTOR_IMPLEMENTATION_DECISION.md` for exactly what has and hasn't been
live-verified.

## Files

- `client.py` — `pubmed_search`, `pubmed_fetch`, `pubmed_search_systematic_reviews`,
  `pubmed_search_clinical_studies`. CLI entry point for invocation via the Bash tool.
- `models.py` — publication-type/MeSH constants, RCT disambiguation.
- `parser.py` — ESearch/EFetch XML parsing into `EvidenceRecord`-shaped dicts.
- `rate_limit.py` — token-bucket limiter, 3 req/s (no key) or 10 req/s (`NCBI_API_KEY` set).
- `errors.py` — failure-state taxonomy.

## Usage (via Bash tool, from a skill)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/connectors/pubmed/client.py" search \
  --query "minimal preparation porcelain veneers survival" --max-results 20

python3 "${CLAUDE_PLUGIN_ROOT}/connectors/pubmed/client.py" fetch --pmids "12345678,23456789"

python3 "${CLAUDE_PLUGIN_ROOT}/connectors/pubmed/client.py" search-systematic-reviews \
  --query "porcelain veneers survival"

python3 "${CLAUDE_PLUGIN_ROOT}/connectors/pubmed/client.py" search-clinical-studies \
  --query "no-prep veneers" --designs rct,cohort
```

Each command prints a single JSON object to stdout and exits 0 on `SUCCESS`/`ZERO_RESULTS`,
non-zero on any other status (see `errors.py`).

## Configuration (environment variables — see `.env.example` and `../../docs/CONNECTOR_SECURITY.md`)

`NCBI_API_KEY` (optional), `NCBI_TOOL` (default `dana_dental_evidence`), `NCBI_EMAIL`
(recommended).

## Status

Not `CONNECTED`. See `../../skills/evidence-research/references/connector-capability-map.md`'s
Implementation Ledger for exactly why "built and unit-tested" is not the same as "connected."
