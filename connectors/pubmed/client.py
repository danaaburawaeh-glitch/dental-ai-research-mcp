"""
connectors/pubmed/client.py

PubMed / NCBI E-utilities client. Implements pubmed_search, pubmed_fetch,
pubmed_search_systematic_reviews, pubmed_search_clinical_studies (Phase 3).

LIVE STATUS (v0.4.5, corrected in v0.4.5.1): this code HAS been executed successfully against
the live network — real ESearch/EFetch requests to eutils.ncbi.nlm.nih.gov returning real PMIDs
and records, run from this exact packaged code on Claude Code / macOS (2026-08-30/31). See
"Live Validation Record" in connector-capability-map.md. Earlier releases carried a warning here
that the code had never been run live; that warning described the original build sandbox and was
superseded.

RUNTIME CAVEAT — network availability is environment-dependent and must be checked at runtime,
never assumed from this note. Some sandboxes block outbound hosts (the original build environment
returned HTTP 403 `x-deny-reason: host_not_allowed`). A blocked environment is reported through
the normal failure contract below (STATUS_TIMEOUT / STATUS_UPSTREAM_ERROR), so callers must read
the returned `status` field rather than presuming connectivity either way.

Invoked by a skill via the Bash tool, e.g.:
    python3 "${CLAUDE_PLUGIN_ROOT}/connectors/pubmed/client.py" search --query "..." --max-results 20
Outputs a single JSON object to stdout. Non-zero exit code on failure, with a JSON error object
on stdout describing the failure per the errors.py status taxonomy.
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # for shared/

from shared.provenance import build_provenance, now_iso
from shared.models import EvidenceRecord
from shared.retry import with_backoff, RetryExhausted

from errors import (
    PubMedConnectorError, classify_http_status,
    STATUS_SUCCESS, STATUS_ZERO_RESULTS, STATUS_TIMEOUT, STATUS_PARSE_ERROR,
    STATUS_UPSTREAM_ERROR,
)
from rate_limit import build_rate_limiter_from_env
from models import build_publication_type_filter, RCT_STUDY_DESIGN
from parser import parse_esearch_xml, parse_efetch_pubmed_xml, is_actually_systematic_review

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
DEFAULT_TIMEOUT_SECONDS = 15
RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 4


def _identification_params():
    """Builds tool/email/api_key params from environment. Never hard-coded (CONNECTOR_SECURITY.md)."""
    params = {
        "tool": os.environ.get("NCBI_TOOL", "dana_dental_evidence"),
    }
    email = os.environ.get("NCBI_EMAIL")
    if email:
        params["email"] = email
    api_key = os.environ.get("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key
    return params


class _Response:
    def __init__(self, status_code, text, headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _single_attempt(url, rate_limiter, timeout):
    """
    One HTTP attempt. Acquires the rate limiter for THIS attempt (each retry is a fresh
    real request and must be individually rate-limited, not just the first attempt).
    Returns a _Response for any HTTP-level outcome (including 429/5xx — with_backoff
    inspects .status_code to decide whether to retry). Network-level failures (no
    response at all — DNS, connection refused, timeout) are allowed to propagate as
    exceptions so with_backoff's exception-retry path handles them identically to a
    retryable HTTP status.
    """
    rate_limiter.acquire()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return _Response(resp.status, resp.read().decode("utf-8"), dict(resp.headers))
    except urllib.error.HTTPError as e:
        return _Response(e.code, e.read().decode("utf-8", errors="replace"), dict(e.headers or {}))


def _http_get(url, rate_limiter, timeout=DEFAULT_TIMEOUT_SECONDS):
    """
    Wires shared/retry.py's with_backoff into the real request path (v0.4.1 fix —
    previously this called urlopen directly with no retry at all, despite retry.py
    existing and being unit-tested in isolation). Bounded retry on 429/5xx and on
    network-level exceptions (timeout, connection failure). Never retries a permanent
    4xx (other than 429). Raises PubMedConnectorError(STATUS_TIMEOUT, ...) if retries
    are exhausted on a network-level failure — never lets a raw exception escape to
    the caller.
    """
    try:
        return with_backoff(
            lambda: _single_attempt(url, rate_limiter, timeout),
            max_attempts=MAX_ATTEMPTS,
            retryable_statuses=RETRYABLE_STATUSES,
        )
    except RetryExhausted as exc:
        raise PubMedConnectorError(
            STATUS_TIMEOUT,
            f"Network error reaching NCBI after {exc.attempts} attempts: {exc.last_error}",
        )


def pubmed_search(query, date_range=None, study_type=None, max_results=20, sort=None,
                   rate_limiter=None):
    """
    ESearch. Returns dict: {status, pmids, count, query_translation, raw_query, provenance}
    date_range: optional (mindate, maxdate) tuple, "YYYY" or "YYYY/MM/DD" strings.
    study_type: optional keyword mapped via models.build_publication_type_filter.

    Guarantees (v0.4.1): NEVER raises. Every code path — retry exhaustion, HTTP error,
    XML parse failure, or any other unexpected exception — is caught here and converted
    to the connector failure-status JSON contract. This is what a CLI caller depends on
    to always emit valid JSON, never a raw traceback.
    """
    rate_limiter = rate_limiter or build_rate_limiter_from_env()

    term = query
    filter_clause = build_publication_type_filter(study_type) if study_type else None
    if filter_clause:
        term = f"({term}) AND {filter_clause}"

    params = {
        "db": "pubmed",
        "term": term,
        "retmax": str(max_results),
        "retmode": "xml",  # xml chosen for reliable parsing per PUBMED_CONNECTOR_SPEC.md
    }
    if date_range:
        mindate, maxdate = date_range
        params["mindate"] = mindate
        params["maxdate"] = maxdate
        params["datetype"] = "pdat"
    if sort:
        params["sort"] = sort
    params.update(_identification_params())

    url = BASE_URL + "esearch.fcgi?" + urllib.parse.urlencode(params)

    try:
        response = _http_get(url, rate_limiter)
    except PubMedConnectorError as exc:
        return _error_result(exc.status, str(exc), query)
    except Exception as exc:  # last-resort guarantee: never leak a raw traceback
        return _error_result(STATUS_UPSTREAM_ERROR, f"Unexpected error during ESearch: {exc}", query)

    error_status = classify_http_status(response.status_code)
    if error_status:
        return _error_result(error_status, f"ESearch HTTP {response.status_code}", query, response)

    try:
        pmids, count, translation = parse_esearch_xml(response.text)
    except PubMedConnectorError as exc:
        return _error_result(exc.status, str(exc), query, response)
    except Exception as exc:
        return _error_result(STATUS_PARSE_ERROR, f"Unexpected ESearch parse error: {exc}", query, response)

    status = STATUS_ZERO_RESULTS if count == 0 else STATUS_SUCCESS
    return {
        "status": status,
        "pmids": pmids,
        "count": count,
        "query_translation": translation,
        "raw_query": term,
        "provenance": build_provenance(
            "pubmed", "pubmed", term, status
        ).to_dict(),
    }


def pubmed_fetch(pmids, rate_limiter=None):
    """
    EFetch + parse. pmids: str or list[str]. Returns dict: {status, records: [EvidenceRecord dict]}

    Guarantees (v0.4.1): NEVER raises — same contract as pubmed_search.
    """
    rate_limiter = rate_limiter or build_rate_limiter_from_env()
    if isinstance(pmids, str):
        pmids = [pmids]
    if not pmids:
        return {"status": STATUS_ZERO_RESULTS, "records": []}

    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
    }
    params.update(_identification_params())
    url = BASE_URL + "efetch.fcgi?" + urllib.parse.urlencode(params)

    try:
        response = _http_get(url, rate_limiter)
    except PubMedConnectorError as exc:
        return _error_result(exc.status, str(exc), pmids)
    except Exception as exc:
        return _error_result(STATUS_UPSTREAM_ERROR, f"Unexpected error during EFetch: {exc}", pmids)

    error_status = classify_http_status(response.status_code)
    if error_status:
        return _error_result(error_status, f"EFetch HTTP {response.status_code}", pmids, response)

    try:
        parsed = parse_efetch_pubmed_xml(response.text)
    except PubMedConnectorError as exc:
        return _error_result(exc.status, str(exc), pmids, response)
    except Exception as exc:
        return _error_result(STATUS_PARSE_ERROR, f"Unexpected EFetch parse error: {exc}", pmids, response)

    records = []
    retrieved_at = now_iso()
    for p in parsed:
        p["retrieved_at"] = retrieved_at
        p["query"] = ",".join(pmids)
        record = EvidenceRecord.from_dict(p)
        records.append(record.to_dict())

    status = STATUS_ZERO_RESULTS if not records else STATUS_SUCCESS
    return {"status": status, "records": records}


def pubmed_search_systematic_reviews(query, max_results=20, rate_limiter=None):
    """
    Search restricted to legitimate PubMed publication-type tags for systematic
    review/meta-analysis. Does NOT classify by title text — see is_actually_systematic_review
    for the enforcement point once records are fetched.
    """
    result = pubmed_search(query, study_type="systematic_review", max_results=max_results,
                            rate_limiter=rate_limiter)
    result["note"] = (
        "Filtered on PubMed's structured Publication Type field only. A title containing "
        "'systematic review' does not by itself satisfy this filter — see "
        "parser.is_actually_systematic_review, applied downstream after fetch."
    )
    return result


def pubmed_search_clinical_studies(query, designs=None, max_results=20, rate_limiter=None):
    """
    designs: list of keys from models.build_publication_type_filter, e.g.
    ["rct", "controlled_trial", "cohort"]. RCT here strictly means RCT_STUDY_DESIGN
    (randomized controlled trial) — never confused with root canal treatment; see
    models.RCT_DENTAL_PROCEDURE_DISAMBIGUATION_NOTE.
    """
    designs = designs or ["rct", "controlled_trial", "cohort"]
    clauses = [build_publication_type_filter(d) for d in designs]
    clauses = [c for c in clauses if c]
    combined_query = query
    if clauses:
        combined_query = f"({query}) AND ({' OR '.join(clauses)})"
    return pubmed_search(combined_query, max_results=max_results, rate_limiter=rate_limiter)


def _error_result(status, message, query, response=None):
    result = {
        "status": status,
        "error": message,
        "provenance": build_provenance("pubmed", "pubmed", str(query), status).to_dict(),
    }
    if response is not None:
        result["http_status"] = response.status_code
    return result


def _main():
    parser = argparse.ArgumentParser(description="PubMed/NCBI E-utilities connector client")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search")
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--max-results", type=int, default=20)
    p_search.add_argument("--study-type", default=None)
    p_search.add_argument("--mindate", default=None)
    p_search.add_argument("--maxdate", default=None)
    p_search.add_argument("--sort", default=None)

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--pmids", required=True, help="comma-separated PMIDs")

    p_sr = sub.add_parser("search-systematic-reviews")
    p_sr.add_argument("--query", required=True)
    p_sr.add_argument("--max-results", type=int, default=20)

    p_cs = sub.add_parser("search-clinical-studies")
    p_cs.add_argument("--query", required=True)
    p_cs.add_argument("--designs", default=None, help="comma-separated: rct,controlled_trial,cohort")
    p_cs.add_argument("--max-results", type=int, default=20)

    args = parser.parse_args()

    if args.command == "search":
        date_range = (args.mindate, args.maxdate) if args.mindate and args.maxdate else None
        try:
            result = pubmed_search(args.query, date_range=date_range, study_type=args.study_type,
                                    max_results=args.max_results, sort=args.sort)
        except Exception as exc:  # v0.4.1: CLI-level last resort — pubmed_search/fetch already
            result = _error_result(STATUS_UPSTREAM_ERROR, f"Unhandled error: {exc}", args.query)
    elif args.command == "fetch":
        try:
            result = pubmed_fetch(args.pmids.split(","))
        except Exception as exc:
            result = _error_result(STATUS_UPSTREAM_ERROR, f"Unhandled error: {exc}", args.pmids)
    elif args.command == "search-systematic-reviews":
        try:
            result = pubmed_search_systematic_reviews(args.query, max_results=args.max_results)
        except Exception as exc:
            result = _error_result(STATUS_UPSTREAM_ERROR, f"Unhandled error: {exc}", args.query)
    elif args.command == "search-clinical-studies":
        designs = args.designs.split(",") if args.designs else None
        try:
            result = pubmed_search_clinical_studies(args.query, designs=designs,
                                                      max_results=args.max_results)
        except Exception as exc:
            result = _error_result(STATUS_UPSTREAM_ERROR, f"Unhandled error: {exc}", args.query)
    else:
        parser.error("unknown command")
        return

    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("status") in (STATUS_SUCCESS, STATUS_ZERO_RESULTS) else 1)


if __name__ == "__main__":
    _main()
