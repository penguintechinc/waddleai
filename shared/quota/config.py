"""Quota config: DB loaders, JSON codec, and the Valkey read-through cache (spec §5, §6).

The proxy never queries config tables per request: :class:`QuotaConfigStore` reads a JSON blob
from Valkey, loading from the DB on a miss. Management deletes the blob on every config edit
(``services/management/app/services/quota_invalidation.py``); the TTL bounds staleness when
that delete cannot reach Valkey -- the teams ``auth_cache_invalidation`` pattern.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from shared.quota import keys
from shared.quota.types import OrgQuotaConfig, OverrideRule, TeamQuotaConfig, UserReservation

logger = logging.getLogger(__name__)


class QuotaConfigError(Exception):
    """The quota config could not be loaded from the database."""


def _opt_int(value: Any) -> int | None:
    """``None`` stays ``None``; anything else becomes ``int``."""
    return None if value is None else int(value)


def _as_date(value: Any) -> date | None:
    """Normalize a DB date value (date, datetime, ISO string, or None) to ``date | None``."""
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def load_org_config(db: Any, org_id: int) -> OrgQuotaConfig | None:
    """Load an org's pool switch, anchor and overrides; ``None`` when no pool row exists."""
    pool = db(db.quota_org_pool.org_id == org_id).select().first()
    if pool is None:
        return None
    rows = db(db.quota_override.organization_id == org_id).select(orderby=db.quota_override.id)
    overrides = tuple(
        OverrideRule(
            id=int(r.id),
            cap_credits=int(r.cap_credits),
            user_id=_opt_int(r.user_id),
            model=r.model or None,
            provider=r.provider or None,
        )
        for r in rows
    )
    return OrgQuotaConfig(
        org_id=org_id,
        enabled=bool(pool.enabled),
        cycle_anchor_date=_as_date(pool.cycle_anchor_date),
        pool_credits=int(pool.pool_credits or 0),
        overrides=overrides,
    )


def load_team_config(db: Any, team_id: int) -> TeamQuotaConfig | None:
    """Load a team's pool/buffer and per-user reservations; ``None`` when no pool row exists."""
    pool = db(db.quota_team_pool.team_id == team_id).select().first()
    if pool is None:
        return None
    rows = db(db.quota_user_reservation.team_id == team_id).select()
    reservations = {
        int(r.user_id): UserReservation(
            user_id=int(r.user_id),
            guaranteed_credits=int(r.guaranteed_credits or 0),
            daily_subcap=_opt_int(r.daily_subcap),
        )
        for r in rows
    }
    return TeamQuotaConfig(
        team_id=team_id,
        org_id=int(pool.organization_id),
        pool_credits=int(pool.pool_credits or 0),
        shared_buffer_credits=int(pool.shared_buffer_credits or 0),
        reservations=reservations,
    )


def org_config_to_json(cfg: OrgQuotaConfig) -> str:
    """Serialize an org config for the Valkey cache."""
    return json.dumps(
        {
            "org_id": cfg.org_id,
            "enabled": cfg.enabled,
            "anchor": cfg.cycle_anchor_date.isoformat() if cfg.cycle_anchor_date else None,
            "pool": cfg.pool_credits,
            "overrides": [
                {"id": o.id, "cap": o.cap_credits, "user_id": o.user_id,
                 "model": o.model, "provider": o.provider}
                for o in cfg.overrides
            ],
        }
    )  # fmt: skip


def org_config_from_json(raw: str) -> OrgQuotaConfig | None:
    """Inverse of :func:`org_config_to_json`; the literal ``null`` decodes to ``None``."""
    data = json.loads(raw)
    if data is None:
        return None
    return OrgQuotaConfig(
        org_id=int(data["org_id"]),
        enabled=bool(data["enabled"]),
        cycle_anchor_date=date.fromisoformat(data["anchor"]) if data["anchor"] else None,
        pool_credits=int(data["pool"]),
        overrides=tuple(
            OverrideRule(
                int(o["id"]), int(o["cap"]), _opt_int(o["user_id"]), o["model"], o["provider"]
            )
            for o in data["overrides"]
        ),
    )


