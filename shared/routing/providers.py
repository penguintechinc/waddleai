"""Single source of truth for which provider types serve from the local fleet.

``offers.py`` and the proxy's ``stages.py`` previously each carried their own literal; a new
local provider (vLLM) added to one but not the other made offers and dispatch disagree.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

__all__ = ["LOCAL_PROVIDERS", "foreign_only_models", "visible_model_configs"]

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


def _owner(row: Any) -> int | None:
    """``owner_org_id`` of a model_configs row; absent/non-int (shared) reads as None."""
    owner = getattr(row, "owner_org_id", None)
    return owner if isinstance(owner, int) and not isinstance(owner, bool) else None


def visible_model_configs(rows: list[Any], org_id: int | None) -> list[Any]:
    """The ``model_configs`` rows ``org_id`` may see: shared rows plus its own (AM3).

    A row owned by another org is never visible, so an org-owned local offer cannot reach, or
    shadow a commercial offer of the same name for, any OTHER org. For the owner itself, its own
    row wins over a shared row of the same model name. The columns are read defensively
    (``owner_org_id`` may not exist on an older schema: every row is then shared).
    """
    chosen: dict[str, Any] = {}
    order: list[str] = []
    for row in rows:
        owner = _owner(row)
        if owner is not None and (not org_id or owner != org_id):
            continue
        name = row.model_name
        if name not in chosen:
            order.append(name)
            chosen[name] = row
        elif owner is not None and _owner(chosen[name]) is None:
            chosen[name] = row  # the owner's own row beats the shared one
    return [chosen[name] for name in order]
