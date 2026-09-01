"""§9.1 CodeRAG repo registration: ``/api/v1/code-repos`` CRUD + reindex + webhook.

Registers a git repository for CodeRAG indexing (``code_repos``), triggers
manual/cron re-indexing via ``CodeRagWorker``, and accepts GitHub/Gitea push
webhooks (HMAC-verified via a per-repo, Fernet-encrypted shared secret
generated at registration time and shown to the caller exactly once).

Every org-scoped route (list/get/delete/reindex) filters on the caller's
``org_id`` from the validated JWT at the query layer -- a repo id outside
the caller's org resolves to 404, identical to "doesn't exist", so no
route ever confirms or denies another org's repo names/ids (IDOR-safe).
The webhook route is the one unauthenticated route in this module by
design (external Git hosts cannot carry a WaddleAI JWT); it substitutes an
HMAC check against a per-repo secret and fails closed on any error path
(unknown repo, missing secret, undecryptable secret, bad signature) --
never falls through to indexing.

Flag: ``waddleai.coderag``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime
from typing import Any

from quart import g, jsonify, request

from shared.auth.rbac import Permission
from shared.security.credential_encryption import decrypt_credential, encrypt_credential

from ...extensions import db
from ...services.coderag_worker import create_coderag_worker
from . import api_v1_bp
from .auth import require_auth, require_scope
from .webhooks import verify_webhook_signature

logger = logging.getLogger(__name__)

_FLAG_KEY = "waddleai.coderag"

# git supports an "ext::<command>" transport that shells out to an arbitrary
# command (and similarly-dangerous "fd::"/"file://" local-path forms) --
# restricting registration to https:// and SSH (git@host:path or ssh://) is
# the boundary control against a caller using source_url as a git-clone RCE
# vector once CodeRagWorker._clone_or_pull() shells out to GitPython.
_ALLOWED_SOURCE_URL_PREFIXES = ("https://", "git@", "ssh://")


def _valid_source_url(url: str) -> bool:
    """Reject any git transport other than https:// or SSH (blocks ext::/file:// RCE)."""
    return url.startswith(_ALLOWED_SOURCE_URL_PREFIXES)


def _coderag_enabled(org_id: int) -> bool:
    """Fail-safe-OFF check of the ``waddleai.coderag`` flag (§14.5)."""
    try:
        from shared.utils.feature_flags import is_feature_enabled

        return is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id), default=False)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("coderag flag evaluation failed, treating as OFF: %s", exc)
        return False


