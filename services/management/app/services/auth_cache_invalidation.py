"""Invalidate the proxy's API-key auth cache when team membership changes (teams spec D7).

The proxy caches ``key_id -> {owner, role, resolved acting team}`` in Valkey
(``proxy/apps/proxy_server/auth_cache.py``) for up to ``PROXY_AUTH_CACHE_TTL_SECONDS``.
A team/membership/role change makes every cached entry for the affected users wrong, so the
write path deletes them -- best-effort: a missing or unreachable cache is logged and the proxy's
own TTL becomes the staleness bound, exactly like ``proxy_keys.py``'s revoke.
"""

import asyncio
import logging
from collections.abc import Iterable

from penguin_dal.db import DB

from shared.auth.rbac import auth_cache_key

from .. import extensions as _ext

logger = logging.getLogger(__name__)


async def invalidate_user_auth_cache(database: DB, user_ids: Iterable[int]) -> int:
    """Delete the proxy auth-cache entry of every API key owned by ``user_ids``.

    Returns how many keys were targeted (zero for an empty id list, in which case neither the
    DB nor the cache is touched). Never raises: invalidation failure must not fail or roll back
    the membership write that triggered it.
    """
    ids = sorted({int(u) for u in user_ids})
    if not ids:
        return 0

    def _key_ids() -> list[str]:
        rows = database(database.api_keys.user_id.belongs(ids)).select(database.api_keys.key_id)
        return [str(r.key_id) for r in rows]

    key_ids = await asyncio.to_thread(_key_ids)
    if not key_ids:
        return 0

    redis_client = getattr(_ext, "redis_client", None)
    if redis_client is None:
        logger.warning(
            "teams: no cache client configured; auth-cache invalidation for %d key(s) skipped "
            "(bounded by the proxy's cache TTL instead)",
            len(key_ids),
        )
        return len(key_ids)
    try:
        await asyncio.to_thread(redis_client.delete, *[auth_cache_key(k) for k in key_ids])
    except Exception as exc:  # noqa: BLE001 -- cache outage must not fail the membership write
        logger.warning("teams: failed to invalidate %d auth-cache key(s): %s", len(key_ids), exc)
    return len(key_ids)
