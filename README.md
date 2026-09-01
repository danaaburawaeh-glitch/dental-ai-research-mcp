# Dental AI Research MCP

Remote MCP server giving **Dental Research & Clinical Intelligence by Dr. Dana** real
research tools inside **Claude Web** and **Claude Desktop**.

> **Status: MVP, validated locally. NOT deployed. NOT yet wired into the Dental AI plugin.**
> The public Dental AI v1.0.2 plugin is **unmodified**. `DENTAL-AI-MASTER` is **untouched**.

---

## What a dentist can do with it

| Tool | What it does |
|---|---|
| `search_pubmed` | Search PubMed/NCBI for dental and biomedical literature |
| `search_systematic_reviews` | Search PubMed restricted to Systematic Review / Meta-Analysis publication types |
| `verify_citation` | Verify a DOI / PMID / title against Crossref **and** PubMed |
| `search_clinical_trials` | Search the ClinicalTrials.gov registry (API v2) |

Exactly four tools. Nothing else is exposed in v1.

**Deliberately out of scope:** SFDA, Cochrane, Embase, Scopus, clinical guidelines,
manufacturer IFUs, database, user accounts, dashboard, analytics, new clinical features.

---

## Architecture

```
Claude Web / Claude Desktop
        │  MCP Streamable HTTP (JSON-RPC 2.0 over POST /mcp)
        ▼
   server.py            zero-dependency WSGI app — transport + tool registry
        │
        ▼
   tools.py             the 4 tool contracts, normalization, provenance, failure semantics
        │
        ▼
connector_bridge.py     isolated in-process loading of three connectors
        │
        ▼
   connectors/          VENDORED VERBATIM from Dental AI v1.0.2 — retrieval logic unchanged
   ├── pubmed/          ESearch + EFetch, retry, rate limiting, XML parsing
   ├── crossref/        DOI lookup, bibliographic search
   ├── clinical_trials/ ClinicalTrials.gov API v2
   └── shared/          EvidenceRecord, provenance, retry, identifiers
        │
        ▼
   PubMed · Crossref · ClinicalTrials.gov   (public APIs, no auth)
```

### Why the retrieval logic was not rewritten

`connectors/` is a byte-for-byte copy of the validated v1.0.2 connector packages. That code
already carries the retry wiring, per-attempt rate limiting, retraction/correction parsing,
and the failure-status taxonomy that took several releases to get right. Rewriting it would
have thrown away that validation for no gain. This project adds a transport and a
normalization layer on top — nothing more.

`connector_bridge.py` exists because each connector was written as a standalone CLI that
imports a top-level `errors` / `parser` / `models`. Loading three of them into one process
naively would let `pubmed/errors.py` and `clinical_trials/errors.py` fight over
`sys.modules["errors"]`, silently applying the wrong status taxonomy to the wrong connector.
The bridge loads each under a private namespace so that cannot happen.

### Zero dependencies

The server and all connectors are pure standard library. Python 3.9+ runs it with nothing
installed. The single production dependency is `gunicorn`, the WSGI server that fronts it.

---

## Safety properties

These are enforced in code and covered by tests.

**Identifiers are never fabricated.** A PMID, DOI or NCT ID appears in output only if an
upstream payload actually carried it. A record that arrives without its identifier is
dropped, never assigned one.

**A retrieval failure is never "no evidence".** `UPSTREAM_ERROR`, `TIMEOUT`, `PARSE_ERROR`
and `RATE_LIMITED` return `ok: false`, `results: null`, and an explicit `interpretation`
saying the result must not be read as an absence of evidence.

**`ZERO_RESULTS` is distinct from failure.** It means "this query matched nothing" — stated
in the response as explicitly *not* establishing that no relevant evidence exists.

**No overclaiming of coverage.** `search_systematic_reviews` reports
`cochrane_status: NOT IMPLEMENTED` (and the same for Embase and Scopus) on every response,
and its description forbids presenting it as a Cochrane search.

**Registry ≠ efficacy.** Every clinical-trial response and every individual trial record
carries the caveat that a registry record is not evidence an intervention works.

**Provenance on everything.** Every response and every record carries the source connector,
source database, the exact query sent upstream, the retrieval status and a UTC timestamp.

---

## Run locally

```bash
cd ~/Downloads/DENTAL-AI-RESEARCH-MCP
python3 server.py --port 8000
```

```bash
curl -s localhost:8000/health | python3 -m json.tool
```

```bash
curl -s -X POST localhost:8000/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -m json.tool
```

## Tests

```bash
python3 tests/test_mcp.py
```

96 assertions: protocol conformance, all four schemas, live retrieval against all three
upstreams, invalid-DOI handling, simulated upstream failure, identifier-fabrication guards,
input validation, health, and a source scan for hard-coded credentials.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/mcp` | MCP JSON-RPC (Streamable HTTP) |
| `GET` | `/mcp` | `405` — this server offers no server-initiated SSE stream (spec permits) |
| `DELETE` | `/mcp` | `200` — session teardown is a no-op; the server is stateless |
| `GET` | `/health` | Cheap upstream reachability probe — runs **no** searches |
| `GET` | `/` | Service descriptor |

The health check hits `einfo.fcgi` (PubMed), `/types` (Crossref) and `/version`
(ClinicalTrials.gov) — small metadata endpoints, so the probe cannot burn upstream rate
limits.

---

## Configuration

No configuration is required. All four functions use public APIs with no authentication.

Three optional environment variables improve upstream courtesy and limits. All are read
from the environment by the vendored connectors and are **never** hard-coded:

| Variable | Effect |
|---|---|
| `NCBI_API_KEY` | Raises the NCBI rate limit from 3/s to 10/s |
| `NCBI_EMAIL` | NCBI contact address for the polite pool |
| `CROSSREF_MAILTO` | Crossref polite-pool contact address |

There are no server secrets. There is no auth, no session store and no user data.

---

## Deployment

See `DEPLOY.md`. One recommended host, with the exact steps.
