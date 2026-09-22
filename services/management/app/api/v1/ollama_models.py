"""WaddleAI Management API v1 - Ollama Model Assignment Endpoints.

Endpoints for assigning models to specific Ollama nodes and managing
model-specific routing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from quart import current_app, jsonify, request
from quart_schema import validate_request, validate_response

from shared.auth.rbac import Permission

from ...extensions import db
from ...services.provider_sync import ProviderSyncService
from . import api_v1_bp
from ._pagination import PageRequest
from .auth import require_auth, require_scope

# ---------------------------------------------------------------------------
# OpenAPI request/response models (audit-2026-09-14-wave2).
#
# Request models make every field Optional with the handler's own defaults so
# quart-schema never pre-empts the handler's presence checks. Response models
# list exactly the fields each handler returns -- a field omitted here is
# silently dropped from the response, the regression these models prevent.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PageMeta:
    """Pagination metadata block emitted by ``PageRequest.meta``."""

    page: int
    limit: int
    total: int | None
    pages: int | None


@dataclass(slots=True)
class OllamaModelListItem:
    """One row of the cross-deployment model-list response."""

    id: int
    model_name: str
    model_tag: str | None
    deployment_id: int | None
    deployment_name: str | None
    deployment_endpoint: str | None
    status: str | None
    size_bytes: int | None
    auto_pull: bool | None
    route_synced: bool
    route_id: str | None
    last_updated: str | None


@dataclass(slots=True)
class OllamaModelListResponse:
    """Response body for GET /api/v1/ollama/models."""

    models: list[OllamaModelListItem]
    total: int
    pagination: PageMeta


@dataclass(slots=True)
class DeploymentModelItem:
    """One row of the per-deployment model-list response."""

    id: int
    model_name: str
    model_tag: str | None
    status: str | None
    size_bytes: int | None
    auto_pull: bool | None
    route_synced: bool
    route_id: str | None
    last_updated: str | None


@dataclass(slots=True)
class DeploymentModelsResponse:
    """Response body for GET /api/v1/ollama/deployments/<id>/models."""

    deployment_id: int
    deployment_name: str
    models: list[DeploymentModelItem]
    total: int
    pagination: PageMeta


@dataclass(slots=True)
class AssignModelRequest:
    """Request body for POST /api/v1/ollama/models/assign."""

    deployment_id: int | None = None
    model_name: str | None = None
    model_tag: str | None = "latest"
    auto_pull: bool | None = False
    sync_to_ailb: bool | None = True


@dataclass(slots=True)
class AssignModelResponse:
    """Response body for a successful model assignment."""

    success: bool
    model_id: int
    message: str
    route_sync_status: str | None


@dataclass(slots=True)
class ReassignModelRequest:
    """Request body for POST /api/v1/ollama/models/<id>/reassign."""

    new_deployment_id: int | None = None
    sync_to_ailb: bool | None = True


@dataclass(slots=True)
class ReassignModelResponse:
    """Response body for a successful model reassignment."""

    success: bool
    model_id: int
    old_deployment_id: int | None
    new_deployment_id: int
    message: str


@dataclass(slots=True)
class UnassignModelResponse:
    """Response body for DELETE /api/v1/ollama/models/<id>."""

    success: bool
    message: str
    deployment_id: int | None


@dataclass(slots=True)
class SyncModelRouteResponse:
    """Response body for POST /api/v1/ollama/models/<id>/sync (success branch)."""

    success: bool
    message: str
    route_status: dict[str, Any] | None


@dataclass(slots=True)
class BulkAssignRequest:
    """Request body for POST /api/v1/ollama/models/bulk-assign."""

    assignments: list[dict[str, Any]] | None = field(default=None)
    sync_to_ailb: bool | None = True


@dataclass(slots=True)
class BulkAssignResponse:
    """Response body for POST /api/v1/ollama/models/bulk-assign.

    ``results`` entries are intentionally heterogeneous (success and failure
    rows carry different keys), so each is typed as a free object rather than
    a fixed model -- pinning a single shape here would silently drop the
    per-outcome fields. ``sync_results`` is keyed by deployment id.
    """

    success: bool
    results: list[dict[str, Any]]
    total_assigned: int
    total_failed: int
    sync_results: dict[int, Any]


@dataclass(slots=True)
class SyncModelStatus:
    """A single model's route-sync status within the deployment-sync response."""

    model_id: int
    model_name: str
    route_synced: bool
    route_id: str | None


