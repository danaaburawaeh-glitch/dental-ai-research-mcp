"""
connectors/pubmed/rate_limit.py

NCBI E-utilities rate limiting (Phase 8). Verified current limits:
- without NCBI_API_KEY: 3 requests/second
- with NCBI_API_KEY: 10 requests/second (default enhanced tier)
Source: NLM Support Center ("How do I obtain an API Key..." / API key articles), retrieved
2026-08-29 — see PUBMED_CONNECTOR_SPEC.md.

v0.4.1: replaced the v0.4 token-bucket implementation (which started with a full bucket and
therefore permitted an immediate burst of up to `rate` requests before throttling kicked in)
with a strict spacing/leaky-bucket limiter. This never permits more than one request per
(1/rate) seconds, from the very first call — no upfront burst allowance, matching the review
requirement for a "conservative limiter that does not exceed the configured request rate in a
rolling/spacing sense."
"""
import os
import time
import threading


class SpacingRateLimiter:
    """
    Strict-interval (leaky-bucket) limiter: enforces a minimum spacing of 1/rate seconds
    between the START of successive requests. Unlike a token bucket that begins full, this
    has no initial burst capacity — the second call ever made through this limiter still
    waits the full interval after the first, exactly like every call after it.
    """

    def __init__(self, rate_per_second, sleep_fn=time.sleep, clock=time.monotonic):
        self.rate = rate_per_second
        self.min_interval = 1.0 / rate_per_second
        self._next_allowed = None  # set on first acquire — no wait before the very first call
        self._lock = threading.Lock()
        self._sleep = sleep_fn
        self._clock = clock

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


# Backward-compatible alias — client.py and any external caller import this name.
TokenBucketRateLimiter = SpacingRateLimiter


def build_rate_limiter_from_env():
    """
    Returns a SpacingRateLimiter configured per whether NCBI_API_KEY is set.
    Never reads the key's value here beyond checking presence — the key itself is only
    read by client.py when constructing a request, per CONNECTOR_SECURITY.md.
    """
    has_key = bool(os.environ.get("NCBI_API_KEY"))
    rate = 10.0 if has_key else 3.0
    return SpacingRateLimiter(rate_per_second=rate)
