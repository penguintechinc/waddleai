"""§9.3 manual knowledge ingestion: ``/api/v1/knowledge`` upload + CRUD.

PDF -> text via ``pypdf`` (BSD-3); Markdown/plain text -> direct passthrough.
**PyMuPDF/``fitz`` is banned (AGPL) -- never import it here.** Uploaded
content is chunked + embedded via the shared knowledge-layer primitives and
stored org-scoped in ``rag_documents`` with filename + uploader provenance
(§9.3), served through the same search/injection paths as fetched docs.
Every write passes through ``injection_safety.filter_for_store()`` before
persistence -- a poisoned upload is quarantined, not stored clean.

Flag: ``waddleai.knowledge_ingest``.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime
from typing import Any

from quart import g, jsonify, request

from shared.auth.rbac import Permission
from shared.knowledge.embed import embed_cached
from shared.knowledge.injection_safety import filter_for_store
from shared.security.content_filter import ContentFilter
from shared.security.prompt_security import PromptSecurityScanner
from shared.utils.rag_integration import chunk_text

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_scope

logger = logging.getLogger(__name__)

_FLAG_KEY = "waddleai.knowledge_ingest"
_ALLOWED_EXTENSIONS = (".pdf", ".md", ".markdown", ".txt")


def _knowledge_ingest_enabled(org_id: int) -> bool:
    """Fail-safe-OFF check of the ``waddleai.knowledge_ingest`` flag (§14.5)."""
    try:
        from shared.utils.feature_flags import is_feature_enabled

        return is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id), default=False)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("knowledge_ingest flag evaluation failed, treating as OFF: %s", exc)
        return False


def _extract_text(filename: str, raw: bytes) -> str:
    """PDF -> text via ``pypdf``; Markdown/text -> decoded passthrough.

    PyMuPDF/``fitz`` is banned (AGPL) -- this function must never import it.
    """
    if filename.lower().endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    return raw.decode("utf-8", errors="replace")


def _serialize(row: Any) -> dict[str, Any]:
    """Explicit response schema -- never serialize the raw ORM row (security.md)."""
    created_at = getattr(row, "created_at", None)
    return {
        "id": row.id,
        "content": row.content,
        "source": row.source,
        "provenance": row.provenance,
        "created_at": created_at.isoformat() if created_at else None,
    }


@api_v1_bp.route("/knowledge", methods=["POST"])
@require_auth
@require_scope(Permission.KNOWLEDGE_WRITE)
async def upload_knowledge():
    """Upload a PDF or Markdown document into the org knowledge base (§9.3)."""
    org_id = g.user.get("organization_id")
    user_id = g.user.get("user_id")

    if not _knowledge_ingest_enabled(org_id):
        return jsonify({"error": "knowledge_ingest feature disabled"}), 404

    files = await request.files
    upload = files.get("file")
    if upload is None:
        return jsonify({"error": "file required"}), 400

    filename = upload.filename or "upload"
    if not filename.lower().endswith(_ALLOWED_EXTENSIONS):
        return jsonify({"error": "only .pdf, .md, .markdown, .txt files are supported"}), 400

    raw = upload.read()
    try:
        text = await asyncio.to_thread(_extract_text, filename, raw)
    except Exception as exc:
        logger.warning("knowledge upload: extraction failed for %s: %s", filename, exc)
        return jsonify({"error": "failed to extract text from file"}), 400

    if not text.strip():
        return jsonify({"error": "document contained no extractable text"}), 400

    scanner = PromptSecurityScanner(db)
    content_filter = ContentFilter(db=db)
    filter_result = await filter_for_store(
        text, scanner, content_filter, org_id=org_id, user_id=user_id
    )
    if filter_result.quarantined:
        return (
            jsonify(
                {
                    "error": "document rejected by content-safety filter",
                    "reason": filter_result.reason,
                }
            ),
            400,
        )

    provenance = {
        "source_filename": filename,
        "uploader_user_id": user_id,
        "uploaded_at": datetime.utcnow().isoformat(),
    }

    chunks = chunk_text(filter_result.content)
    document_ids: list[int] = []
    for chunk in chunks:
        vector = await embed_cached(chunk, db=db)
        doc_id = await asyncio.to_thread(_insert_document, org_id, chunk, provenance, vector)
        document_ids.append(doc_id)

    return (
        jsonify(
            {
                "status": "created",
                "document_ids": document_ids,
                "chunks": len(chunks),
                "provenance": provenance,
            }
        ),
        201,
    )


def _insert_document(
    org_id: int, content: str, provenance: dict[str, Any], vector: list[float]
) -> int:
    doc_id = db.rag_documents.insert(
        organization_id=org_id,
        collection="knowledge",
        content=content,
        source=provenance["source_filename"],
        scope_type="org",
        author_user_id=provenance["uploader_user_id"],
        trust_tier="verified",
        status="active",
        provenance=provenance,
        embedding=vector,
    )
    db.commit()
    return doc_id


@api_v1_bp.route("/knowledge", methods=["GET"])
@require_auth
async def list_knowledge():
    """List uploaded knowledge documents for the caller's org (§9.3)."""
    org_id = g.user.get("organization_id")
    if not _knowledge_ingest_enabled(org_id):
        return jsonify({"error": "knowledge_ingest feature disabled"}), 404

    def _fetch() -> list[Any]:
        docs = db.rag_documents
        query = (docs.organization_id == org_id) & (docs.collection == "knowledge")
        return list(db(query).select())

    rows = await asyncio.to_thread(_fetch)
    return jsonify({"documents": [_serialize(r) for r in rows]}), 200


@api_v1_bp.route("/knowledge/<int:doc_id>", methods=["GET"])
@require_auth
async def get_knowledge(doc_id: int):
    """Fetch a single knowledge document, org-scoped."""
    org_id = g.user.get("organization_id")
    if not _knowledge_ingest_enabled(org_id):
        return jsonify({"error": "knowledge_ingest feature disabled"}), 404

    def _fetch() -> Any:
        query = (db.rag_documents.id == doc_id) & (db.rag_documents.organization_id == org_id)
        return db(query).select().first()

    row = await asyncio.to_thread(_fetch)
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(_serialize(row)), 200


@api_v1_bp.route("/knowledge/<int:doc_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.KNOWLEDGE_WRITE)
async def delete_knowledge(doc_id: int):
    """Delete a knowledge document, org-scoped (IDOR-safe: 404 outside the caller's org)."""
    org_id = g.user.get("organization_id")
    if not _knowledge_ingest_enabled(org_id):
        return jsonify({"error": "knowledge_ingest feature disabled"}), 404

    def _delete() -> bool:
        query = (db.rag_documents.id == doc_id) & (db.rag_documents.organization_id == org_id)
        existing = db(query).select().first()
        if existing is None:
            return False
        db(query).delete()
        db.commit()
        return True

    deleted = await asyncio.to_thread(_delete)
    if not deleted:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "deleted", "id": doc_id}), 200
