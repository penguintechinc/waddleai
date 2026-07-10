"""
WaddleAI Management API v1 - Ollama Model Assignment Endpoints

Endpoints for assigning models to specific Ollama nodes and managing
model-specific routing.
"""

import asyncio
from datetime import datetime

from quart import current_app, jsonify, request

from ...extensions import db
from ...services.provider_sync import ProviderSyncService
from . import api_v1_bp
from .auth import require_auth, require_role


@api_v1_bp.route("/ollama/models", methods=["GET"])
@require_auth
@require_role("admin")
async def list_all_ollama_models():
    """List all Ollama models across all deployments"""

    def _fetch():
        models = db(db.ollama_models.id > 0).select()
        rows = []
        for model in models:
            deployment = db(db.ollama_deployments.id == model.deployment_id).select().first()
            route = db(db.ollama_model_routes.model_id == model.id).select().first()
            rows.append((model, deployment, route))
        return rows

    rows = await asyncio.to_thread(_fetch)

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

    return jsonify({"models": result, "total": len(result)})


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/models", methods=["GET"])
@require_auth
@require_role("admin")
async def list_deployment_models(deployment_id):
    """List models on a specific Ollama deployment"""

    def _fetch():
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return None, None

        models = db(db.ollama_models.deployment_id == deployment_id).select()
        rows = []
        for model in models:
            route = db(db.ollama_model_routes.model_id == model.id).select().first()
            rows.append((model, route))
        return deployment, rows

    deployment, rows = await asyncio.to_thread(_fetch)

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

    return jsonify(
        {"deployment_id": deployment_id, "deployment_name": deployment.name, "models": result, "total": len(result)}
    )


@api_v1_bp.route("/ollama/models/assign", methods=["POST"])
@require_auth
@require_role("admin")
async def assign_model_to_deployment():
    """
    Assign a model to a specific Ollama deployment.

    This creates a model-to-node mapping that will be used for
    intelligent routing via MarchProxy AILB.

    Request body:
    {
        "deployment_id": 1,
        "model_name": "llama3.2",
        "model_tag": "latest",
        "auto_pull": true,
        "sync_to_ailb": true
    }
    """
    data = await request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    required_fields = ["deployment_id", "model_name"]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"{field} is required"}), 400

    deployment_id = data["deployment_id"]
    model_name = data["model_name"]
    model_tag = data.get("model_tag", "latest")
    auto_pull = data.get("auto_pull", False)
    sync_to_ailb = data.get("sync_to_ailb", True)
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

    return (
        jsonify(
            {
                "success": True,
                "model_id": model_id,
                "message": "Model assigned successfully",
                "route_sync_status": route_sync_status,
            }
        ),
        201,
    )


@api_v1_bp.route("/ollama/models/<int:model_id>/reassign", methods=["POST"])
@require_auth
@require_role("admin")
async def reassign_model(model_id):
    """
    Reassign a model to a different Ollama deployment.

    This is useful for load balancing or moving models between nodes.

    Request body:
    {
        "new_deployment_id": 2,
        "sync_to_ailb": true
    }
    """
    data = await request.get_json()

    if not data or "new_deployment_id" not in data:
        return jsonify({"error": "new_deployment_id is required"}), 400

    new_deployment_id = data["new_deployment_id"]
    sync_to_ailb = data.get("sync_to_ailb", True)
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

    return jsonify(
        {
            "success": True,
            "model_id": model_id,
            "old_deployment_id": old_deployment_id,
            "new_deployment_id": new_deployment_id,
            "message": "Model reassigned successfully",
        }
    )


@api_v1_bp.route("/ollama/models/<int:model_id>", methods=["DELETE"])
@require_auth
@require_role("admin")
async def unassign_model(model_id):
    """
    Remove a model assignment from a deployment.

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

    return jsonify({"success": True, "message": "Model unassigned successfully", "deployment_id": deployment_id})


@api_v1_bp.route("/ollama/models/<int:model_id>/sync", methods=["POST"])
@require_auth
@require_role("admin")
async def sync_model_route(model_id):
    """
    Manually trigger AILB route sync for a specific model.

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
        return jsonify({"success": True, "message": "Model route synced successfully", "route_status": payload})

    sync_result = payload
    return jsonify({"success": False, "error": sync_result.error, "message": sync_result.message}), 500


@api_v1_bp.route("/ollama/models/<int:model_id>/route-status", methods=["GET"])
@require_auth
@require_role("admin")
async def get_model_route_status(model_id):
    """Get AILB route sync status for a specific model"""
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
@require_role("admin")
async def bulk_assign_models():
    """
    Bulk assign multiple models to deployments.

    Useful for initial setup or rebalancing.

    Request body:
    {
        "assignments": [
            {"deployment_id": 1, "model_name": "llama3.2", "model_tag": "latest"},
            {"deployment_id": 2, "model_name": "mistral", "model_tag": "latest"},
            {"deployment_id": 1, "model_name": "codellama", "model_tag": "latest"}
        ],
        "sync_to_ailb": true
    }
    """
    data = await request.get_json()

    if not data or "assignments" not in data:
        return jsonify({"error": "assignments array is required"}), 400

    assignments = data["assignments"]
    sync_to_ailb = data.get("sync_to_ailb", True)
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
                    {"success": False, "model_name": model_name, "error": "deployment_id and model_name required"}
                )
                continue

            # Check if deployment exists
            deployment = db(db.ollama_deployments.id == deployment_id).select().first()
            if not deployment:
                results.append({"success": False, "model_name": model_name, "error": "Deployment not found"})
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
                {"success": True, "model_id": model_id, "model_name": model_name, "deployment_id": deployment_id}
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

    return (
        jsonify(
            {
                "success": True,
                "results": results,
                "total_assigned": sum(1 for r in results if r["success"]),
                "total_failed": sum(1 for r in results if not r["success"]),
                "sync_results": sync_results,
            }
        ),
        201,
    )


@api_v1_bp.route("/ollama/deployments/<int:deployment_id>/sync-models", methods=["POST"])
@require_auth
@require_role("admin")
async def sync_deployment_models(deployment_id):
    """
    Sync all models on a deployment to AILB.

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
        return jsonify(
            {
                "success": True,
                "message": message,
                "deployment_id": deployment_id,
                "models_synced": len(model_statuses),
                "model_statuses": model_statuses,
            }
        )

    sync_result = payload
    return jsonify({"success": False, "error": sync_result.error, "message": sync_result.message}), 500