def _serialize(row: Any) -> dict[str, Any]:
    """Explicit response schema -- never the raw ORM row, never webhook_secret/credentials_ref."""
    created_at = getattr(row, "created_at", None)
    updated_at = getattr(row, "updated_at", None)
    return {
        "id": row.id,
        "org_id": row.org_id,
        "name": row.name,
        "source_url": row.source_url,
        "index_status": row.index_status,
        "last_commit": row.last_commit,
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


@api_v1_bp.route("/code-repos", methods=["POST"])
@require_auth
@require_scope(Permission.CODE_REPO_WRITE)
async def create_code_repo() -> tuple[Any, int]:
    """Register a repo for CodeRAG indexing; returns its one-time webhook_secret."""
    org_id = g.user.get("organization_id")
    if not _coderag_enabled(org_id):
        return jsonify({"error": "coderag feature disabled"}), 404

    body = await request.get_json(silent=True) or {}
    name = body.get("name")
    source_url = body.get("source_url")
    if not name or not source_url:
        return jsonify({"error": "name and source_url are required"}), 400
    if not _valid_source_url(source_url):
        return jsonify({"error": "source_url must use https:// or an SSH git transport"}), 400
    credentials_ref = body.get("credentials_ref")

    webhook_secret_plain = secrets.token_urlsafe(32)
    webhook_secret_encrypted = encrypt_credential(webhook_secret_plain)
    now = datetime.utcnow()

    def _create() -> int | None:
        existing = (
            db((db.code_repos.org_id == org_id) & (db.code_repos.name == name)).select().first()
        )
        if existing is not None:
            return None
        repo_id = db.code_repos.insert(
            org_id=org_id,
            name=name,
            source_url=source_url,
            credentials_ref=credentials_ref,
            webhook_secret=webhook_secret_encrypted,
            index_status="pending",
        )
        db.commit()
        return repo_id

    repo_id = await asyncio.to_thread(_create)
    if repo_id is None:
        return jsonify({"error": "a repo with this name already exists in your org"}), 409

    response = {
        "id": repo_id,
        "org_id": org_id,
        "name": name,
        "source_url": source_url,
        "index_status": "pending",
        "last_commit": None,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "webhook_secret": webhook_secret_plain,  # shown exactly once, never stored/returned again
    }
    return jsonify(response), 201


@api_v1_bp.route("/code-repos", methods=["GET"])
@require_auth
async def list_code_repos() -> tuple[Any, int]:
    """List CodeRAG-registered repos for the caller's org."""
    org_id = g.user.get("organization_id")
    if not _coderag_enabled(org_id):
        return jsonify({"error": "coderag feature disabled"}), 404

    def _fetch() -> list[Any]:
        return list(db(db.code_repos.org_id == org_id).select())

    rows = await asyncio.to_thread(_fetch)
    return jsonify({"repos": [_serialize(r) for r in rows]}), 200


@api_v1_bp.route("/code-repos/<int:repo_id>", methods=["GET"])
@require_auth
async def get_code_repo(repo_id: int) -> tuple[Any, int]:
    """Fetch a single repo, org-scoped (IDOR-safe: 404 outside the caller's org)."""
    org_id = g.user.get("organization_id")
    if not _coderag_enabled(org_id):
        return jsonify({"error": "coderag feature disabled"}), 404

    def _fetch() -> Any:
        query = (db.code_repos.id == repo_id) & (db.code_repos.org_id == org_id)
        return db(query).select().first()

    row = await asyncio.to_thread(_fetch)
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(_serialize(row)), 200


@api_v1_bp.route("/code-repos/<int:repo_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.CODE_REPO_WRITE)
async def delete_code_repo(repo_id: int) -> tuple[Any, int]:
    """Delete a repo registration, org-scoped (IDOR-safe: 404 outside the caller's org)."""
    org_id = g.user.get("organization_id")
    if not _coderag_enabled(org_id):
        return jsonify({"error": "coderag feature disabled"}), 404

    def _delete() -> bool:
        query = (db.code_repos.id == repo_id) & (db.code_repos.org_id == org_id)
        existing = db(query).select().first()
        if existing is None:
            return False
        db(query).delete()
        db.commit()
        return True

    deleted = await asyncio.to_thread(_delete)
    if not deleted:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "deleted", "id": repo_id}), 200


@api_v1_bp.route("/code-repos/<int:repo_id>/reindex", methods=["POST"])
@require_auth
@require_scope(Permission.CODE_REPO_WRITE)
async def reindex_code_repo(repo_id: int) -> tuple[Any, int]:
    """Manually trigger (re)indexing for one repo, org-scoped.

    The org-ownership check happens here, before ``CodeRagWorker`` is ever
    constructed -- ``CodeRagWorker.index()`` looks a repo up by id alone
    with no org filter (it also serves the cron/webhook triggers, which
    have no caller org to check), so this route is the IDOR guard for the
    manual-trigger path: a repo id outside the caller's org must never
    reach the worker at all.
    """
    org_id = g.user.get("organization_id")
    if not _coderag_enabled(org_id):
        return jsonify({"error": "coderag feature disabled"}), 404

    def _fetch() -> Any:
        query = (db.code_repos.id == repo_id) & (db.code_repos.org_id == org_id)
        return db(query).select().first()

    row = await asyncio.to_thread(_fetch)
    if row is None:
        return jsonify({"error": "not found"}), 404

    body = await request.get_json(silent=True) or {}
    worker = create_coderag_worker(db)
    result = await worker.index(repo_id, branch=body.get("branch"), trigger="manual")
    return (
        jsonify(
            {
                "repo_id": result.repo_id,
                "branch_ref": result.branch_ref,
                "index_status": result.index_status,
                "last_commit": result.last_commit,
                "files_changed": result.files_changed,
                "files_deleted": result.files_deleted,
                "error": result.error,
            }
        ),
        200,
    )


