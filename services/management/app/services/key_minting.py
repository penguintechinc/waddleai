"""Proxy-key issuance for the management plane (teams spec §7, D3/D10; closes issue #303).

The management ``POST /keys`` endpoint historically minted ``wa-{secret}`` into ``virtual_keys``
only -- a credential the proxy can never authenticate. This module is the one place that decides
which team a new key acts in and mints the proxy-parsable ``wa-{key_id}-{secret}`` credential.
"""

import logging
import secrets
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from passlib.hash import bcrypt
from penguin_dal.db import DB

from shared.auth.rbac import (
    Permission,
    access_level_for_role,
    expand_team_permissions,
    lookup_user_team,
    mint_wa_credential,
)
from shared.auth.team_flags import teams_enabled

logger = logging.getLogger(__name__)

#: A management-verified virtual key is matched by its first 11 characters (``wa-`` + 8) plus
#: ``...`` -- must stay equal to ``api.v1.auth._virtual_key_prefix`` (a test pins the pair).
_VIRTUAL_KEY_PREFIX_LEN = 11


@dataclass(slots=True)
class MintedKey:
    """A freshly minted proxy key: both row ids, the non-secret key_id, and the one-time secret."""

    virtual_key_id: int
    api_key_row_id: int
    key_id: str
    api_key: str
    key_prefix: str


def virtual_key_prefix(api_key: str) -> str:
    """Return the ``virtual_keys.key_prefix`` a presented ``api_key`` must match."""
    return f"{api_key[:_VIRTUAL_KEY_PREFIX_LEN]}..."


class KeyTeamError(Exception):
    """A requested key team is invalid or the caller may not use it; carries the HTTP status."""

    def __init__(self, message: str, status: int) -> None:
        """Record the user-facing message and the HTTP status to answer with."""
        super().__init__(message)
        self.message = message
        self.status = status


def _membership(database: DB, team_id: int, user_id: int):
    """Return the ``team_members`` row for (team, user), or ``None``."""
    return (
        database(
            (database.team_members.team_id == team_id) & (database.team_members.user_id == user_id)
        )
        .select()
        .first()
    )


def resolve_key_team(
    database: DB,
    *,
    org_id: int,
    target_user_id: int,
    requested_team_id: int | None,
    caller_user_id: int,
    caller_scopes: Collection[str],
) -> int | None:
    """Pick the acting team for a key being created for ``target_user_id`` in ``org_id``.

    No team requested: the key owner's own team in the tenant -- **required**: with teams
    enabled an owner in no team is a 409, so every new key acts in a team (users are seated
    on creation; an owner removed from their team must be re-added first). A team
    requested: it must exist in the tenant and be enabled; the CALLER must be allowed to issue
    into it (org admin via ``team:org_admin``; otherwise a live member holding ``team:key_create``
    for their own key or ``team:key_admin`` for someone else's); and the key OWNER must be a
    member, since the proxy would otherwise drop the team. Authorization is checked before the
    owner's membership so a non-member cannot probe who belongs to a team.

    Raises:
        KeyTeamError: 404 unknown/foreign team or teams disabled, 403 caller not authorized,
            409 team disabled or the key owner is in no team, 400 key owner not a member.

    """
    if not teams_enabled(str(org_id)):
        if requested_team_id is not None:
            raise KeyTeamError("Teams are disabled", 404)
        return None

    if requested_team_id is None:
        team_id, _role = lookup_user_team(database, target_user_id, org_id)
        if team_id is None:
            raise KeyTeamError(
                "Key owner is not in a team in this organization; add them to a team first",
                409,
            )
        return team_id

    team = database(database.teams.id == requested_team_id).select().first()
    if team is None or team.organization_id != org_id:
        raise KeyTeamError("Team not found", 404)
    if team.enabled is False:
        raise KeyTeamError("Team is disabled", 409)

    if Permission.TEAM_ORG_ADMIN.value not in caller_scopes:
        caller = _membership(database, requested_team_id, caller_user_id)
        needed = (
            Permission.TEAM_KEY_CREATE
            if target_user_id == caller_user_id
            else Permission.TEAM_KEY_ADMIN
        )
        if caller is None or needed not in expand_team_permissions(caller.team_role):
            raise KeyTeamError("Insufficient permissions to issue keys for this team", 403)

    if _membership(database, requested_team_id, target_user_id) is None:
        raise KeyTeamError("Key owner is not a member of that team", 400)
    return int(requested_team_id)


def _discard_orphan_api_key(database: DB, api_row_id: Any) -> None:
    """Best-effort removal of a half-minted ``api_keys`` row; never raises.

    penguin-dal runs every statement in its own ``engine.begin()`` transaction, so the failed
    insert is already rolled back by the engine (``DB`` has no ``rollback()``; ``__getattr__``
    would treat the name as a table) and only the committed first row needs deleting. The
    delete's own failure is swallowed so the caller's ORIGINAL exception propagates.
    """
    try:
        database(database.api_keys.id == api_row_id).delete()
        database.commit()
    except Exception:
        logger.exception("mint_proxy_key: failed to remove orphan api_keys row %s", api_row_id)


