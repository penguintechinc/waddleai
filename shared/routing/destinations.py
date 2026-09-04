"""Destination resolution for provider failover (spec §5.2).

One parameterized executesql JOIN (model_destinations -> ai_providers ->
provider_credentials) filtered by org + model + enabled AND the §3.2 ownership
predicate; ordered by priority; TTL-cached (30 s) keyed (org_id, model). The
Destination value type NEVER carries the secret; the registry loads material
on demand via load_material().

Deviation from spec §3.2 (documented, orchestrator-adjudicated): reads go via
parameterized ``db.executesql`` (the ``shared/graph/resolver.py`` precedent),
not a proxy PyDAL mirror. Reason: the read must JOIN ``provider_credentials``,
which the proxy deliberately does not mirror into its PyDAL schema --
``executesql`` reads the live columns without ever mirroring a credential
field into PyDAL. This is *stronger* than the spec wording (no credential
columns enter the proxy schema at all) and matches the established
graph-resolver pattern.

Defense-in-depth (self-review, beyond the SQL predicate): the SQL WHERE
clause already excludes rows whose credential is owned by another org
(S2/S8). Because that predicate can only be exercised against a real
database, ``_resolve_all`` re-checks ownership in Python on every row it
maps and drops (and logs as a config defect) anything that still violates
it -- a bug in the SQL, a future refactor that loosens it, or a stub DB in
tests must never result in a cross-org credential reaching a caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_LOCAL_PROVIDERS = frozenset({"ollama", "llamacpp"})
_DEFAULT_TIMEOUT_SECONDS = 30

_RESOLVE_SQL = """
SELECT d.id, d.organization_id, d.model, d.priority, d.provider_id,
       p.provider_type, p.endpoint_url, p.extra_config,
       d.provider_model_id, d.region, d.timeout_seconds,
       d.credential_id, c.owner_org_id, c.updated_at
FROM model_destinations d
JOIN ai_providers p ON p.id = d.provider_id AND p.enabled = TRUE
LEFT JOIN provider_credentials c ON c.id = d.credential_id
WHERE d.organization_id = %s AND d.model = %s AND d.enabled = TRUE
  AND (c.id IS NULL OR c.owner_org_id IS NULL OR c.owner_org_id = d.organization_id)
ORDER BY d.priority ASC
"""  # nosec B608 -- fixed literal, org_id/model bound via executesql params  # noqa: S608

_MATERIAL_SQL = """
SELECT id, provider_id, owner_org_id, api_key, updated_at
FROM provider_credentials WHERE id = %s LIMIT 1
"""  # nosec B608 -- fixed literal, credential_id bound via executesql params  # noqa: S608


@dataclass(slots=True, frozen=True)
class Destination:
    """One resolved active/standby destination. NEVER carries the secret (spec §5.2)."""

    id: int
    organization_id: int
    model: str
    priority: int
    provider_id: int
    provider_type: str
    endpoint_url: str | None
    region: str | None
    provider_model_id: str | None
    timeout_seconds: int
    credential_id: int | None
    owner_org_id: int | None
    credential_version: str

    @property
    def role(self) -> str:
        """'active' for priority 0, else 'standby'."""
        return "active" if self.priority == 0 else "standby"


@dataclass(slots=True, frozen=True)
class CredentialMaterial:
    """Secret-bearing credential row loaded on demand by the registry only (never cached/logged)."""

    credential_id: int
    provider_id: int
    owner_org_id: int | None
    encrypted_material: str = field(repr=False, default="")
    updated_at: str | None = None


def _region_of(dest_region: object, extra_config: object) -> str | None:
    """Destination's own region if set, else fall back to the provider's extra_config region."""
    if isinstance(dest_region, str) and dest_region:
        return dest_region
    if isinstance(extra_config, str) and extra_config:
        try:
            extra_config = json.loads(extra_config)
        except ValueError:
            return None
    if isinstance(extra_config, dict):
        value = extra_config.get("region")
        return value if isinstance(value, str) else None
    return None


class DestinationResolver:
    """Reads and caches the ordered destination list for (org, logical model)."""

    def __init__(
        self, db: Any, *, ttl_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Bind the DB handle; configure the in-process TTL cache and injectable clock."""
        self._db = db
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[tuple[int, str], tuple[float, list[Destination]]] = {}

    async def resolve(
        self, org_id: int, model: str, *, pin: str | None = None, local_only: bool = False
    ) -> list[Destination]:
        """Ordered enabled destinations for (org, model), pin/local_only filtered (spec §5.2)."""
        dests = await self._resolve_all(org_id, model)
        if pin:
            dests = [d for d in dests if d.provider_type == pin]
        if local_only:
            dests = [d for d in dests if d.provider_type in _LOCAL_PROVIDERS]
        return dests

    async def _resolve_all(self, org_id: int, model: str) -> list[Destination]:
        """Cached, ownership-checked read of every enabled destination for (org_id, model)."""
        key = (org_id, model)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]

        def _read() -> list[tuple[Any, ...]]:
            return list(self._db.executesql(_RESOLVE_SQL, [org_id, model]))

        rows = await asyncio.to_thread(_read)
        dests = [
            Destination(
                id=r[0],
                organization_id=r[1],
                model=r[2],
                priority=r[3],
                provider_id=r[4],
                provider_type=r[5],
                endpoint_url=r[6],
                region=_region_of(r[9], r[7]),
                provider_model_id=r[8],
                timeout_seconds=r[10] if r[10] is not None else _DEFAULT_TIMEOUT_SECONDS,
                credential_id=r[11],
                owner_org_id=r[12],
                credential_version=str(r[13]) if r[13] is not None else "",
            )
            for r in rows
        ]
        dests = [d for d in dests if self._ownership_ok(d, org_id)]
        self._cache[key] = (now, dests)
        return dests

    @staticmethod
    def _ownership_ok(dest: Destination, org_id: int) -> bool:
        """S2/S8 defense-in-depth: drop and log any row the SQL predicate should have excluded.

        A platform-pool credential (``owner_org_id`` NULL) or no credential at
        all is always fine. Anything owned by a *different* org must never
        reach a caller -- this should be unreachable given the SQL WHERE
        clause, so its occurrence is logged as a config defect rather than
        silently trusted.
        """
        if dest.owner_org_id is None or dest.owner_org_id == org_id:
            return True
        logger.error(
            "config defect: destination %d (org %d, model %r) references credential %s "
            "owned by org %s -- excluded, never used",
            dest.id,
            org_id,
            dest.model,
            dest.credential_id,
            dest.owner_org_id,
        )
        return False

    async def load_material(self, credential_id: int) -> CredentialMaterial | None:
        """Load a credential's secret-bearing row for the registry (never cached here)."""

        def _read() -> tuple[Any, ...] | None:
            rows = self._db.executesql(_MATERIAL_SQL, [credential_id])
            return rows[0] if rows else None

        row = await asyncio.to_thread(_read)
        if row is None:
            return None
        return CredentialMaterial(
            credential_id=row[0],
            provider_id=row[1],
            owner_org_id=row[2],
            encrypted_material=row[3] or "",
            updated_at=str(row[4]) if row[4] is not None else None,
        )
