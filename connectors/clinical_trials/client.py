"""
connectors/clinical_trials/client.py

ClinicalTrials.gov API v2 client. Implements clinical_trials_search and clinical_trials_fetch.

API v2 ONLY. The deprecated classic API (/api/query/*) is not referenced anywhere.

LIVE STATUS: this code has been executed successfully against the live API from this exact
packaged source on Claude Code / macOS (2026-08-31) — see LIVE_CLINICALTRIALS_VALIDATION.md.
RUNTIME CAVEAT: network availability is environment-dependent and must be checked at runtime by
reading the returned `status`, never assumed from this note. Some sandboxes block outbound hosts.

Invoked by a skill via the Bash tool, e.g.:
    python3 "${CLAUDE_PLUGIN_ROOT}/connectors/clinical_trials/client.py" search --condition "..."
Outputs a single JSON object to stdout. Non-zero exit on failure, with a JSON error object on
stdout per the errors.py status taxonomy.

Guarantees, matching the PubMed/Crossref contract exactly: the public functions NEVER raise.
Every path — retry exhaustion, HTTP error, parse failure, unexpected exception — is converted to
the failure-status JSON contract.
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
from shared.retry import with_backoff, RetryExhausted

from errors import (
    ClinicalTrialsConnectorError, classify_http_status,
    STATUS_SUCCESS, STATUS_ZERO_RESULTS, STATUS_TIMEOUT, STATUS_PARSE_ERROR,
    STATUS_UPSTREAM_ERROR, STATUS_IDENTIFIER_INVALID, STATUS_NOT_FOUND,
)
from rate_limit import build_rate_limiter_from_env
from models import (
    validate_nct_id, build_status_filter, build_phase_filter, STUDY_TYPES,
    NCT_NORMALIZATION_POLICY_NOTE,
)
from parser import parse_search_response, parse_study_response

BASE_URL = "https://clinicaltrials.gov/api/v2"
DEFAULT_TIMEOUT_SECONDS = 30
RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 4

# Verified live: pageSize above 1000 is silently clamped by the server (1001 -> 1000 studies,
# HTTP 200, no error). We clamp client-side so the caller's max_results is never quietly wrong.
MAX_PAGE_SIZE = 1000


class _Response:
    def __init__(self, status_code, text, headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _single_attempt(url, rate_limiter, timeout):
    """
    One HTTP attempt, rate-limited individually (each retry is a real request).
    HTTP-level outcomes return a _Response so with_backoff can inspect .status_code; network-level
    failures propagate so with_backoff's exception path handles them.

    Note: ClinicalTrials.gov returns PLAIN TEXT error bodies, not JSON (verified). The body is
    captured as text and never JSON-decoded on an error path.
    """
    rate_limiter.acquire()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return _Response(resp.status, resp.read().decode("utf-8"), dict(resp.headers))
    except urllib.error.HTTPError as e:
        return _Response(e.code, e.read().decode("utf-8", errors="replace"), dict(e.headers or {}))


def _http_get(url, rate_limiter, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Bounded retry on 429/5xx and on network-level exceptions. Never leaks a raw exception."""
    try:
        return with_backoff(
            lambda: _single_attempt(url, rate_limiter, timeout),
            max_attempts=MAX_ATTEMPTS,
            retryable_statuses=RETRYABLE_STATUSES,
        )
    except RetryExhausted as exc:
        raise ClinicalTrialsConnectorError(
            STATUS_TIMEOUT,
            f"Network error reaching ClinicalTrials.gov after {exc.attempts} attempts: {exc.last_error}",
        )


def _error_result(status, message, query, response=None, extra=None):
    result = {
        "status": status,
        "error": message,
        "provenance": build_provenance(
            "clinical_trials", "ClinicalTrials.gov", str(query), status).to_dict(),
    }
    if response is not None:
        result["http_status"] = response.status_code
        # Plain-text error body, surfaced verbatim and truncated — never parsed as JSON.
        result["upstream_message"] = (response.text or "").strip()[:400]
    if extra:
        result.update(extra)
    return result