def team_config_to_json(cfg: TeamQuotaConfig) -> str:
    """Serialize a team config for the Valkey cache."""
    return json.dumps(
        {
            "team_id": cfg.team_id,
            "org_id": cfg.org_id,
            "pool": cfg.pool_credits,
            "buffer": cfg.shared_buffer_credits,
            "reservations": [
                {"user_id": r.user_id, "guaranteed": r.guaranteed_credits, "daily": r.daily_subcap}
                for r in cfg.reservations.values()
            ],
        }
    )


def team_config_from_json(raw: str) -> TeamQuotaConfig | None:
    """Inverse of :func:`team_config_to_json`; the literal ``null`` decodes to ``None``."""
    data = json.loads(raw)
    if data is None:
        return None
    return TeamQuotaConfig(
        team_id=int(data["team_id"]),
        org_id=int(data["org_id"]),
        pool_credits=int(data["pool"]),
        shared_buffer_credits=int(data["buffer"]),
        reservations={
            int(r["user_id"]): UserReservation(
                int(r["user_id"]), int(r["guaranteed"]), _opt_int(r["daily"])
            )
            for r in data["reservations"]
        },
    )


class QuotaConfigStore:
    """Valkey read-through cache of org/team quota config (see module docstring)."""

    def __init__(self, valkey: Any, db: Any, ttl_s: int = 300, negative_ttl_s: int = 30) -> None:
        """Bind the (possibly ``None``) async Valkey client, the DAL handle and the TTLs."""
        self.valkey = valkey
        self.db = db
        self.ttl_s = ttl_s
        self.negative_ttl_s = negative_ttl_s

    async def get_org(self, org_id: int) -> OrgQuotaConfig | None:
        """Return the org's config (cached), or ``None`` when it has none."""
        return await self._get(
            keys.org_config(org_id), lambda: load_org_config(self.db, org_id),
            org_config_from_json, org_config_to_json,
        )  # fmt: skip

    async def get_team(self, team_id: int) -> TeamQuotaConfig | None:
        """Return the team's config (cached), or ``None`` when it has none."""
        return await self._get(
            keys.team_config(team_id), lambda: load_team_config(self.db, team_id),
            team_config_from_json, team_config_to_json,
        )  # fmt: skip

    async def _get(self, key: str, loader: Callable[[], Any], decode: Callable[[str], Any],
                   encode: Callable[[Any], str]) -> Any:  # fmt: skip
        """Cache hit -> decode; miss/outage/bad JSON -> DB load (thread) then best-effort SET."""
        if self.valkey is not None:
            try:
                cached = await self.valkey.get(key)
            except Exception as exc:  # noqa: BLE001 -- cache outage falls back to the DB
                logger.warning("quota config cache read failed (%s); using DB", exc)
                cached = None
            if cached is not None:
                try:
                    return decode(cached)
                except (ValueError, KeyError, TypeError) as exc:
                    logger.warning(
                        "quota config cache entry %s malformed (%s); reloading", key, exc
                    )
        try:
            value = await asyncio.to_thread(loader)
        except Exception as exc:  # noqa: BLE001 -- config DB down fails open as "unconfigured"
            logger.error(
                "quota config load failed for %s (%s: %s); failing open as unconfigured",
                key,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return None
        if self.valkey is not None:
            try:
                await self.valkey.set(
                    key, "null" if value is None else encode(value),
                    ex=self.negative_ttl_s if value is None else self.ttl_s,
                )  # fmt: skip
            except Exception as exc:  # noqa: BLE001 -- failing to cache must not fail the request
                logger.warning("quota config cache write failed (%s)", exc)
        return value
