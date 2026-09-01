"""
connectors/pubmed/errors.py

Failure-state taxonomy for the PubMed connector (Phase 7).
These map to gateway-level messages in CONNECTOR_FAILURE_MODEL.md — this module only
classifies; it does not decide what message reaches the user.
"""

STATUS_SUCCESS = "SUCCESS"
STATUS_ZERO_RESULTS = "ZERO_RESULTS"
STATUS_RATE_LIMITED = "RATE_LIMITED"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_AUTH_ERROR = "AUTH_ERROR"
STATUS_UPSTREAM_ERROR = "UPSTREAM_ERROR"
STATUS_PARSE_ERROR = "PARSE_ERROR"
STATUS_NOT_CONNECTED = "NOT_CONNECTED"

ALL_STATUSES = {
    STATUS_SUCCESS, STATUS_ZERO_RESULTS, STATUS_RATE_LIMITED, STATUS_TIMEOUT,
    STATUS_AUTH_ERROR, STATUS_UPSTREAM_ERROR, STATUS_PARSE_ERROR, STATUS_NOT_CONNECTED,
}


class PubMedConnectorError(Exception):
    """Base class. Always carries a status from the taxonomy above."""
    def __init__(self, status, message, http_status=None):
        assert status in ALL_STATUSES, f"Unknown status: {status}"
        self.status = status
        self.http_status = http_status
        super().__init__(message)


def classify_http_status(http_status):
    """Map a raw HTTP status code to our taxonomy. Returns None if it's a plain success (2xx)."""
    if http_status is None:
        return STATUS_TIMEOUT  # no response at all — treated as timeout upstream
    if 200 <= http_status < 300:
        return None
    if http_status == 429:
        return STATUS_RATE_LIMITED
    if http_status in (401, 403):
        return STATUS_AUTH_ERROR
    if 500 <= http_status < 600:
        return STATUS_UPSTREAM_ERROR
    return STATUS_UPSTREAM_ERROR  # conservative default for unexpected 4xx