def clinical_trials_search(condition=None, intervention=None, keywords=None,
                            recruitment_status=None, study_type=None, phase=None,
                            sponsor=None, location=None, max_results=20,
                            page_token=None, rate_limiter=None):
    """
    Search the registry. Returns:
        {status, records, total_count, next_page_token, executed_query, provenance}

    Only parameters verified to be accepted by the live API are ever sent — the API rejects an
    unknown parameter with HTTP 400 (verified), so passing an unverified one would break the whole
    query rather than being ignored.

    ZERO_RESULTS means "this query matched no registry records" — NOT "no such trials exist".

    NEVER raises.
    """
    rate_limiter = rate_limiter or build_rate_limiter_from_env()

    params = {}
    if condition:
        params["query.cond"] = condition
    if intervention:
        params["query.intr"] = intervention
    if keywords:
        params["query.term"] = keywords
    if sponsor:
        params["query.spons"] = sponsor
    if location:
        params["query.locn"] = location

    status_filter = build_status_filter(recruitment_status)
    if status_filter:
        params["filter.overallStatus"] = status_filter

    advanced = []
    phase_filter = build_phase_filter(phase)
    if phase_filter:
        advanced.append(phase_filter)
    if study_type:
        st = str(study_type).strip().upper()
        # Unrecognised study types are dropped rather than sent (HTTP 400 risk), same rule as
        # build_status_filter.
        if st in STUDY_TYPES:
            advanced.append(f"AREA[StudyType]{st}")
    if advanced:
        params["filter.advanced"] = " AND ".join(advanced)

    if not params:
        return {
            "status": STATUS_UPSTREAM_ERROR,
            "error": "No search criteria supplied — refusing to issue an unbounded registry query.",
            "provenance": build_provenance("clinical_trials", "ClinicalTrials.gov", "",
                                            STATUS_UPSTREAM_ERROR).to_dict(),
        }

    page_size = max(1, min(int(max_results or 20), MAX_PAGE_SIZE))
    params["pageSize"] = str(page_size)
    params["countTotal"] = "true"
    if page_token:
        params["pageToken"] = page_token

    url = f"{BASE_URL}/studies?" + urllib.parse.urlencode(params)
    executed_query = urllib.parse.urlencode(params)

    try:
        response = _http_get(url, rate_limiter)
    except ClinicalTrialsConnectorError as exc:
        return _error_result(exc.status, str(exc), executed_query)
    except Exception as exc:
        return _error_result(STATUS_UPSTREAM_ERROR, f"Unexpected error during search: {exc}",
                              executed_query)

    error_status = classify_http_status(response.status_code)
    if error_status:
        return _error_result(error_status,
                              f"ClinicalTrials.gov HTTP {response.status_code}",
                              executed_query, response)

    retrieved_at = now_iso()
    try:
        records, total_count, next_token = parse_search_response(
            response.text, query=executed_query, retrieved_at=retrieved_at,
            retrieval_status=STATUS_SUCCESS)
    except ClinicalTrialsConnectorError as exc:
        return _error_result(exc.status, str(exc), executed_query, response)
    except Exception as exc:
        return _error_result(STATUS_PARSE_ERROR, f"Unexpected search parse error: {exc}",
                              executed_query, response)

    status = STATUS_ZERO_RESULTS if not records else STATUS_SUCCESS
    for r in records:
        r["retrieval_status"] = status

    result = {
        "status": status,
        "records": records,
        "total_count": total_count,
        "next_page_token": next_token,
        "executed_query": executed_query,
        "provenance": build_provenance("clinical_trials", "ClinicalTrials.gov", executed_query,
                                        status).to_dict(),
    }
    if status == STATUS_ZERO_RESULTS:
        from errors import ZERO_RESULTS_MEANING
        result["zero_results_meaning"] = ZERO_RESULTS_MEANING
    return result


