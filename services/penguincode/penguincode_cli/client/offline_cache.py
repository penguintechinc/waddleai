"""Scope-safe local read cache for the CLI's knowledge-platform queries (O8 CLI resilience).

`KnowledgeClient.query()`/`index_status()` (and anything else wired in by
`core/repl.py`) go through the server for every read -- a transient outage
(`KnowledgeServerUnavailableError`) previously meant those commands simply failed with no
fallback. `OfflineCache` lets a caller stash the last successful result for a given read and
serve it back, clearly marked stale, the next time the same read is attempted while the
server is unreachable.

**Scope safety is the whole point of this module.** A cache keyed only by the query text
would happily hand tenant A's cached result to tenant B if both ran the same search --
exactly the cross-tenant leak `security.md` Tenant Isolation forbids. Every key is therefore
derived from the caller's *own* WaddleAI bearer token (`scope_key_from_token`, an unverified
local decode of the `tenant`/`sub` claims -- the same trust model `waddleai_auth._validate_claims`
already uses: the CLI trusts TLS + the server that issued the token, it does not re-verify
the signature client-side). When a token can't be decoded at all, caching is skipped
entirely (fail closed to "no cache", never "shared/unscoped cache").
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt

from penguincode_cli.flags.client import DISABLE_OFFLINE_CACHE_FLAG, SYSTEM_SCOPE
from penguincode_cli.flags.client import is_enabled as _flag_is_enabled

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = "~/.penguincode/cache"
_DEFAULT_TTL_SECONDS = 3600.0


def _cache_enabled() -> bool:
    """Whether the O8 offline read cache is active (opt-out kill-switch, process-wide).

    `penguincode.disable-offline-cache` unseen/OFF (the default) means the cache is ON;
    setting it ON reverts to the pre-O8 behavior -- every `get()` is a miss and every
    `set()` is a no-op, i.e. read commands always go straight to the server with no
    local fallback.
    """
    return not _flag_is_enabled(DISABLE_OFFLINE_CACHE_FLAG, SYSTEM_SCOPE)


def scope_key_from_token(token: str) -> str | None:
    """Derive a stable, non-reversible cache-scoping key from *token*'s claims.

    Decodes `tenant`/`sub` WITHOUT verifying the signature (mirrors
    `WaddleAITokenProvider._validate_claims`'s trust model: TLS + the issuing server are
    already trusted, this call never leaves the process) and hashes them together --
    never storing the raw tenant/user id on disk. Returns `None` (never raises) if the
    token can't be decoded or is missing the `tenant`/`sub` claims -- callers must treat
    `None` as "do not cache this", not "use a shared key".
    """
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        logger.debug("offline_cache: could not decode token for scoping: %s", exc)
        return None

    tenant = claims.get("tenant")
    user = claims.get("sub")
    if not tenant or not user:
        logger.debug("offline_cache: token missing tenant/sub claim; skipping cache")
        return None

    digest = hashlib.sha256(f"{tenant}:{user}".encode()).hexdigest()
    return digest[:32]


def _write_owner_only(path: Path, content: str) -> None:
    """Write *content* to *path* with owner-only (0600) permissions, atomically.

    Same atomic-create-with-final-mode technique as `waddleai_auth._write_owner_only` --
    duplicated locally rather than imported (a private helper of an unrelated module)
    since cached query results are exactly as sensitive as a token: both are
    tenant/user-scoped data that must never be group/world readable.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


@dataclass(slots=True, frozen=True)
class CacheEntry:
    """One cached read result, plus enough metadata to render a staleness notice."""

    value: dict[str, Any]
    stored_at: float
    is_stale: bool

    def age_seconds(self, *, now: float | None = None) -> float:
        """Seconds elapsed since this entry was stored (clock-injectable for tests)."""
        return (now if now is not None else time.time()) - self.stored_at


class OfflineCache:
    """TTL-aware, scope-keyed JSON file cache for one CLI session's read commands.

    One instance is cheap to construct per `KnowledgeClient`/REPL command; all state lives
    on disk under *cache_dir*, keyed by `scope_key_from_token` + a caller-supplied
    *namespace* (e.g. `"query"`, `"index_status"`) + *key* (e.g. the raw query string).
    """

    def __init__(
        self,
        *,
        cache_dir: str | None = None,
        ttl_seconds: float | None = None,
        clock: Any = time.time,
    ) -> None:
        """Bind this cache to *cache_dir* (default `~/.penguincode/cache`) and *ttl_seconds*
        (default 3600s). *clock* is a test seam only -- production code leaves it at
        `time.time`.
        """
        self._dir = Path(cache_dir or _DEFAULT_CACHE_DIR).expanduser()
        self._ttl_seconds = ttl_seconds if ttl_seconds is not None else _DEFAULT_TTL_SECONDS
        self._clock = clock

    def _entry_path(self, scope_key: str, namespace: str, key: str) -> Path:
        key_digest = hashlib.sha256(key.encode()).hexdigest()[:40]
        return self._dir / scope_key / namespace / f"{key_digest}.json"

    def get(self, *, token: str, namespace: str, key: str) -> CacheEntry | None:
        """Return the cached entry for (*token*'s scope, *namespace*, *key*), or `None`.

        `None` covers every "no usable cache" case uniformly: the kill-switch is ON, an
        undecodable token, no entry ever written, or a corrupt cache file -- never raises.
        """
        if not _cache_enabled():
            return None

        scope_key = scope_key_from_token(token)
        if scope_key is None:
            return None

        path = self._entry_path(scope_key, namespace, key)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            stored_at = float(data["stored_at"])
            value = data["value"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.debug("offline_cache: discarding unreadable entry %s: %s", path, exc)
            return None

        age = self._clock() - stored_at
        return CacheEntry(value=value, stored_at=stored_at, is_stale=age > self._ttl_seconds)

    def set(self, *, token: str, namespace: str, key: str, value: dict[str, Any]) -> None:
        """Store *value* for (*token*'s scope, *namespace*, *key*). No-op if the kill-switch
        is ON or the token is unscopeable.

        Never raises on a write failure (disk full, permissions) -- a cache write is
        best-effort; failing to cache must never break the read that just succeeded.
        """
        if not _cache_enabled():
            return

        scope_key = scope_key_from_token(token)
        if scope_key is None:
            return

        path = self._entry_path(scope_key, namespace, key)
        payload = json.dumps({"stored_at": self._clock(), "value": value})
        try:
            _write_owner_only(path, payload)
        except OSError as exc:
            logger.warning("offline_cache: failed to write cache entry %s: %s", path, exc)
