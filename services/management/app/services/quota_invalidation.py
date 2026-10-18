"""Invalidate the proxy's quota-config cache when an admin edits quota config (spec §6, D11).

The proxy reads org/team quota config through a Valkey read-through cache
(``shared.quota.config.QuotaConfigStore``, TTL 300s; a missing config is cached as the literal
``null`` under the SAME key for 30s). Every management write deletes the affected key after the
DB commit, which clears both the positive and negative entry -- best-effort: a missing or
unreachable cache is logged and the TTL becomes the staleness bound (like
``auth_cache_invalidation``).
Live consumption counters are never touched here.
"""

import asyncio
import logging

from shared.quota import keys

from .. import extensions as _ext

logger = logging.getLogger(__name__)


async def invalidate_quota_config(*, org_id: int | None = None, team_id: int | None = None) -> int:
    """Delete the org and/or team config key; return how many keys were targeted. Never raises."""
    targets: list[str] = []
    if org_id is not None:
        targets.append(keys.org_config(org_id))
    if team_id is not None:
        targets.append(keys.team_config(team_id))
    if not targets:
        return 0
    client = getattr(_ext, "redis_client", None)
    if client is None:
        logger.warning(
            "quota: no cache client configured; config invalidation for %d key(s) skipped "
            "(bounded by the config TTL instead)",
            len(targets),
        )
        return len(targets)
    try:
        await asyncio.to_thread(client.delete, *targets)
    except Exception as exc:  # noqa: BLE001 -- cache outage must not fail the config write
        logger.warning("quota: failed to invalidate %d config key(s): %s", len(targets), exc)
    return len(targets)
