"""One place that sends Google API requests and retries the ones worth retrying.

Google's Drive and Sheets APIs answer "slow down" in three ways: HTTP 429,
HTTP 403 with reason rateLimitExceeded / userRateLimitExceeded, and the odd
5xx. Those are retried with exponential backoff plus jitter (and Retry-After
when Google sends it). Every other error is raised straight away as a
GoogleApiError carrying Google's own reason, so the caller can tell
"not shared with the service account" from "API not enabled".
"""
from __future__ import annotations

import random
import time
from typing import Callable, Optional

RETRY_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError"}


class GoogleApiError(RuntimeError):
    def __init__(self, status: int, reason: str, message: str, url: str = ""):
        super().__init__(f"Google API {status} {reason}: {message}")
        self.status = status
        self.reason = reason
        self.message = message
        self.url = url


def error_reason(resp) -> tuple[str, str]:
    """(reason, message) from a Google error body; tolerant of non-JSON bodies."""
    try:
        err = resp.json().get("error", {})
    except Exception:
        return "unknown", (getattr(resp, "text", "") or "")[:200]
    if isinstance(err, str):  # OAuth token endpoint style
        return err, str(resp.json().get("error_description", ""))
    reasons = [e.get("reason", "") for e in err.get("errors", []) if isinstance(e, dict)]
    reason = next((r for r in reasons if r), "") or str(err.get("status", "")) or "unknown"
    return reason, str(err.get("message", ""))


def should_retry(resp) -> bool:
    if resp.status_code == 429 or resp.status_code >= 500:
        return True
    if resp.status_code == 403:
        return error_reason(resp)[0] in RETRY_REASONS
    return False


class GoogleHttp:
    """Wraps any session with a requests-style .request() (google-auth's AuthorizedSession or the fake)."""

    def __init__(self, session, max_retries: int = 6, base_delay: float = 1.0, max_delay: float = 32.0,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 60.0,
                 rng: Optional[random.Random] = None):
        self.session = session
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.sleep = sleep
        self.timeout = timeout
        self.rng = rng or random.Random()
        self.retries = 0  # total retries, for the run report

    def request(self, method: str, url: str, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        for attempt in range(self.max_retries + 1):
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code < 400:
                return resp
            if attempt < self.max_retries and should_retry(resp):
                self.retries += 1
                self.sleep(self._delay(attempt, resp))
                continue
            reason, message = error_reason(resp)
            raise GoogleApiError(resp.status_code, reason, message, url)
        raise AssertionError("unreachable")

    def _delay(self, attempt: int, resp) -> float:
        retry_after = (getattr(resp, "headers", None) or {}).get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), self.max_delay)
            except ValueError:
                pass
        return min(self.base_delay * (2 ** attempt), self.max_delay) + self.rng.uniform(0, 1)
