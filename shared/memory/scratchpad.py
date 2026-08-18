"""Session scratchpad store (§6A.1): Valkey hot / Postgres durable KV.

Isolation is a composite key on every operation -- (org_id, session_id,
user_id) -- so a caller can never read across any one of those three axes.
Writes route through ``filter_on_write``; quarantined values are stored
with ``status='quarantined'`` and are never returned by ``get``/``list``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from shared.memory.provenance import WriteVerdict, filter_on_write

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 86400  # 24h
DEFAULT_MAX_VALUE_KB = 256
DEFAULT_MAX_KEYS = 128


class ScratchpadError(Exception):
    """Base class for scratchpad abuse-limit errors."""


class ScratchpadValueTooLargeError(ScratchpadError):
    """Raised when a put() value exceeds max_value_kb."""


class ScratchpadKeyLimitExceededError(ScratchpadError):
    """Raised when a session has already reached max_keys distinct keys."""


@dataclass(slots=True)
class ScratchpadLimits:
    """Abuse limits for a single put() -- explicit errors, never silent truncation."""

    max_value_kb: int = DEFAULT_MAX_VALUE_KB
    max_keys: int = DEFAULT_MAX_KEYS


@dataclass(slots=True)
class PutResult:
    """Outcome of a scratchpad put()."""

    ok: bool
    quarantined: bool
    reasons: list = field(default_factory=list)


@dataclass(slots=True)
class ScratchpadKeyInfo:
    """Metadata returned by list() -- never the value itself."""

    key: str
    size_bytes: int
    updated_at: datetime


class ScratchpadStore:
    """Valkey-hot / Postgres-durable per-(org, session, user) KV store."""

    def __init__(
        self,
        valkey: Any,
        db: Any,
        scanner: Any,
        content_filter: Any,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        """Wire the Valkey/db tiers and the security tiers filter_on_write/get route through."""
        self.valkey = valkey
        self.db = db
        self.scanner = scanner
        self.content_filter = content_filter
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _valkey_key(org_id: int, session_id: str, user_id: int, key: str) -> str:
        return f"waddleai:sp:{org_id}:{session_id}:{user_id}:{key}"

    async def put(
        self,
        org_id: int,
        session_id: str,
        user_id: int,
        key: str,
        value: str,
        *,
        limits: ScratchpadLimits | None = None,
    ) -> PutResult:
        """Write a scratchpad value. Quarantines instead of raising on injection payloads."""
        limits = limits or ScratchpadLimits()

        size_bytes = len(value.encode("utf-8"))
        if size_bytes > limits.max_value_kb * 1024:
            raise ScratchpadValueTooLargeError(
                f"value is {size_bytes} bytes, exceeds max_value_kb={limits.max_value_kb}"
            )

        existing = await asyncio.to_thread(self._select_row, org_id, session_id, user_id, key)
        if existing is None:
            current_count = await asyncio.to_thread(
                self._count_active_keys, org_id, session_id, user_id
            )
            if current_count >= limits.max_keys:
                raise ScratchpadKeyLimitExceededError(
                    f"session already has {current_count} keys, exceeds max_keys={limits.max_keys}"
                )

        verdict: WriteVerdict = await filter_on_write(
            value,
            scanner=self.scanner,
            content_filter=self.content_filter,
            user_id=user_id,
            org_id=org_id,
        )
        status = "quarantined" if verdict.quarantine else "active"
        expires_at = datetime.now(UTC) + timedelta(seconds=self.ttl_seconds)

        await asyncio.to_thread(
            self._upsert_row,
            org_id,
            session_id,
            user_id,
            key,
            verdict.filtered_text,
            status,
            expires_at,
        )

        vkey = self._valkey_key(org_id, session_id, user_id, key)
        if status == "active":
            await self.valkey.set(vkey, verdict.filtered_text, ex=self.ttl_seconds)
        else:
            # Never cache quarantined content in the hot path.
            await self.valkey.delete(vkey)
            logger.warning(
                "ScratchpadStore.put: quarantined value for org=%s session=%s user=%s "
                "key=%s reasons=%s",
                org_id,
                session_id,
                user_id,
                key,
                verdict.reasons,
            )

        return PutResult(
            ok=(status == "active"), quarantined=(status == "quarantined"), reasons=verdict.reasons
        )

    async def get(self, org_id: int, session_id: str, user_id: int, key: str) -> str | None:
        """Read a scratchpad value. Valkey-first, falls through to Postgres and re-warms."""
        vkey = self._valkey_key(org_id, session_id, user_id, key)
        cached = await self.valkey.get(vkey)
        if cached is not None:
            return cached

        row = await asyncio.to_thread(self._select_row, org_id, session_id, user_id, key)
        if row is None:
            return None
        if row["status"] != "active":
            return None
        if row["expires_at"] is not None and row["expires_at"] < datetime.now(UTC):
            return None

        await self.valkey.set(vkey, row["value"], ex=self.ttl_seconds)
        return row["value"]

    async def list(self, org_id: int, session_id: str, user_id: int) -> list[ScratchpadKeyInfo]:
        """List key metadata (key, size, updated_at) -- never values."""
        rows = await asyncio.to_thread(self._select_active_rows, org_id, session_id, user_id)
        return [
            ScratchpadKeyInfo(
                key=row["key"],
                size_bytes=len(row["value"].encode("utf-8")),
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def delete(self, org_id: int, session_id: str, user_id: int, key: str) -> bool:
        """Delete a key from both tiers. Returns True if a row existed."""
        vkey = self._valkey_key(org_id, session_id, user_id, key)
        await self.valkey.delete(vkey)
        return await asyncio.to_thread(self._delete_row, org_id, session_id, user_id, key)

    # ------------------------------------------------------------------
    # Postgres access (raw SQL via the injected db handle, matching the
    # existing PgvectorMemoryStore.executesql convention). Wrapped in
    # asyncio.to_thread since the underlying call is blocking (§3.5).
    # ------------------------------------------------------------------

    def _select_row(self, org_id: int, session_id: str, user_id: int, key: str) -> dict | None:
        rows = self.db.executesql(
            "SELECT value, status, updated_at, expires_at FROM session_scratchpad "
            "WHERE org_id = %s AND session_id = %s AND user_id = %s AND key = %s",
            (org_id, session_id, user_id, key),
        )
        if not rows:
            return None
        value, status, updated_at, expires_at = rows[0]
        return {
            "value": value,
            "status": status,
            "updated_at": updated_at,
            "expires_at": expires_at,
        }

    def _select_active_rows(self, org_id: int, session_id: str, user_id: int) -> list[dict]:
        rows = self.db.executesql(
            "SELECT key, value, updated_at FROM session_scratchpad "
            "WHERE org_id = %s AND session_id = %s AND user_id = %s AND status = 'active' "
            "ORDER BY key",
            (org_id, session_id, user_id),
        )
        return [{"key": r[0], "value": r[1], "updated_at": r[2]} for r in rows]

    def _count_active_keys(self, org_id: int, session_id: str, user_id: int) -> int:
        rows = self.db.executesql(
            "SELECT COUNT(*) FROM session_scratchpad "
            "WHERE org_id = %s AND session_id = %s AND user_id = %s AND status = 'active'",
            (org_id, session_id, user_id),
        )
        return int(rows[0][0]) if rows else 0

    def _upsert_row(
        self,
        org_id: int,
        session_id: str,
        user_id: int,
        key: str,
        value: str,
        status: str,
        expires_at: datetime,
    ) -> None:
        self.db.executesql(
            "INSERT INTO session_scratchpad "
            "(org_id, session_id, user_id, key, value, status, author_user_id, scope_type, "
            "trust_tier, expires_at, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'session', 'unverified', %s, now(), now()) "
            "ON CONFLICT (org_id, session_id, user_id, key) DO UPDATE SET "
            "value = EXCLUDED.value, status = EXCLUDED.status, expires_at = EXCLUDED.expires_at, "
            "updated_at = now(), version = session_scratchpad.version + 1",
            (org_id, session_id, user_id, key, value, status, user_id, expires_at),
        )

    def _delete_row(self, org_id: int, session_id: str, user_id: int, key: str) -> bool:
        rowcount = self.db.executesql(
            "DELETE FROM session_scratchpad "
            "WHERE org_id = %s AND session_id = %s AND user_id = %s AND key = %s",
            (org_id, session_id, user_id, key),
        )
        return bool(rowcount)