@dataclass(slots=True)
class SyncDeploymentModelsResponse:
    """Response body for POST /api/v1/ollama/deployments/<id>/sync-models (success)."""

    success: bool
    message: str
    deployment_id: int
    models_synced: int
    model_statuses: list[SyncModelStatus]


@api_v1_bp.route("/ollama/models", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_MODEL_ADMIN)
@validate_response(OllamaModelListResponse, 200)
async def list_all_ollama_models():
    """List all Ollama models across all deployments (bounded page)."""
    page = PageRequest.from_request()

    def _fetch():
        query = db.ollama_models.id > 0
        total = db(query).count()
        models = db(query).select(limitby=page.limitby, orderby=db.ollama_models.id)
        rows = []
        for model in models:
            deployment = db(db.ollama_deployments.id == model.deployment_id).select().first()
            route = db(db.ollama_model_routes.model_id == model.id).select().first()
            rows.append((model, deployment, route))
        return total, rows

    total, rows = await asyncio.to_thread(_fetch)

    result = []
    for model, deployment, route in rows:
        result.append(
            {
                "id": model.id,
                "model_name": model.model_name,
                "model_tag": model.model_tag,
                "deployment_id": model.deployment_id,
                "deployment_name": deployment.name if deployment else None,
                "deployment_endpoint": deployment.endpoint_url if deployment else None,
                "status": model.status,
                "size_bytes": model.size_bytes,
                "auto_pull": model.auto_pull,
                "route_synced": route.sync_status == "synced" if route else False,
                "route_id": route.ailb_route_id if route else None,
                "last_updated": model.last_updated.isoformat() if model.last_updated else None,
            }
        )

    return {"models": result, "total": len(result), **page.meta(total)}, 200


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/models", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_MODEL_ADMIN)
@validate_response(DeploymentModelsResponse, 200)
async def list_deployment_models(deployment_id):
    """List models on a specific Ollama deployment (bounded page)."""
    page = PageRequest.from_request()

    def _fetch():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return None, 0, None

        query = db.ollama_models.deployment_id == deployment_id
        total = db(query).count()
        models = db(query).select(limitby=page.limitby, orderby=db.ollama_models.id)
        rows = []
        for model in models:
            route = db(db.ollama_model_routes.model_id == model.id).select().first()
            rows.append((model, route))
        return deployment, total, rows

    deployment, total, rows = await asyncio.to_thread(_fetch)

    if not deployment:
        return jsonify({"error": "Deployment not found"}), 404

    result = []
    for model, route in rows:
        result.append(
            {
                "id": model.id,
                "model_name": model.model_name,
                "model_tag": model.model_tag,
                "status": model.status,
                "size_bytes": model.size_bytes,
                "auto_pull": model.auto_pull,
                "route_synced": route.sync_status == "synced" if route else False,
                "route_id": route.ailb_route_id if route else None,
                "last_updated": model.last_updated.isoformat() if model.last_updated else None,
            }
        )

    return {
        "deployment_id": deployment_id,
        "deployment_name": deployment.name,
        "models": result,
        "total": len(result),
        **page.meta(total),
    }, 200


