"""Admin-action audit trail for the management API.

Audit gap G10: none of the state-changing admin endpoints (user/org/API-key/
provider/routing/quota CRUD -- 64 mutation routes across 16 files) wrote any
audit record. This module closes that gap with a *single* ``after_request``
middleware rather than editing 64 handlers: every ``POST``/``PUT``/``PATCH``/
``DELETE`` to ``/api/v1/*`` produces one ``audit_log`` row recording who
(user id -- never the raw username, per the PII-tokenization boundary), what
(method, path, resource id), when, and the outcome (HTTP status).

Request bodies are never read or stored: they can carry secrets and PII, and
the path + status already answer "what changed and did it succeed".
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from quart import g, request

if TYPE_CHECKING:  # pragma: no cover - typing only
    from quart import Quart, Response

logger = logging.getLogger(__name__)

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_API_PREFIX = "/api/v1/"


@dataclass(slots=True, frozen=True)
class AuditRecord:
    """One admin-mutation audit entry: who / what / when / outcome.

    ``user_id`` references the ``users`` identity table by id only -- the raw
    username never enters this record, keeping the PII boundary intact.
    """

    method: str
    path: str
    status_code: int
    user_id: int | None
    organization_id: int | None
    resource_id: str | None


def _resource_id_from_path(path: str) -> str | None:
    """Best-effort trailing resource id from an ``/api/v1/<collection>/<id>`` path."""
    suffix = path[len(_API_PREFIX) :].strip("/")
    parts = [p for p in suffix.split("/") if p]
    # A bare collection ("users") has no target id; "users/123" does.
    return parts[-1] if len(parts) >= 2 else None


def _build_record(status_code: int) -> AuditRecord | None:
    """Assemble an AuditRecord for the current request, or None if not auditable."""
    method = request.method.upper()
    path = request.path
    if method not in _MUTATING_METHODS or not path.startswith(_API_PREFIX):
        return None

    user: dict[str, Any] | None = getattr(g, "user", None)
    user_id = user.get("user_id") if isinstance(user, dict) else None
    org_id = user.get("organization_id") if isinstance(user, dict) else None

    return AuditRecord(
        method=method,
        path=path,
        status_code=status_code,
        user_id=user_id,
        organization_id=org_id,
        resource_id=_resource_id_from_path(path),
    )


def _write_audit_row(record: AuditRecord) -> None:
    """Insert one audit row via penguin-dal (synchronous; run off the event loop)."""
    from . import extensions

    db = extensions.db
    if db is None:  # pragma: no cover - DB always initialised in a served app
        return
    # penguin-dal's TableProxy.insert() commits its own session; the top-level
    # DB.commit() is a documented no-op, so no explicit commit is needed here.
    db.audit_log.insert(
        method=record.method,
        path=record.path,
        status_code=record.status_code,
        user_id=record.user_id,
        organization_id=record.organization_id,
        resource_id=record.resource_id,
        created_at=datetime.utcnow(),
    )


def register_audit_middleware(app: Quart) -> None:
    """Register the after-request hook that audits every /api/v1 mutation."""

    @app.after_request
    async def _audit(response: Response) -> Response:
        """Write exactly one audit row per mutating /api/v1 request; never raise."""
        try:
            record = _build_record(response.status_code)
            if record is not None:
                await asyncio.to_thread(_write_audit_row, record)
        except Exception as exc:
            # An audit-write failure must never turn a successful admin action
            # into a 500. Log and move on -- the missing row is the signal.
            logger.warning("audit-log write failed (non-fatal): %s", exc)
        return response
