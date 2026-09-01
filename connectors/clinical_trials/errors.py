"""
connectors/clinical_trials/errors.py

Failure-state taxonomy for the ClinicalTrials.gov connector (Phase B).

Deliberately REUSES the existing taxonomy (see pubmed/errors.py, crossref/errors.py) rather than
inventing a parallel one — CONNECTOR_FAILURE_MODEL.md is the single vocabulary the gateway
understands. Two identifier-specific states are added because ClinicalTrials.gov makes a
distinction the other connectors do not:

- IDENTIFIER_MISMATCH (already in the shared taxonomy, used by Crossref) — the record returned
  does not correspond to the identifier requested.
- IDENTIFIER_INVALID (NEW in Phase B) — the NCT ID is malformed. This is NOT a network state and
  must never be reported as "no trials exist" or as an upstream failure. It is the only new
  status added; see CLINICAL_TRIALS_CONNECTOR_SPEC.md for why nothing else was needed.
"""

STATUS_SUCCESS = "SUCCESS"
STATUS_ZERO_RESULTS = "ZERO_RESULTS"
STATUS_RATE_LIMITED = "RATE_LIMITED"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_AUTH_ERROR = "AUTH_ERROR"
STATUS_UPSTREAM_ERROR = "UPSTREAM_ERROR"
STATUS_PARSE_ERROR = "PARSE_ERROR"
STATUS_NOT_CONNECTED = "NOT_CONNECTED"
STATUS_IDENTIFIER_MISMATCH = "IDENTIFIER_MISMATCH"
STATUS_IDENTIFIER_INVALID = "IDENTIFIER_INVALID"
STATUS_NOT_FOUND = "NOT_FOUND"

ALL_STATUSES = {
    STATUS_SUCCESS, STATUS_ZERO_RESULTS, STATUS_RATE_LIMITED, STATUS_TIMEOUT,
    STATUS_AUTH_ERROR, STATUS_UPSTREAM_ERROR, STATUS_PARSE_ERROR, STATUS_NOT_CONNECTED,
    STATUS_IDENTIFIER_MISMATCH, STATUS_IDENTIFIER_INVALID, STATUS_NOT_FOUND,
}

# ZERO_RESULTS semantics, stated here because the distinction is a safety rule, not a nicety:
ZERO_RESULTS_MEANING = (
    "The executed search returned zero matching registry records. This is NOT a statement that "
    "no such trials exist — only that this query, as executed, matched nothing."
)

# NOT_FOUND semantics — distinct from ZERO_RESULTS on purpose.
NOT_FOUND_MEANING = (
    "A well-formed NCT ID was requested and the registry has no such record (HTTP 404). Distinct "
    "from IDENTIFIER_INVALID (malformed ID, never sent) and from ZERO_RESULTS (a search matched "
    "nothing)."
)


class ClinicalTrialsConnectorError(Exception):
    """Base class. Always carries a status from the taxonomy above."""
    def __init__(self, status, message, http_status=None):
        assert status in ALL_STATUSES, f"Unknown status: {status}"
        self.status = status
        self.http_status = http_status
        super().__init__(message)


def classify_http_status(http_status):
    """
    Map a raw HTTP status code to the taxonomy. Returns None for a plain 2xx success.

    ClinicalTrials.gov specifics, verified live (CLINICALTRIALS_API_V2_VERIFICATION.md §4):
    - 404 means "well-formed NCT ID, no such record" -> NOT_FOUND, never UPSTREAM_ERROR.
    - 400 means the request itself was rejected (unknown parameter, bad enum, malformed id).
      Treated as UPSTREAM_ERROR because it signals a connector bug, not a user-facing absence.
    """
    if http_status is None:
        return STATUS_TIMEOUT
    if 200 <= http_status < 300:
        return None
    if http_status == 429:
        return STATUS_RATE_LIMITED
    if http_status in (401, 403):
        return STATUS_AUTH_ERROR
    if http_status == 404:
        return STATUS_NOT_FOUND
    if 500 <= http_status < 600:
        return STATUS_UPSTREAM_ERROR
    return STATUS_UPSTREAM_ERROR
