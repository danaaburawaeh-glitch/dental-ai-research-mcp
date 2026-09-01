"""
connectors/clinical_trials/rate_limit.py

Client-side rate limiting for ClinicalTrials.gov API v2.

Why a self-imposed limit rather than a documented one: the live service returns NO rate-limit
headers at all (no X-RateLimit-*, no Retry-After; `server: istio-envoy`) and no public quota was
confirmed during verification — see CLINICALTRIALS_API_V2_VERIFICATION.md §10. Assuming a
generous allowance because none was advertised would be exactly the kind of unverified assumption
this codebase avoids elsewhere. A conservative fixed spacing is applied instead.

Mirrors the structure of pubmed/rate_limit.py and crossref/rate_limit.py deliberately: same
acquire() contract, so client.py's retry path treats all three connectors identically.
"""
import os
import time

# Conservative default: 3 requests/second. Chosen to match the unauthenticated NCBI tier already
# used elsewhere in this package rather than invented independently.
DEFAULT_REQUESTS_PER_SECOND = 3.0
ENV_VAR_RPS = "CLINICALTRIALS_REQUESTS_PER_SECOND"


class RateLimiter:
    """Minimum-interval limiter. Blocks in acquire() until the next request is permitted."""

    def __init__(self, requests_per_second=DEFAULT_REQUESTS_PER_SECOND, sleep=time.sleep,
                 clock=time.monotonic):
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be > 0")
        self.min_interval = 1.0 / requests_per_second
        self._sleep = sleep
        self._clock = clock
        self._last = None

    def acquire(self):
        now = self._clock()
        if self._last is not None:
            elapsed = now - self._last
            if elapsed < self.min_interval:
                self._sleep(self.min_interval - elapsed)
                now = self._clock()
        self._last = now


def build_rate_limiter_from_env(sleep=time.sleep, clock=time.monotonic):
    """
    Reads CLINICALTRIALS_REQUESTS_PER_SECOND if set; otherwise the conservative default.
    An unparseable or non-positive value falls back to the default rather than crashing or
    silently disabling rate limiting.
    """
    raw = os.environ.get(ENV_VAR_RPS)
    rps = DEFAULT_REQUESTS_PER_SECOND
    if raw:
        try:
            candidate = float(raw)
            if candidate > 0:
                rps = candidate
        except ValueError:
            pass
    return RateLimiter(requests_per_second=rps, sleep=sleep, clock=clock)