def mint_proxy_key(
    database: DB,
    *,
    user_id: int,
    org_id: int,
    name: str,
    owner_role: str,
    team_id: int | None,
    expires_at: datetime | None,
    allowed_models: list[str] | None,
    allowed_providers: list[str] | None,
    budget_limit_daily: float | None,
    budget_limit_monthly: float | None,
    tpm_limit: int | None,
    rpm_limit: int | None,
) -> MintedKey:
    """Mint one ``wa-{key_id}-{secret}`` credential into BOTH key tables (issue #303).

    ``api_keys`` is what the proxy authenticates (carries ``team_id``, the acting team);
    ``virtual_keys`` keeps the limits/metadata every other management endpoint reads, and links
    back via ``api_key_id`` so revoke/rotate can reach the proxy credential. The same
    plaintext is hashed once and stored in both rows -- mirroring the bootstrap admin key.
    ``enabled`` and ``created_at`` are written explicitly: a SQLAlchemy-built schema has no
    server-side defaults and auth filters on ``enabled == True``.

    There is no cross-table transaction in penguin-dal, so if the second insert fails the first
    row is deleted before the error propagates -- never a half-minted key.
    """
    key_id, api_key = mint_wa_credential()
    key_hash = bcrypt.hash(api_key)
    now = datetime.utcnow()

    api_row_id = database.api_keys.insert(
        key_id=key_id,
        key_hash=key_hash,
        user_id=user_id,
        organization_id=org_id,
        name=name,
        rate_limit_rpm=rpm_limit,
        enabled=True,
        expires_at=expires_at,
        created_at=now,
        api_access_level=access_level_for_role(owner_role),
        team_id=team_id,
    )
    prefix = virtual_key_prefix(api_key)
    try:
        virtual_row_id = database.virtual_keys.insert(
            user_id=user_id,
            organization_id=org_id,
            name=name,
            key_prefix=prefix,
            key_hash=key_hash,
            allowed_models=allowed_models,
            allowed_providers=allowed_providers,
            budget_limit_daily=budget_limit_daily,
            budget_limit_monthly=budget_limit_monthly,
            tpm_limit=tpm_limit,
            rpm_limit=rpm_limit,
            enabled=True,
            expires_at=expires_at,
            created_at=now,
            api_key_id=api_row_id,
        )
    except Exception:
        _discard_orphan_api_key(database, api_row_id)
        raise
    database.commit()
    return MintedKey(
        virtual_key_id=int(virtual_row_id),
        api_key_row_id=int(api_row_id),
        key_id=key_id,
        api_key=api_key,
        key_prefix=prefix,
    )


def mint_rotated_credential(database: DB, virtual_key: Any) -> tuple[str, str] | None:
    """New ``(api_key, key_prefix)`` for a LINKED virtual key, keeping its ``key_id``; else None.

    Rotation swaps only the secret half, so the proxy's ``key_id`` lookup and the virtual key's
    prefix stay stable. ``None`` means a legacy unlinked key: the caller keeps its old format.
    """
    if virtual_key.api_key_id is None:
        return None
    row = database(database.api_keys.id == virtual_key.api_key_id).select().first()
    if row is None:
        logger.error(
            "mint_rotated_credential: virtual key %d links to missing proxy-credential row %d",
            int(virtual_key.id),
            int(virtual_key.api_key_id),
        )
        return None
    api_key = f"wa-{row.key_id}-{secrets.token_urlsafe(32)}"
    return api_key, virtual_key_prefix(api_key)


def sync_linked_proxy_key(
    database: DB, virtual_key: Any, *, enabled: bool | None = None, key_hash: str | None = None
) -> str | None:
    """Mirror an ``enabled`` / ``key_hash`` change onto the linked ``api_keys`` row.

    Returns the proxy ``key_id`` whose auth-cache entry the caller must now invalidate, or
    ``None`` when the virtual key has no linked proxy credential (legacy) -- nothing to sync.
    """
    if virtual_key.api_key_id is None:
        return None
    row = database(database.api_keys.id == virtual_key.api_key_id).select().first()
    if row is None:
        logger.error(
            "sync_linked_proxy_key: virtual key %d links to missing proxy-credential row %d",
            int(virtual_key.id),
            int(virtual_key.api_key_id),
        )
        return None
    fields: dict[str, Any] = {}
    if enabled is not None:
        fields["enabled"] = enabled
    if key_hash is not None:
        fields["key_hash"] = key_hash
    if fields:
        database(database.api_keys.id == row.id).update(**fields)
        database.commit()
    return str(row.key_id)