@api_v1_bp.route("/ollama/models/assign", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_MODEL_ADMIN)
@validate_response(AssignModelResponse, 201)
@validate_request(AssignModelRequest)
async def assign_model_to_deployment(data: AssignModelRequest):
    """Assign a model to a specific Ollama deployment.

    This creates a model-to-node mapping that will be used for
    intelligent routing via MarchProxy AILB.
    """
    if data.deployment_id is None:
        return jsonify({"error": "deployment_id is required"}), 400
    if data.model_name is None:
        return jsonify({"error": "model_name is required"}), 400

    deployment_id = data.deployment_id
    model_name = data.model_name
    model_tag = data.model_tag or "latest"
    auto_pull = data.auto_pull if data.auto_pull is not None else False
    sync_to_ailb = data.sync_to_ailb if data.sync_to_ailb is not None else True
    ailb_client = current_app.extensions.get("ailb_client")

    def _assign():
        # Check if deployment exists
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return "not_found", None, None

        # Check if model already assigned to this deployment
        existing = (
            db(
                (db.ollama_models.deployment_id == deployment_id)
                & (db.ollama_models.model_name == model_name)
                & (db.ollama_models.model_tag == model_tag)
            )
            .select()
            .first()
        )

        if existing:
            return "conflict", None, None

        # Create model assignment
        new_model_id = db.ollama_models.insert(
            deployment_id=deployment_id,
            model_name=model_name,
            model_tag=model_tag,
            status="assigned",
            auto_pull=auto_pull,
            last_updated=datetime.utcnow(),
        )
        db.commit()

        # Sync to AILB if requested
        route_sync_status = None
        if sync_to_ailb:
            sync_service = ProviderSyncService(db, ailb_client)
            sync_service.set_instance_id("ailb-default")

            sync_result = sync_service.sync_ollama_deployment(deployment_id)
            route_sync_status = sync_result.status.value

        return "ok", new_model_id, route_sync_status

    status, model_id, route_sync_status = await asyncio.to_thread(_assign)

    if status == "not_found":
        return jsonify({"error": "Deployment not found"}), 404
    if status == "conflict":
        return jsonify({"error": "Model already assigned to this deployment"}), 409

    return {
        "success": True,
        "model_id": model_id,
        "message": "Model assigned successfully",
        "route_sync_status": route_sync_status,
    }, 201


@api_v1_bp.route("/ollama/models/<int:model_id>/reassign", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_MODEL_ADMIN)
@validate_response(ReassignModelResponse, 200)
@validate_request(ReassignModelRequest)
async def reassign_model(model_id, data: ReassignModelRequest):
    """Reassign a model to a different Ollama deployment.

    This is useful for load balancing or moving models between nodes.
    """
    if data.new_deployment_id is None:
        return jsonify({"error": "new_deployment_id is required"}), 400

    new_deployment_id = data.new_deployment_id
    sync_to_ailb = data.sync_to_ailb if data.sync_to_ailb is not None else True
    ailb_client = current_app.extensions.get("ailb_client")

    def _reassign():
        model = db(db.ollama_models.id == model_id).select().first()
        if not model:
            return "model_not_found", None

        # Check if new deployment exists
        new_deployment = db(db.ollama_deployments.id == new_deployment_id).select().first()
        if not new_deployment:
            return "deployment_not_found", None

        # Check if model already on new deployment
        existing = (
            db(
                (db.ollama_models.deployment_id == new_deployment_id)
                & (db.ollama_models.model_name == model.model_name)
                & (db.ollama_models.model_tag == model.model_tag)
                & (db.ollama_models.id != model_id)
            )
            .select()
            .first()
        )

        if existing:
            return "conflict", None

        old_deployment_id = model.deployment_id

        # Update model assignment
        db(db.ollama_models.id == model_id).update(
            deployment_id=new_deployment_id, status="assigned", last_updated=datetime.utcnow()
        )
        db.commit()

        # Sync both deployments to AILB if requested
        if sync_to_ailb:
            sync_service = ProviderSyncService(db, ailb_client)
            sync_service.set_instance_id("ailb-default")

            # Remove old route
            sync_service.remove_ollama_model_route(model_id)

            # Sync new deployment (will create new route)
            sync_service.sync_ollama_deployment(new_deployment_id)

            # Resync old deployment if it still has models
            old_model_count = db(db.ollama_models.deployment_id == old_deployment_id).count()
            if old_model_count > 0:
                sync_service.sync_ollama_deployment(old_deployment_id)

        return "ok", old_deployment_id

    status, old_deployment_id = await asyncio.to_thread(_reassign)

    if status == "model_not_found":
        return jsonify({"error": "Model not found"}), 404
    if status == "deployment_not_found":
        return jsonify({"error": "New deployment not found"}), 404
    if status == "conflict":
        return jsonify({"error": "Model already exists on target deployment"}), 409

    return {
        "success": True,
        "model_id": model_id,
        "old_deployment_id": old_deployment_id,
        "new_deployment_id": new_deployment_id,
        "message": "Model reassigned successfully",
    }, 200


