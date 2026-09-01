"""
connectors/crossref/errors.py

Failure-state taxonomy for the Crossref connector (Phase 7), plus IDENTIFIER_MISMATCH
specific to this connector's dual-verification role (Phase 5).
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

ALL_STATUSES = {
    STATUS_SUCCESS, STATUS_ZERO_RESULTS, STATUS_RATE_LIMITED, STATUS_TIMEOUT,
    STATUS_AUTH_ERROR, STATUS_UPSTREAM_ERROR, STATUS_PARSE_ERROR, STATUS_NOT_CONNECTED,
    STATUS_IDENTIFIER_MISMATCH,
}


class CrossrefConnectorError(Exception):
    def __init__(self, status, message, http_status=None):
        assert status in ALL_STATUSES, f"Unknown status: {status}"
        self.status = status
        self.http_status = http_status
        super().__init__(message)


def classify_http_status(http_status):
    if http_status is None:
        return STATUS_TIMEOUT
    if 200 <= http_status < 300:
        return None
    if http_status == 404:
        return STATUS_ZERO_RESULTS  # DOI not found — a real, meaningful non-error result
    if http_status == 429:
        return STATUS_RATE_LIMITED
    if http_status in (401, 403):
        return STATUS_AUTH_ERROR
    if 500 <= http_status < 600:
        return STATUS_UPSTREAM_ERROR
    return STATUS_UPSTREAM_ERROR
