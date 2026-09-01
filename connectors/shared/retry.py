"""
connectors/shared/retry.py

Bounded exponential backoff with jitter, and Retry-After header awareness (Phase 8).
No network calls itself — wraps a caller-supplied request function.
"""
import time
import random


class RetryExhausted(Exception):
    def __init__(self, attempts, last_error):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"Retry exhausted after {attempts} attempts: {last_error}")


def with_backoff(fn, max_attempts=4, base_delay=1.0, max_delay=30.0,
                  retryable_statuses=(429, 500, 502, 503, 504), sleep_fn=time.sleep):
    """
    Call fn() (a zero-arg callable that performs one request attempt and returns a
    response-like object with .status_code and optional .headers), retrying on
    retryable_statuses with exponential backoff + jitter.

    Honors a Retry-After header (seconds, integer form only — HTTP-date form is not
    parsed here and falls back to computed backoff) when present on a 429/503 response.

    Raises RetryExhausted if max_attempts is reached without a non-retryable outcome.
    Does not retry on non-retryable statuses (e.g. 4xx other than 429) — those are
    returned immediately for the caller to classify (see errors.py).
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = fn()
        except Exception as exc:  # network-level failure (timeout, connection error)
            last_error = exc
            if attempt == max_attempts:
                raise RetryExhausted(attempt, exc)
            delay = _compute_delay(attempt, base_delay, max_delay)
            sleep_fn(delay)
            continue

        status = getattr(response, "status_code", None)
        if status is None or status not in retryable_statuses:
            return response  # success or non-retryable — caller classifies

        last_error = f"HTTP {status}"
        if attempt == max_attempts:
            return response  # let caller see the final failing response and classify it

        retry_after = _parse_retry_after(getattr(response, "headers", None))
        delay = retry_after if retry_after is not None else _compute_delay(attempt, base_delay, max_delay)
        sleep_fn(delay)

    raise RetryExhausted(max_attempts, last_error)


def _compute_delay(attempt, base_delay, max_delay):
    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
    jitter = random.uniform(0, delay * 0.25)
    return delay + jitter


def _parse_retry_after(headers):
    if not headers:
        return None
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None  # HTTP-date form not parsed; fall back to computed backoff