def clinical_trials_fetch(nct_id, rate_limiter=None):
    """
    Fetch the complete structured registry record for one NCT ID.

    The ID is VALIDATED before any request is issued. An invalid ID returns IDENTIFIER_INVALID and
    is never sent, never repaired, and never normalized into a valid-looking ID
    (models.NCT_NORMALIZATION_POLICY_NOTE).

    Also guards IDENTIFIER_MISMATCH: if the registry somehow returns a record whose nctId is not
    the one requested, that is reported, not silently accepted.

    NEVER raises.
    """
    rate_limiter = rate_limiter or build_rate_limiter_from_env()

    canonical = validate_nct_id(nct_id)
    if not canonical:
        return {
            "status": STATUS_IDENTIFIER_INVALID,
            "error": f"Not a valid NCT ID: {nct_id!r}. No request was issued.",
            "policy": NCT_NORMALIZATION_POLICY_NOTE,
            "provenance": build_provenance("clinical_trials", "ClinicalTrials.gov", str(nct_id),
                                            STATUS_IDENTIFIER_INVALID).to_dict(),
        }

    url = f"{BASE_URL}/studies/{urllib.parse.quote(canonical)}"

    try:
        response = _http_get(url, rate_limiter)
    except ClinicalTrialsConnectorError as exc:
        return _error_result(exc.status, str(exc), canonical)
    except Exception as exc:
        return _error_result(STATUS_UPSTREAM_ERROR, f"Unexpected error during fetch: {exc}",
                              canonical)

    error_status = classify_http_status(response.status_code)
    if error_status:
        msg = (f"No registry record for {canonical} (HTTP 404)."
               if error_status == STATUS_NOT_FOUND
               else f"ClinicalTrials.gov HTTP {response.status_code}")
        return _error_result(error_status, msg, canonical, response)

    retrieved_at = now_iso()
    try:
        record = parse_study_response(response.text, query=canonical, retrieved_at=retrieved_at,
                                       retrieval_status=STATUS_SUCCESS)
    except ClinicalTrialsConnectorError as exc:
        return _error_result(exc.status, str(exc), canonical, response)
    except Exception as exc:
        return _error_result(STATUS_PARSE_ERROR, f"Unexpected fetch parse error: {exc}",
                              canonical, response)

    if record.get("nct_id") != canonical:
        from errors import STATUS_IDENTIFIER_MISMATCH
        return _error_result(
            STATUS_IDENTIFIER_MISMATCH,
            f"Requested {canonical} but registry returned {record.get('nct_id')!r}",
            canonical, response)

    return {
        "status": STATUS_SUCCESS,
        "record": record,
        "provenance": build_provenance("clinical_trials", "ClinicalTrials.gov", canonical,
                                        STATUS_SUCCESS, pmid=None).to_dict() | {"nct_id": canonical},
    }


def _main():
    parser = argparse.ArgumentParser(description="ClinicalTrials.gov API v2 connector client")
    sub = parser.add_subparsers(dest="command", required=True)

    p_s = sub.add_parser("search")
    p_s.add_argument("--condition", default=None)
    p_s.add_argument("--intervention", default=None)
    p_s.add_argument("--keywords", default=None)
    p_s.add_argument("--status", dest="recruitment_status", default=None,
                     help="comma-separated overall statuses, e.g. RECRUITING,COMPLETED")
    p_s.add_argument("--study-type", default=None)
    p_s.add_argument("--phase", default=None, help="comma-separated, e.g. PHASE3,PHASE4")
    p_s.add_argument("--sponsor", default=None)
    p_s.add_argument("--location", default=None)
    p_s.add_argument("--max-results", type=int, default=20)
    p_s.add_argument("--page-token", default=None)

    p_f = sub.add_parser("fetch")
    p_f.add_argument("--nct-id", required=True)

    args = parser.parse_args()

    if args.command == "search":
        try:
            result = clinical_trials_search(
                condition=args.condition, intervention=args.intervention,
                keywords=args.keywords,
                recruitment_status=args.recruitment_status.split(",") if args.recruitment_status else None,
                study_type=args.study_type,
                phase=args.phase.split(",") if args.phase else None,
                sponsor=args.sponsor, location=args.location,
                max_results=args.max_results, page_token=args.page_token)
        except Exception as exc:
            result = _error_result(STATUS_UPSTREAM_ERROR, f"Unhandled error: {exc}", "search")
    elif args.command == "fetch":
        try:
            result = clinical_trials_fetch(args.nct_id)
        except Exception as exc:
            result = _error_result(STATUS_UPSTREAM_ERROR, f"Unhandled error: {exc}", args.nct_id)
    else:
        parser.error("unknown command")
        return

    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("status") in (STATUS_SUCCESS, STATUS_ZERO_RESULTS) else 1)


if __name__ == "__main__":
    _main()
