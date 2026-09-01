# ClinicalTrials.gov connector (API v2)

Registry search and retrieval for `~~clinical-trials`. API v2 only — the deprecated classic API
(`/api/query/*`) is not used.

## Commands

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/connectors/clinical_trials/client.py" search \
    --condition "dental implants" --status RECRUITING --max-results 20

python3 "${CLAUDE_PLUGIN_ROOT}/connectors/clinical_trials/client.py" fetch \
    --nct-id NCT00782171
```

`search` accepts `--condition --intervention --keywords --status --study-type --phase --sponsor
--location --max-results --page-token`. Only parameters verified against the live API are ever
sent; the API rejects an unknown parameter with HTTP 400, so an unrecognised status or phase is
DROPPED rather than forwarded.

Both commands print one JSON object to stdout and exit non-zero on failure. Neither ever raises.

## The rules this connector enforces in code, not just prose

**A registry record is not evidence that anything works.** Every parsed record carries
`evidence_class` (A: registered only / B: registry results posted / C: linked publication) and a
`status_safety_note`. `COMPLETED` carries an explicit "means finished, not succeeded" warning;
`WITHDRAWN` states the trial never started and that withdrawal is not a negative result.

**Registry results are labelled, never upgraded.** When `hasResults` is true, the structured
results are captured under `registry_results` with a mandatory
`REGISTRY-REPORTED RESULTS — sponsor-submitted, NOT peer-reviewed` label. Nothing is calculated:
no significance, no effect size, no direction of benefit is derived from them.

**An NCT ID is validated, never repaired.** Case and surrounding whitespace are normalised because
neither changes which trial is designated. Everything else is rejected with `IDENTIFIER_INVALID`
and no request is issued — `NCT123` never becomes `NCT00000123`.

**A linkage needs an identifier.** `shared/trial_publication_linkage.py` returns
`LINK VERIFIED` only when the registry's own reference list names the PMID (type `RESULT` or
`DERIVED`) or the publication's metadata contains the NCT ID. A `BACKGROUND` reference does not
verify a link, and matching topic/title/authors never does.

**A trial and its publication are one study.** `deduplicate_trials_and_publications` returns
`independent_study_count`, which is the only figure a synthesis may cite.

## Status vocabulary

Reuses the shared taxonomy (`SUCCESS`, `ZERO_RESULTS`, `RATE_LIMITED`, `TIMEOUT`, `AUTH_ERROR`,
`UPSTREAM_ERROR`, `PARSE_ERROR`, `IDENTIFIER_MISMATCH`) plus two identifier states this registry
needs: `NOT_FOUND` (well-formed NCT ID, no such record — HTTP 404) and `IDENTIFIER_INVALID`
(malformed ID, never sent).

`ZERO_RESULTS` means *this query matched no registry records*. It is never a claim that no such
trials exist.

## Configuration

None required — no API key, no registration. `CLINICALTRIALS_REQUESTS_PER_SECOND` optionally
overrides the conservative 3 req/s client-side limit (the service publishes no rate-limit headers
and no confirmed quota, so the limiter is self-imposed).

## Tests

```bash
python3 connectors/clinical_trials/tests/test_clinical_trials.py    # 50 assertions, network mocked
```

Live validation against the real API: `docs/LIVE_CLINICALTRIALS_VALIDATION.md`.
API surface verification: `docs/CLINICALTRIALS_API_V2_VERIFICATION.md`.
