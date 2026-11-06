"""Single source of truth for which provider types serve from the local fleet.

``offers.py`` and the proxy's ``stages.py`` previously each carried their own literal; a new
local provider (vLLM) added to one but not the other made offers and dispatch disagree.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

__all__ = ["LOCAL_PROVIDERS", "foreign_only_models"]

logger = logging.getLogger(__name__)

LOCAL_PROVIDERS: frozenset[str] = frozenset({"ollama", "llamacpp", "vllm"})


def _foreign_only(links: list[Any], org_id: int | None) -> set[str]:
    """Models listed ONLY by links owned by another org (AM3): never offered to ``org_id``."""
    visible: set[str] = set()
    foreign: set[str] = set()
    for link in links:
        owner = getattr(link, "owner_org_id", None)
        raw = getattr(link, "model_list", None)
        models = {m for m in raw if isinstance(m, str)} if isinstance(raw, list | tuple) else set()
        owned = isinstance(owner, int) and not isinstance(owner, bool)
        if not owned or (org_id is not None and owner == org_id):
            visible |= models
        else:
            foreign |= models
    return foreign - visible


async def foreign_only_models(db: Any, org_id: int | None) -> set[str]:
    """Models served exclusively by connection links owned by an org other than ``org_id``."""
    if not hasattr(db, "connection_links"):
        return set()
    try:
        links = await asyncio.to_thread(
            lambda: list(db(db.connection_links.enabled == True).select())  # noqa: E712
        )
    except Exception as exc:  # noqa: BLE001 -- offers are advisory; dispatch enforces isolation
        logger.warning("foreign-owner offer filter unavailable, offers unfiltered: %s", exc)
        return set()
    return _foreign_only(links, org_id)
