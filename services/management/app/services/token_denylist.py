"""Server-side JWT revocation denylist keyed on the token's ``jti`` claim.

Closes the 2026-09-14 audit finding that ``POST /auth/logout`` returned
success while doing nothing, leaving a stolen or logged-out bearer token
valid for its full lifetime with no kill switch.

Entries expire at the revoked token's own ``exp``, so the store self-cleans
and never grows beyond the set of tokens that are still live. Storage prefers
the shared cache (Valkey/Redis) via penguin-aaa's ``RedisTokenStore`` so a
logout on one replica is honoured by all of them, and falls back to a bounded
per-process denylist when no shared cache is configured.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class RevocationStore(Protocol):
    """The two-method slice of penguin-aaa's TokenStore this module needs.

    Declared structurally rather than importing ``TokenStore`` so any backend
    that can record and look up a revoked ``jti`` qualifies, without having to
    implement the refresh-token and nonce halves of that protocol.
    """

    def add_revoked_jti(self, jti: str, ttl: timedelta) -> None:
        """Record *jti* as revoked for *ttl*."""
        ...

    def is_jti_revoked(self, jti: str) -> bool:
        """Return True when *jti* has been recorded as revoked."""
        ...


_KEY_PREFIX = "waddleai:auth:"


@dataclass(slots=True, frozen=True)
class RevocationResult:
    """Outcome of a revocation attempt.

    ``durable`` distinguishes "recorded in the shared cache, every replica
    will honour it" from "recorded in this process only", which callers log
    so a silent downgrade of a security control is visible in the logs.
    """

    revoked: bool
    durable: bool


class _LocalDenylist:
    """Bounded, thread-safe in-process denylist of revoked ``jti`` values."""

    _MAX_ENTRIES: ClassVar[int] = 16384

    def __init__(self) -> None:
        """Create an empty denylist."""
        self._lock = threading.Lock()
        self._entries: dict[str, float] = {}

    def add(self, jti: str, expires_at_epoch: float) -> None:
        """Deny *jti* until *expires_at_epoch* (the token's own ``exp``)."""
        with self._lock:
            now = time.time()
            if len(self._entries) >= self._MAX_ENTRIES:
                for key in [k for k, exp in self._entries.items() if exp <= now]:
                    del self._entries[key]
            if len(self._entries) >= self._MAX_ENTRIES:
                # Still full of live entries: drop the soonest-to-expire to make
                # room. Evicting the entry that protects for the shortest
                # remaining time is the least-bad choice under pressure.
                oldest = min(self._entries, key=lambda k: self._entries[k])
                del self._entries[oldest]
            self._entries[jti] = expires_at_epoch

    def contains(self, jti: str) -> bool:
        """Return True while *jti* is denied and its entry has not expired."""
        with self._lock:
            expires_at = self._entries.get(jti)
            if expires_at is None:
                return False
            if expires_at <= time.time():
                del self._entries[jti]
                return False
            return True

    def clear(self) -> None:
        """Drop every entry (test helper)."""
        with self._lock:
            self._entries.clear()


@dataclass(slots=True)
class TokenDenylist:
    """Two-tier ``jti`` denylist: shared cache first, in-process fallback.

    Read-path degradation is deliberately *open* by default: a hard
    fail-closed read turns a cache outage into a total authentication outage
    for every user of the product, and the residual exposure is bounded by the
    one-hour token lifetime. Operators who would rather take the outage can
    set ``TOKEN_DENYLIST_FAIL_CLOSED=true``.
    """

    store_provider: Callable[[], RevocationStore | None] = field(default=lambda: None)
    fail_closed: bool = field(default_factory=lambda: _env_flag("TOKEN_DENYLIST_FAIL_CLOSED"))
    _local: _LocalDenylist = field(default_factory=_LocalDenylist, init=False)
    _degraded_logged: bool = field(default=False, init=False)

    def _store(self) -> RevocationStore | None:
        """Return the shared revocation store, or None when unavailable."""
        try:
            return self.store_provider()
        except Exception:  # pragma: no cover -- provider is a plain attribute read
            return None

    def _degrade(self, reason: str, exc: BaseException | None = None) -> None:
        """Log the first fall-back to the in-process denylist at WARNING."""
        if self._degraded_logged:
            return
        self._degraded_logged = True
        logger.warning(
            "token_denylist: shared revocation store unavailable (%s); "
            "revocations are per-process only until it recovers (fail_closed=%s)",
            reason,
            self.fail_closed,
            exc_info=exc is not None,
        )

    def revoke(self, jti: str, expires_at: datetime) -> RevocationResult:
        """Deny *jti* until *expires_at*, the revoked token's own expiry.

        Always records the revocation in-process first so the answer is never
        worse than "this replica will reject it", then mirrors it to the
        shared store when one is reachable.
        """
        if not jti:
            return RevocationResult(revoked=False, durable=False)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        ttl_seconds = int((expires_at - datetime.now(UTC)).total_seconds())
        if ttl_seconds <= 0:
            # Already expired: nothing to deny, and a non-positive TTL would be
            # rejected (or worse, stored forever) by the cache backend.
            return RevocationResult(revoked=True, durable=True)

        self._local.add(jti, expires_at.timestamp())

        store = self._store()
        if store is None:
            return RevocationResult(revoked=True, durable=False)
        try:
            store.add_revoked_jti(jti, timedelta(seconds=ttl_seconds))
        except Exception as exc:
            self._degrade("revocation write failed", exc)
            return RevocationResult(revoked=True, durable=False)
        return RevocationResult(revoked=True, durable=True)

    def is_revoked(self, jti: str | None) -> bool:
        """Return True when *jti* has been revoked and must be rejected.

        A missing ``jti`` is not revoked: API-key authentication carries no
        JWT ID, and refusing those here would break a working auth path.
        """
        if not jti:
            return False
        if self._local.contains(jti):
            return True
        store = self._store()
        if store is None:
            return False
        try:
            return bool(store.is_jti_revoked(jti))
        except Exception as exc:
            self._degrade("revocation lookup failed", exc)
            return self.fail_closed


def _env_flag(name: str) -> bool:
    """Return True when environment variable *name* holds a truthy value."""
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def _default_store_provider() -> RevocationStore | None:
    """Return a penguin-aaa RedisTokenStore over the process-wide cache client.

    Anything that is not an actual ``redis.Redis`` (an unconfigured ``None``,
    or a test double) is reported as "no shared store" so the caller uses the
    in-process denylist rather than issuing calls whose return types it cannot
    trust.
    """
    import redis
    from penguin_aaa.token_store.redis import RedisTokenStore

    from .. import extensions as ext

    client = ext.redis_client
    if isinstance(client, redis.Redis):
        return RedisTokenStore(client, prefix=_KEY_PREFIX)
    return None


_denylist: TokenDenylist | None = None
_denylist_lock = threading.Lock()


def get_token_denylist() -> TokenDenylist:
    """Return the process-wide token denylist, building it on first use."""
    global _denylist
    if _denylist is None:
        with _denylist_lock:
            if _denylist is None:
                _denylist = TokenDenylist(store_provider=_default_store_provider)
    return _denylist


def reset_token_denylist(denylist: TokenDenylist | None = None) -> None:
    """Replace the process-wide denylist; passing None rebuilds it from env.

    Exists so tests can install a denylist with a known backend without
    reaching into module internals.
    """
    global _denylist
    with _denylist_lock:
        _denylist = denylist
