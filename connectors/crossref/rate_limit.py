"""
connectors/crossref/rate_limit.py

Crossref rate limiting (Phase 8). Verified CURRENT limits (2025-12-01 policy change,
confirmed via community.crossref.org announcement thread retrieved 2026-08-29):
    Public pool:  5 req/s,  concurrency 1
    Polite pool: 10 req/s,  concurrency 5   (polite = mailto param supplied)

Deliberately NOT using the commonly-cited but outdated "50 req/s" figure — see
CROSSREF_CONNECTOR_SPEC.md for the correction and its source.

Single-record (DOI lookup) and list/query requests get independent limiters as a conservative
internal design choice (Crossref's public docs don't split limits by endpoint shape — this
assumption is stated explicitly, not presented as confirmed Crossref policy).

v0.4.1: replaced the v0.4 token-bucket implementation (full bucket at start, permitting an
immediate burst) with a strict spacing/leaky-bucket limiter — same fix and same rationale as
pubmed/rate_limit.py. The concurrency semaphore (separate from the spacing) is unchanged.
"""
import os
import time
import threading


class SpacingRateLimiter:
    """
    Strict-interval (leaky-bucket) limiter for the request RATE, plus a separate concurrency
    semaphore bounding how many requests may be in flight at once. No upfront burst allowance —
    see pubmed/rate_limit.py's SpacingRateLimiter docstring for the same rationale.
    """

    def __init__(self, rate_per_second, concurrency_limit, sleep_fn=time.sleep, clock=time.monotonic):
        self.rate = rate_per_second
        self.min_interval = 1.0 / rate_per_second
        self._next_allowed = None
        self._lock = threading.Lock()
        self._sleep = sleep_fn
        self._clock = clock
        self._concurrency_sema = threading.Semaphore(concurrency_limit)

    def acquire(self):
        with self._lock:
            now = self._clock()
            if self._next_allowed is None or now >= self._next_allowed:
                wait = 0.0
                self._next_allowed = now + self.min_interval
            else:
                wait = self._next_allowed - now
                self._next_allowed = self._next_allowed + self.min_interval
        if wait > 0:
            self._sleep(wait)
        self._concurrency_sema.acquire()

    def release(self):
        self._concurrency_sema.release()


# Backward-compatible alias
TokenBucketRateLimiter = SpacingRateLimiter


def _is_polite():
    return bool(os.environ.get("CROSSREF_MAILTO"))


def build_single_record_rate_limiter():
    """DOI lookup (/works/{doi}) limiter."""
    if _is_polite():
        return SpacingRateLimiter(rate_per_second=10.0, concurrency_limit=5)
    return SpacingRateLimiter(rate_per_second=5.0, concurrency_limit=1)


def build_list_query_rate_limiter():
    """List/query (/works?query...) limiter — same published limits, separate instance
    for internal fairness (see module docstring)."""
    if _is_polite():
        return SpacingRateLimiter(rate_per_second=10.0, concurrency_limit=5)
    return SpacingRateLimiter(rate_per_second=5.0, concurrency_limit=1)