@api_v1_bp.route("/ollama/models/<int:model_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.OLLAMA_MODEL_ADMIN)
@validate_response(UnassignModelResponse, 200)
async def unassign_model(model_id):
    """Remove a model assignment from a deployment.

    Query params:
    - remove_route: If true, also remove the AILB route (default: true)
    """
    remove_route = request.args.get("remove_route", "true").lower() == "true"
    ailb_client = current_app.extensions.get("ailb_client")

    def _unassign():
        model = db(db.ollama_models.id == model_id).select().first()
        if not model:
            return "not_found", None

        deployment_id = model.deployment_id

        # Remove AILB route if requested
        if remove_route:
            sync_service = ProviderSyncService(db, ailb_client)
            sync_service.set_instance_id("ailb-default")
            sync_service.remove_ollama_model_route(model_id)

        # Delete model assignment
        db(db.ollama_models.id == model_id).delete()
        db.commit()

        return "ok", deployment_id

    status, deployment_id = await asyncio.to_thread(_unassign)

    if status == "not_found":
        return jsonify({"error": "Model not found"}), 404

    return {
        "success": True,
        "message": "Model unassigned successfully",
        "deployment_id": deployment_id,
    }, 200


@api_v1_bp.route("/ollama/models/<int:model_id>/sync", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_MODEL_ADMIN)
@validate_response(SyncModelRouteResponse, 200)
async def sync_model_route(model_id):
    """Manually trigger AILB route sync for a specific model.

    This creates or updates the model-specific route in MarchProxy AILB.
    """
    ailb_client = current_app.extensions.get("ailb_client")

    def _sync():
        model = db(db.ollama_models.id == model_id).select().first()
        if not model:
            return "not_found", None

        sync_service = ProviderSyncService(db, ailb_client)
        sync_service.set_instance_id("ailb-default")

        # Sync the deployment (which includes this model)
        sync_result = sync_service.sync_ollama_deployment(model.deployment_id)

        if sync_result.success:
            # Get updated route status
            route_status = sync_service.get_model_route_status(model_id)
            return "ok", route_status
        else:
            return "failed", sync_result

    status, payload = await asyncio.to_thread(_sync)

    if status == "not_found":
        return jsonify({"error": "Model not found"}), 404

    if status == "ok":
        return {
            "success": True,
            "message": "Model route synced successfully",
            "route_status": payload,
        }, 200

    sync_result = payload
    return jsonify(
        {"success": False, "error": sync_result.error, "message": sync_result.message}
    ), 500


@api_v1_bp.route("/ollama/models/<int:model_id>/route-status", methods=["GET"])
@require_auth
@require_scope(Permission.OLLAMA_MODEL_ADMIN)
async def get_model_route_status(model_id):
    """Get AILB route sync status for a specific model.

    The body is the ``ProviderSyncService.get_model_route_status`` result
    verbatim -- a computed, backend-shaped status object, not a database row.
    It is deliberately left without a ``@validate_response`` schema: the
    service owns its shape and may add diagnostic keys, and no persisted
    columns (let alone PII) are echoed here, so there is nothing to over-expose.
    """
    ailb_client = current_app.extensions.get("ailb_client")

    def _fetch():
        model = db(db.ollama_models.id == model_id).select().first()
        if not model:
            return "not_found", None

        sync_service = ProviderSyncService(db, ailb_client)
        route_status = sync_service.get_model_route_status(model_id)
        return "ok", route_status

    status, route_status = await asyncio.to_thread(_fetch)

    if status == "not_found":
        return jsonify({"error": "Model not found"}), 404

    return jsonify(route_status)


@api_v1_bp.route("/ollama/models/bulk-assign", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_MODEL_ADMIN)
@validate_response(BulkAssignResponse, 201)
@validate_request(BulkAssignRequest)
async def bulk_assign_models(data: BulkAssignRequest):
    """Bulk assign multiple models to deployments.

    Useful for initial setup or rebalancing.
    """
    if data.assignments is None:
        return jsonify({"error": "assignments array is required"}), 400

    assignments = data.assignments
    sync_to_ailb = data.sync_to_ailb if data.sync_to_ailb is not None else True
    ailb_client = current_app.extensions.get("ailb_client")

    def _bulk_assign():
        results = []
        affected_deployments = set()

        for assignment in assignments:
            deployment_id = assignment.get("deployment_id")
            model_name = assignment.get("model_name")
            model_tag = assignment.get("model_tag", "latest")
            auto_pull = assignment.get("auto_pull", False)

            if not deployment_id or not model_name:
                results.append(
                    {
                        "success": False,
                        "model_name": model_name,
                        "error": "deployment_id and model_name required",
                    }
                )
                continue

            # Check if deployment exists
            deployment = db(db.ollama_deployments.id == deployment_id).select().first()
            if not deployment:
                results.append(
                    {"success": False, "model_name": model_name, "error": "Deployment not found"}
                )
                continue

            # Check for existing assignment
            existing = (
                db(
                    (db.ollama_models.deployment_id == deployment_id)
                    & (db.ollama_models.model_name == model_name)
                    & (db.ollama_models.model_tag == model_tag)
                )
                .select()
                .first()
            )

            if existing:
                results.append(
                    {
                        "success": False,
                        "model_name": model_name,
                        "model_id": existing.id,
                        "error": "Model already assigned",
                    }
                )
                continue

            # Create assignment
            model_id = db.ollama_models.insert(
                deployment_id=deployment_id,
                model_name=model_name,
                model_tag=model_tag,
                status="assigned",
                auto_pull=auto_pull,
                last_updated=datetime.utcnow(),
            )

            affected_deployments.add(deployment_id)

            results.append(
                {
                    "success": True,
                    "model_id": model_id,
                    "model_name": model_name,
                    "deployment_id": deployment_id,
                }
            )

        db.commit()

        # Sync affected deployments to AILB
        sync_results = {}
        if sync_to_ailb and affected_deployments:
            sync_service = ProviderSyncService(db, ailb_client)
            sync_service.set_instance_id("ailb-default")

            for deployment_id in affected_deployments:
                sync_result = sync_service.sync_ollama_deployment(deployment_id)
                sync_results[deployment_id] = sync_result.status.value

        return results, sync_results

    results, sync_results = await asyncio.to_thread(_bulk_assign)

    return {
        "success": True,
        "results": results,
        "total_assigned": sum(1 for r in results if r["success"]),
        "total_failed": sum(1 for r in results if not r["success"]),
        "sync_results": sync_results,
    }, 201


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/sync-models", methods=["POST"])
@require_auth
@require_scope(Permission.OLLAMA_MODEL_ADMIN)
@validate_response(SyncDeploymentModelsResponse, 200)
async def sync_deployment_models(deployment_id):
    """Sync all models on a deployment to AILB.

    Creates/updates model-specific routes for all models on this deployment.
    """
    ailb_client = current_app.extensions.get("ailb_client")

    def _sync():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return "not_found", None

        sync_service = ProviderSyncService(db, ailb_client)
        sync_service.set_instance_id("ailb-default")

        sync_result = sync_service.sync_ollama_deployment(deployment_id)

        if sync_result.success:
            # Get all model route statuses
            models = db(db.ollama_models.deployment_id == deployment_id).select()
            model_statuses = []

            for model in models:
                status = sync_service.get_model_route_status(model.id)
                model_statuses.append(
                    {
                        "model_id": model.id,
                        "model_name": model.model_name,
                        "route_synced": status["synced"],
                        "route_id": status.get("route_id"),
                    }
                )

            return "ok", (sync_result.message, model_statuses)
        else:
            return "failed", sync_result

    status, payload = await asyncio.to_thread(_sync)

    if status == "not_found":
        return jsonify({"error": "Deployment not found"}), 404

    if status == "ok":
        message, model_statuses = payload
        return {
            "success": True,
            "message": message,
            "deployment_id": deployment_id,
            "models_synced": len(model_statuses),
            "model_statuses": model_statuses,
        }, 200

    sync_result = payload
    return jsonify(
        {"success": False, "error": sync_result.error, "message": sync_result.message}
    ), 500