@api_v1_bp.route("/code-repos/reindex-all", methods=["POST"])
@require_auth
@require_scope(Permission.CODE_REPO_WRITE)
async def reindex_all_code_repos() -> tuple[Any, int]:
    """Re-index every non-disabled repo in the caller's org -- org-scoped, never another org's.

    Deliberately does NOT call ``CodeRagWorker.run_scheduled()`` -- that
    method sweeps every org's ``code_repos`` with no org filter (it is the
    genuine system/cron-authed entrypoint, invoked outside the HTTP API),
    so calling it from a per-org ``CODE_REPO_WRITE``-scoped route would let
    any org's resource_manager trigger a cross-tenant re-index of every
    other org's repos. This route instead fetches only the caller's org's
    repo ids (from the validated JWT, same IDOR pattern as every other
    route in this module) and calls ``CodeRagWorker.index()`` once per id.
    """
    org_id = g.user.get("organization_id")
    if not _coderag_enabled(org_id):
        return jsonify({"error": "coderag feature disabled"}), 404

    def _fetch_repo_ids() -> list[int]:
        query = (db.code_repos.org_id == org_id) & (db.code_repos.index_status != "disabled")
        return [r.id for r in db(query).select()]

    repo_ids = await asyncio.to_thread(_fetch_repo_ids)
    worker = create_coderag_worker(db)
    results = [await worker.index(repo_id, trigger="manual") for repo_id in repo_ids]
    return (
        jsonify(
            {
                "indexed": len(results),
                "results": [
                    {
                        "repo_id": r.repo_id,
                        "branch_ref": r.branch_ref,
                        "index_status": r.index_status,
                        "error": r.error,
                    }
                    for r in results
                ],
            }
        ),
        200,
    )


def _extract_branch(payload: dict[str, Any]) -> str | None:
    """Extract the target branch from a push-webhook payload's ``ref`` (e.g. ``refs/heads/main``).

    Returns ``None`` when absent -- ``CodeRagWorker.index()`` already
    defaults a missing branch to ``"main"``, so callers pass this straight
    through rather than duplicating that fallback here.
    """
    ref = payload.get("ref", "")
    return ref.rsplit("/", 1)[-1] if ref else None


@api_v1_bp.route("/code-repos/<int:repo_id>/webhook", methods=["POST"])
async def code_repo_webhook(repo_id: int) -> tuple[Any, int]:
    """GitHub/Gitea push webhook for one specific repo: HMAC-verified, then re-index.

    Per-repo path -- deliberately NOT resolved by ``source_url`` -- because
    ``source_url`` is not unique across orgs (only ``(org_id, name)`` is).
    A ``source_url``-based ``.first()`` lookup would silently match an
    arbitrary org's registration whenever two tenants register the same
    clone URL: it would verify against the wrong org's secret and leave
    every *other* org sharing that URL with a permanently, silently dead
    push-triggered reindex (a 401 with no indication why). Identifying the
    repo by path param removes the ambiguity entirely -- each org's push
    provider is configured with that org's own webhook URL.

    Deliberately not behind ``require_auth``/``require_scope`` -- external
    Git hosts cannot carry a WaddleAI JWT. Every failure path (malformed
    body, unknown repo, missing secret, undecryptable secret, bad
    signature) rejects; there is no path that reaches ``worker.index()``
    without a verified signature. The flag check happens AFTER signature
    verification so an unauthenticated caller can't use this endpoint to
    probe an org's flag state.
    """
    raw_body = await request.get_data()
    if isinstance(raw_body, str):  # pragma: no cover -- as_text=False always returns bytes
        raw_body = raw_body.encode("utf-8")
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return jsonify({"error": "invalid JSON payload"}), 400

    def _fetch() -> Any:
        return db(db.code_repos.id == repo_id).select().first()

    row = await asyncio.to_thread(_fetch)
    if row is None:
        return jsonify({"error": "unknown repository"}), 404

    signature = request.headers.get("X-Hub-Signature-256", "")
    try:
        secret = decrypt_credential(row.webhook_secret) if row.webhook_secret else ""
    except ValueError:
        # A corrupted/un-decryptable stored secret fails closed (never 500)
        # -- treated exactly like "no secret configured", which
        # verify_webhook_signature already rejects unconditionally.
        secret = ""  # nosec B105 -- fail-closed sentinel, not a credential

    if not verify_webhook_signature(raw_body, signature, secret):
        return jsonify({"error": "invalid signature"}), 401

    if not _coderag_enabled(row.org_id):
        return jsonify({"error": "coderag feature disabled"}), 404

    branch = _extract_branch(payload)
    worker = create_coderag_worker(db)
    await worker.index(repo_id, branch=branch, trigger="webhook")

    return jsonify({"status": "accepted", "repo_id": repo_id, "branch": branch or "main"}), 202
