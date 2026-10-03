"""Standalone check-in client (O5): reports CLI liveness to a local companion endpoint.

Run directly (``python3 client/checkin_client.py``), never imported as part of the
``penguincode_cli`` package -- this file predates it and has no callers elsewhere in the
repo. The only defect this module fixes is O5: the original ``requests.post(...)`` call
carried no timeout at all, so an unreachable/hung endpoint could block the calling process
forever. ``checkin()`` now always applies a bounded (connect, read) timeout, never raises
into its caller on any request failure, and logs a single masked WARNING instead.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

#: Default (connect, read) timeout in seconds, both env-overridable with a sane floor --
#: a caller running this as a background check-in must never block indefinitely on a dead
#: or slow endpoint (client.md Offline Mode & Connectivity: never silently hang).
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_TIMEOUT_SECONDS = 10.0


def _env_timeout(name: str, default: float) -> float:
    """Parse *name*'s env var as a positive float timeout, falling back to *default*.

    Never raises: unset, blank, non-numeric, or non-positive values all fall back to
    *default* -- a malformed timeout tunable must never crash the caller.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%r; using default %s", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s must be positive, got %s; using default %s", name, value, default)
        return default
    return value


def _timeout() -> tuple[float, float]:
    """Resolve the (connect, read) timeout pair from env, re-read on every call.

    Env vars: ``PENGUINCODE_CHECKIN_CONNECT_TIMEOUT_SECONDS`` /
    ``PENGUINCODE_CHECKIN_READ_TIMEOUT_SECONDS``.
    """
    return (
        _env_timeout(
            "PENGUINCODE_CHECKIN_CONNECT_TIMEOUT_SECONDS", _DEFAULT_CONNECT_TIMEOUT_SECONDS
        ),
        _env_timeout("PENGUINCODE_CHECKIN_READ_TIMEOUT_SECONDS", _DEFAULT_READ_TIMEOUT_SECONDS),
    )


def checkin(user_id: str) -> dict | None:
    """POST a liveness check-in for *user_id*; returns the parsed body, or ``None`` on any failure.

    Never raises -- a connect/read timeout, a connection refusal, or a non-200 response are
    all logged at WARNING (never the full URL/payload beyond the already-non-sensitive
    ``user_id``) and degrade to ``None``, exactly like a disabled check-in would. The caller
    (a background liveness ping) must never crash or hang because of this call.
    """
    url = os.environ.get("PENGUINCODE_CHECKIN_URL", "http://localhost:5000/checkin")
    data = {"user_id": user_id}
    try:
        # bandit B113 false positive: a timeout IS passed (`_timeout()`'s resolved
        # (connect, read) tuple) -- bandit's static check only recognizes a literal value.
        response = requests.post(url, json=data, timeout=_timeout())  # nosec B113
    except requests.exceptions.RequestException as exc:
        logger.warning("checkin request failed: %s", exc)
        return None

    if response.status_code != 200:
        logger.warning("checkin rejected (HTTP %d)", response.status_code)
        return None

    try:
        return response.json()
    except ValueError as exc:
        logger.warning("checkin response was not valid JSON: %s", exc)
        return None


if __name__ == "__main__":
    result = checkin("user123")
    print(result)
