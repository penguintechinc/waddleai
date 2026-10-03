"""PostHog-backed feature flags with graceful degradation.

Evaluation order:
  1. Environment override ``WADDLEAI_FLAG_<NAME>`` ("1"/"true"/"yes"/"on"
     enables; anything else disables) -- used by tests and alpha. Always
     wins, bypassing the cache and PostHog entirely.
  2. A TTL-fresh cached value (``FEATURE_FLAG_TTL_SECONDS``, default 30s) --
     no PostHog round trip is made while a resolved value is still fresh.
  3. A live PostHog lookup, bounded by ``FEATURE_FLAG_TIMEOUT_SECONDS``
     (default 3s) so a hung flag server can't block the caller.
  4. On any PostHog failure (an *outage*): the LAST-KNOWN cached value for
     this (flag, distinct_id), regardless of its TTL freshness -- a flag
     server outage degrades to "whatever we last knew" instead of snapping
     every caller straight to a hardcoded default.
  5. No last-known value exists (never resolved, i.e. never seen or always
     failed): the caller-supplied ``default`` (OFF for new flags, per house
     rules).

Never raises into the caller. Outage WARNings are rate-limited to once per
``FEATURE_FLAG_WARN_INTERVAL_SECONDS`` per flag (default 60s), not once per
call, so a sustained outage does not spam logs under load. The resolved-value
cache is bounded to ``FEATURE_FLAG_CACHE_MAX_ENTRIES`` (default 2048,
evicting the least-recently-used entry) so an unbounded set of distinct_ids
can never leak memory.

Every evaluation increments the ``feature_flag_evaluations_total`` OTel
counter, labelled by ``result`` only (live|cached|default|error) -- never by
flag key or value, keeping cardinality bounded and never leaking flag
identities or states into metric labels.

Kill switch: ``waddleai.disable-flag-degradation-cache`` (unseen/OFF, the
default, keeps this TTL-cache + last-known-value mechanism ON; ON reverts to
the pre-fix behaviour -- no cache, no last-known fallback, straight to
``default`` on any PostHog error). Resolved via its env override
(``WADDLEAI_FLAG_DISABLE_FLAG_DEGRADATION_CACHE``) only, deliberately not via
a live PostHog lookup: this flag gates the very PostHog-lookup/caching
mechanism ``is_feature_enabled`` implements, so resolving it *through*
PostHog would be circular and would double the PostHog round trips of every
single flag evaluation, defeating the TTL cache this kill switch exists to
roll back. Every other flag's test/alpha path already uses this same env
override, so operating this one the same way is consistent, not a special
case.
"""

import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from posthog import Posthog

logger = logging.getLogger(__name__)

_posthog_client: "Posthog | None" = None

_TRUTHY = ("1", "true", "yes", "on")

_DISABLE_DEGRADATION_FLAG = "waddleai.disable-flag-degradation-cache"

_DEFAULT_TTL_SECONDS = 30.0
_DEFAULT_TIMEOUT_SECONDS = 3.0
_DEFAULT_WARN_INTERVAL_SECONDS = 60.0
_DEFAULT_CACHE_MAX_ENTRIES = 2048


@dataclass(slots=True)
class _CacheEntry:
    """One resolved flag value and when it was resolved (monotonic seconds)."""

    value: bool
    resolved_at: float


_cache: "OrderedDict[tuple[str, str], _CacheEntry]" = OrderedDict()
_cache_lock = threading.Lock()

_last_warned: dict[str, float] = {}
_warn_lock = threading.Lock()

_evaluations_counter: Any = None


def _env_float(name: str, default: float) -> float:
    """Read a float-valued env var, falling back to ``default`` on absence/bad input."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default=%s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    """Read an int-valued env var, falling back to ``default`` on absence/bad input."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default=%s", name, raw, default)
        return default


def _ttl_seconds() -> float:
    return _env_float("FEATURE_FLAG_TTL_SECONDS", _DEFAULT_TTL_SECONDS)


