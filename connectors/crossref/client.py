"""
connectors/crossref/client.py

Crossref REST API client. Implements crossref_lookup_doi and crossref_search_bibliographic
(Phase 4).

LIVE STATUS (v0.4.5, corrected in v0.4.5.1): this code HAS been executed successfully against
the live network — a real `api.crossref.org/works/{doi}` request from this exact packaged code,
parsed successfully and cross-checked against the PubMed record by shared/citation_verifier.py
(result: VERIFIED), on Claude Code / macOS (2026-08-30). See "Live Validation Record" in
connector-capability-map.md. Earlier releases carried a warning here that only DOI resolution —
not the literal Crossref REST endpoint — had been confirmed; that warning is superseded.

RUNTIME CAVEAT — network availability is environment-dependent and must be checked at runtime,
never assumed from this note. Some sandboxes block outbound hosts. A blocked environment is
reported through the normal failure contract (see errors.py), so callers must read the returned
`status` field rather than presuming connectivity either way.

Scope is unchanged: metadata / citation verification only. Crossref does not provide full text —
see models.py CAPABILITY_LABEL_NOT_FULL_TEXT_NOTE.
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.provenance import build_provenance, now_iso
from shared.models import EvidenceRecord
from shared.retry import with_backoff, RetryExhausted

from errors import (
    CrossrefConnectorError, classify_http_status,
    STATUS_SUCCESS, STATUS_ZERO_RESULTS, STATUS_TIMEOUT, STATUS_PARSE_ERROR,
    STATUS_UPSTREAM_ERROR,
)
from rate_limit import build_single_record_rate_limiter, build_list_query_rate_limiter
from parser import parse_work_json, parse_bibliographic_search_json

BASE_URL = "https://api.crossref.org"
DEFAULT_TIMEOUT_SECONDS = 15
RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 4


def _polite_headers_and_params():
    mailto = os.environ.get("CROSSREF_MAILTO")
    params = {}
    headers = {"User-Agent": "dana_dental_evidence/0.4 (mailto:{})".format(mailto)} if mailto else {
        "User-Agent": "dana_dental_evidence/0.4"
    }
    if mailto:
        params["mailto"] = mailto
    return headers, params


class _Response:
    def __init__(self, status_code, text, headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _single_attempt(url, rate_limiter, headers, timeout):
    """
    One HTTP attempt, symmetric acquire/release of the rate limiter's concurrency slot
    around exactly this attempt (v0.4.1 fix — previously acquire/release wrapped the
    whole call including retries that didn't exist; now each real attempt gets its own
    rate-limit accounting, matching what actually reaches the network).
    """
    rate_limiter.acquire()
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _Response(resp.status, resp.read().decode("utf-8"), dict(resp.headers))
    except urllib.error.HTTPError as e:
        return _Response(e.code, e.read().decode("utf-8", errors="replace"), dict(e.headers or {}))
    finally:
        rate_limiter.release()


def _http_get(url, rate_limiter, headers=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    """
    Wires shared/retry.py's with_backoff into the real request path (v0.4.1 fix — same
    defect as pubmed/client.py: retry.py existed and was unit-tested standalone but was
    never actually called from here). Never lets a network-level exception escape —
    converts retry exhaustion into a CrossrefConnectorError(STATUS_TIMEOUT, ...).
    """
    try:
        return with_backoff(
            lambda: _single_attempt(url, rate_limiter, headers, timeout),
            max_attempts=MAX_ATTEMPTS,
            retryable_statuses=RETRYABLE_STATUSES,
        )
    except RetryExhausted as exc:
        raise CrossrefConnectorError(
            STATUS_TIMEOUT,
            f"Network error reaching Crossref after {exc.attempts} attempts: {exc.last_error}",
        )


def crossref_lookup_doi(doi):
    """
    Single-record DOI lookup. Returns dict: {status, record, provenance}.
    404 is treated as ZERO_RESULTS (a real, meaningful outcome — "this DOI is not in Crossref"),
    never silently reported as a parse or network failure.

    Guarantees (v0.4.1): NEVER raises — retry exhaustion, HTTP error, JSON parse failure, or
    any other unexpected exception is caught and converted to the failure-status JSON contract.
    """
    rate_limiter = build_single_record_rate_limiter()
    headers, params = _polite_headers_and_params()

    url = f"{BASE_URL}/works/{urllib.parse.quote(doi, safe='')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    try:
        response = _http_get(url, rate_limiter, headers=headers)
    except CrossrefConnectorError as exc:
        return _error_result(exc.status, str(exc), doi)
    except Exception as exc:
        return _error_result(STATUS_UPSTREAM_ERROR, f"Unexpected error during DOI lookup: {exc}", doi)

    error_status = classify_http_status(response.status_code)
    if error_status and error_status != STATUS_ZERO_RESULTS:
        return _error_result(error_status, f"Crossref HTTP {response.status_code}", doi)
    if error_status == STATUS_ZERO_RESULTS:
        return {
            "status": STATUS_ZERO_RESULTS,
            "record": None,
            "provenance": build_provenance("crossref", "crossref-works", doi, STATUS_ZERO_RESULTS,
                                            doi=doi).to_dict(),
        }

    try:
        record = parse_work_json(response.text)
    except CrossrefConnectorError as exc:
        return _error_result(exc.status, str(exc), doi)
    except Exception as exc:
        return _error_result(STATUS_PARSE_ERROR, f"Unexpected DOI-lookup parse error: {exc}", doi)

    record["retrieved_at"] = now_iso()
    record["query"] = doi
    evidence_record = EvidenceRecord.from_dict(record)

    return {
        "status": STATUS_SUCCESS,
        "record": evidence_record.to_dict(),
        "provenance": build_provenance("crossref", "crossref-works", doi, STATUS_SUCCESS,
                                        doi=doi).to_dict(),
    }


def crossref_search_bibliographic(citation_text, max_results=5):
    """
    List/query search via query.bibliographic. Returns dict: {status, candidates: [...]}.
    Never auto-selects a single best match — that judgment is Phase 5's, applied downstream.

    Guarantees (v0.4.1): NEVER raises — same contract as crossref_lookup_doi.
    """
    rate_limiter = build_list_query_rate_limiter()
    headers, params = _polite_headers_and_params()
    params["query.bibliographic"] = citation_text
    params["rows"] = str(max_results)

    url = f"{BASE_URL}/works?" + urllib.parse.urlencode(params)

    try:
        response = _http_get(url, rate_limiter, headers=headers)
    except CrossrefConnectorError as exc:
        return _error_result(exc.status, str(exc), citation_text)
    except Exception as exc:
        return _error_result(STATUS_UPSTREAM_ERROR, f"Unexpected error during bibliographic search: {exc}",
                              citation_text)

    error_status = classify_http_status(response.status_code)
    if error_status:
        return _error_result(error_status, f"Crossref HTTP {response.status_code}", citation_text)

    try:
        candidates = parse_bibliographic_search_json(response.text)
    except CrossrefConnectorError as exc:
        return _error_result(exc.status, str(exc), citation_text)
    except Exception as exc:
        return _error_result(STATUS_PARSE_ERROR, f"Unexpected bibliographic-search parse error: {exc}",
                              citation_text)

    retrieved_at = now_iso()
    records = []
    for c in candidates:
        c["retrieved_at"] = retrieved_at
        c["query"] = citation_text
        records.append(EvidenceRecord.from_dict(c).to_dict())

    status = STATUS_ZERO_RESULTS if not records else STATUS_SUCCESS
    return {"status": status, "candidates": records}


def _error_result(status, message, query):
    return {
        "status": status,
        "error": message,
        "provenance": build_provenance("crossref", "crossref-works", str(query), status).to_dict(),
    }


def _main():
    parser = argparse.ArgumentParser(description="Crossref REST API connector client")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doi = sub.add_parser("lookup-doi")
    p_doi.add_argument("--doi", required=True)

    p_bib = sub.add_parser("search-bibliographic")
    p_bib.add_argument("--citation", required=True)
    p_bib.add_argument("--max-results", type=int, default=5)

    args = parser.parse_args()

    if args.command == "lookup-doi":
        try:
            result = crossref_lookup_doi(args.doi)
        except Exception as exc:
            result = _error_result(STATUS_UPSTREAM_ERROR, f"Unhandled error: {exc}", args.doi)
    elif args.command == "search-bibliographic":
        try:
            result = crossref_search_bibliographic(args.citation, max_results=args.max_results)
        except Exception as exc:
            result = _error_result(STATUS_UPSTREAM_ERROR, f"Unhandled error: {exc}", args.citation)
    else:
        parser.error("unknown command")
        return

    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("status") in (STATUS_SUCCESS, STATUS_ZERO_RESULTS) else 1)


if __name__ == "__main__":
    _main()