def _timeout_seconds() -> float:
    return _env_float("FEATURE_FLAG_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS)


def _warn_interval_seconds() -> float:
    return _env_float("FEATURE_FLAG_WARN_INTERVAL_SECONDS", _DEFAULT_WARN_INTERVAL_SECONDS)


def _cache_max_entries() -> int:
    return _env_int("FEATURE_FLAG_CACHE_MAX_ENTRIES", _DEFAULT_CACHE_MAX_ENTRIES)


def _env_var_name(flag_key: str) -> str:
    """waddleai.memory-org-scope -> WADDLEAI_FLAG_MEMORY_ORG_SCOPE."""
    suffix = flag_key.split(".", 1)[-1]
    return "WADDLEAI_FLAG_" + suffix.replace("-", "_").replace(".", "_").upper()


def _get_posthog_client() -> "Posthog | None":
    """Lazily construct and cache the PostHog client (None if unconfigured)."""
    global _posthog_client
    api_key = os.getenv("POSTHOG_KEY")
    if not api_key:
        return None
    if _posthog_client is None:
        from posthog import Posthog

        _posthog_client = Posthog(
            api_key,
            host=os.getenv("POSTHOG_HOST", "https://license.penguintech.io"),
            feature_flags_request_timeout_seconds=_timeout_seconds(),
        )
    return _posthog_client


def _warn_once(rate_limit_key: str, message: str, *args: object) -> None:
    """Log a WARNING for ``rate_limit_key`` at most once per outage window."""
    now = time.monotonic()
    with _warn_lock:
        last = _last_warned.get(rate_limit_key)
        if last is not None and (now - last) < _warn_interval_seconds():
            return
        _last_warned[rate_limit_key] = now
    logger.warning(message, *args)


def _cache_get_fresh(key: tuple[str, str], now: float) -> _CacheEntry | None:
    """A cached entry, only if still within the TTL (skips the PostHog call)."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and (now - entry.resolved_at) < _ttl_seconds():
            _cache.move_to_end(key)
            return entry
    return None


def _cache_get_any(key: tuple[str, str]) -> _CacheEntry | None:
    """A cached entry regardless of TTL freshness -- the outage fallback."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None:
            _cache.move_to_end(key)
        return entry


def _cache_put(key: tuple[str, str], value: bool, now: float) -> None:
    """Record a freshly-resolved value, evicting the LRU entry once over budget."""
    with _cache_lock:
        _cache[key] = _CacheEntry(value, now)
        _cache.move_to_end(key)
        max_entries = _cache_max_entries()
        while len(_cache) > max_entries:
            _cache.popitem(last=False)


def _record_evaluation(result: str) -> None:
    """Increment ``feature_flag_evaluations_total{result=...}``. Never raises."""
    global _evaluations_counter
    try:
        if _evaluations_counter is None:
            from shared.observability.metrics import get_meter

            _evaluations_counter = get_meter().create_counter(
                "feature_flag_evaluations_total",
                unit="1",
                description=(
                    "Feature flag evaluations by result: live (fresh PostHog/env "
                    "answer), cached (TTL-fresh or outage last-known), default "
                    "(deliberately unconfigured/undefined), error (outage with no "
                    "last-known value)"
                ),
            )
        _evaluations_counter.add(1, {"result": result})
    except Exception as exc:  # noqa: BLE001 -- telemetry must never break flag eval
        logger.debug("feature flag telemetry emission failed: %s", exc)


def _is_degradation_disabled() -> bool:
    """Kill switch check, env-override only. See module docstring for why."""
    env_val = os.getenv(_env_var_name(_DISABLE_DEGRADATION_FLAG))
    if env_val is not None:
        return env_val.strip().lower() in _TRUTHY
    return False


def _legacy_is_feature_enabled(flag_key: str, distinct_id: str, default: bool) -> bool:
    """Pre-fix behaviour: no cache, no last-known fallback. Used only when the kill switch is ON."""
    env_val = os.getenv(_env_var_name(flag_key))
    if env_val is not None:
        return env_val.strip().lower() in _TRUTHY
    try:
        client = _get_posthog_client()
        if client is None:
            return default
        result = client.feature_enabled(flag_key, distinct_id)
        return default if result is None else bool(result)
    except Exception as exc:  # noqa: BLE001 -- legacy contract: swallow and default
        logger.warning(
            "Feature flag %s evaluation failed, using default=%s: %s", flag_key, default, exc
        )
        return default


def is_feature_enabled(flag_key: str, distinct_id: str = "server", default: bool = False) -> bool:
    """Evaluate a feature flag with TTL caching and outage degradation. Never raises.

    See the module docstring for the full evaluation order and the
    ``waddleai.disable-flag-degradation-cache`` kill switch.
    """
    if _is_degradation_disabled():
        return _legacy_is_feature_enabled(flag_key, distinct_id, default)

    did = distinct_id or "server"

    env_val = os.getenv(_env_var_name(flag_key))
    if env_val is not None:
        _record_evaluation("live")
        return env_val.strip().lower() in _TRUTHY

    key = (flag_key, did)
    now = time.monotonic()
    fresh = _cache_get_fresh(key, now)
    if fresh is not None:
        _record_evaluation("cached")
        return fresh.value

    client = _get_posthog_client()
    if client is None:
        _record_evaluation("default")
        return default

    try:
        result = client.feature_enabled(flag_key, did)
    except Exception as exc:  # noqa: BLE001 -- outage: configured but unreachable
        stale = _cache_get_any(key)
        if stale is not None:
            _warn_once(
                flag_key,
                "Feature flag %s lookup failed (PostHog outage): %s; using "
                "last-known cached value=%s for distinct_id=%s",
                flag_key,
                exc,
                stale.value,
                did,
            )
            _record_evaluation("cached")
            return stale.value
        _warn_once(
            flag_key,
            "Feature flag %s lookup failed (PostHog outage): %s; never cached, "
            "falling back to default=%s for distinct_id=%s",
            flag_key,
            exc,
            default,
            did,
        )
        _record_evaluation("error")
        return default

    if result is None:
        # No flag store entry for this key -- deliberate, not an outage.
        # Never-seen flags default OFF (house rule), per the caller's default.
        _record_evaluation("default")
        return default

    value = bool(result)
    _cache_put(key, value, now)
    _record_evaluation("live")
    return value


def reset_for_testing() -> None:
    """Drop the module-level client/cache/warn state so a test starts clean."""
    global _posthog_client, _evaluations_counter
    _posthog_client = None
    _evaluations_counter = None
    with _cache_lock:
        _cache.clear()
    with _warn_lock:
        _last_warned.clear()


__all__ = ["is_feature_enabled", "reset_for_testing"]
