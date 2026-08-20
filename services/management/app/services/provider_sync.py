"""WaddleAI Provider Sync Service.

Synchronizes Ollama deployments' model-specific routes to the AILB gRPC
module (fleet-side load balancing for locally-hosted models -- unrelated to
the retired MarchProxy AILB provider-config/virtual-key sync surface, which
was dropped alongside migration 007: its provider/virtual-key sync-status
table had no live caller and no successor bookkeeping table, so
`sync_provider`, `sync_all_providers`, `remove_provider`,
`verify_sync_status`, `get_pending_syncs`, `sync_virtual_key`, and
`sync_all_virtual_keys` were removed rather than repointed).
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from ..grpc.client import AILBModuleClient, RouteConfig

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):  # noqa: UP042 -- str(x)/format() semantics differ under StrEnum; no logic changes in this pass
    """Sync status states."""

    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"
    DELETED = "deleted"


@dataclass
class SyncResult:
    """Result of a sync operation."""

    success: bool
    provider_id: int
    route_id: str | None = None
    status: SyncStatus = SyncStatus.PENDING
    message: str = ""
    error: str | None = None
    timestamp: datetime = None

    def __post_init__(self):
        """Default timestamp to now if the caller did not supply one."""
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()


class ProviderSyncService:
    """Synchronizes Ollama deployments' model-specific routes to AILB.

    Handles:
    - Model-specific routing for Ollama deployments
    - Tracking per-model route sync status (`ollama_model_routes`)
    - Handling sync failures and retries
    """

    def __init__(self, db, ailb_client: AILBModuleClient | None = None):
        """Bind the DAL handle and optional AILB gRPC client used for route sync calls."""
        self.db = db
        self.ailb_client = ailb_client
        self._instance_id = ""

    def set_ailb_client(self, client: AILBModuleClient):
        """Set the AILB client."""
        self.ailb_client = client

    def set_instance_id(self, instance_id: str):
        """Set the AILB instance ID to sync to."""
        self._instance_id = instance_id

    # Ollama Model-Specific Routing

    def sync_ollama_deployment(self, deployment_id: int) -> SyncResult:
        """Sync an Ollama deployment with model-specific routes to AILB.

        Creates individual routes for each model on the deployment,
        enabling model-aware load balancing.

        Args:
            deployment_id: ID of the Ollama deployment

        Returns:
            SyncResult with success status and details

        """
        db = self.db

        # Get deployment
        deployment = db(db.ollama_deployments.id == deployment_id).select().first()
        if not deployment:
            return SyncResult(
                success=False,
                provider_id=deployment_id,
                status=SyncStatus.FAILED,
                error="Deployment not found",
            )

        # Get all models on this deployment
        models = db(db.ollama_models.deployment_id == deployment_id).select()

        if not models:
            logger.warning(f"No models found for Ollama deployment {deployment_id}")
            return SyncResult(
                success=False,
                provider_id=deployment_id,
                status=SyncStatus.FAILED,
                error="No models assigned to deployment",
            )

        try:
            routes = []
            for model in models:
                # Create model-specific route
                route = self._ollama_model_to_route(deployment, model)
                routes.append(route)

            # Sync all model routes to AILB
            if self.ailb_client and self.ailb_client.is_connected():
                result = self.ailb_client.update_routes(
                    routes=routes, instance_id=self._instance_id
                )
                if not result.get("success"):
                    raise Exception(result.get("message", "Unknown error"))

            # Update sync records for each model
            now = datetime.utcnow()
            for model in models:
                route_id = f"ollama-{deployment_id}-{model.model_name}"

                # Check if sync record exists
                sync_record = db(db.ollama_model_routes.model_id == model.id).select().first()

                if sync_record:
                    db(db.ollama_model_routes.id == sync_record.id).update(
                        ailb_route_id=route_id,
                        sync_status="synced",
                        last_synced=now,
                        sync_error=None,
                    )
                else:
                    db.ollama_model_routes.insert(
                        model_id=model.id,
                        deployment_id=deployment_id,
                        ailb_instance_id=self._instance_id,
                        ailb_route_id=route_id,
                        sync_status="synced",
                        last_synced=now,
                    )

            db.commit()

            logger.info(
                f"Successfully synced {len(models)} model routes for deployment {deployment_id}"
            )
            return SyncResult(
                success=True,
                provider_id=deployment_id,
                status=SyncStatus.SYNCED,
                message=f"Synced {len(models)} model routes",
            )

        except Exception as e:
            logger.error(f"Failed to sync Ollama deployment {deployment_id}: {e}")
            return SyncResult(
                success=False, provider_id=deployment_id, status=SyncStatus.FAILED, error=str(e)
            )

    def _ollama_model_to_route(self, deployment, model) -> RouteConfig:
        """Convert Ollama deployment + model to AILB route configuration.

        Creates a route that matches requests for this specific model
        and routes them to the deployment endpoint.
        """
        from urllib.parse import urlparse

        parsed = urlparse(deployment.endpoint_url)
        host = parsed.netloc.split(":")[0] if ":" in parsed.netloc else parsed.netloc
        port = parsed.port or 11434

        # Create model-specific route
        route = RouteConfig(
            route_id=f"ollama-{deployment.id}-{model.model_name}",
            protocol="PROTOCOL_HTTP" if parsed.scheme == "http" else "PROTOCOL_HTTPS",
            destination_pattern=f"{host}:{port}",
            destination_port=port,
            path_pattern="/v1/chat/completions",  # OpenAI-compatible endpoint
            priority=200,  # Higher priority than generic routes
            headers={
                "X-Ollama-Model": model.model_name,  # Model identifier in header
            },
            metadata={
                "waddleai_deployment_id": str(deployment.id),
                "waddleai_model_id": str(model.id),
                "deployment_name": deployment.name,
                "model_name": model.model_name,
                "model_tag": model.model_tag or "latest",
                "provider_type": "ollama",
                "routing_type": "model-specific",
            },
        )

        return route

    def sync_all_ollama_deployments(self) -> dict[int, SyncResult]:
        """Sync all active Ollama deployments with model-specific routes.

        Returns:
            Dict mapping deployment_id to SyncResult

        """
        db = self.db
        results = {}

        # Get all deployments with models
        deployments = db(
            (db.ollama_deployments.status.belongs(["running", "pending"]))
            & (db.ollama_deployments.id > 0)
        ).select()

        for deployment in deployments:
            # Check if deployment has models
            model_count = db(db.ollama_models.deployment_id == deployment.id).count()
            if model_count > 0:
                results[deployment.id] = self.sync_ollama_deployment(deployment.id)

        return results

    def remove_ollama_model_route(self, model_id: int) -> bool:
        """Remove a specific Ollama model route from AILB.

        Args:
            model_id: ID of the Ollama model

        Returns:
            True if successful

        """
        db = self.db

        # Get sync record
        sync_record = db(db.ollama_model_routes.model_id == model_id).select().first()
        if not sync_record:
            return True  # Nothing to remove

        try:
            # Delete route from AILB
            if self.ailb_client and self.ailb_client.is_connected():
                if sync_record.ailb_route_id:
                    self.ailb_client.delete_route(
                        route_id=sync_record.ailb_route_id, instance_id=self._instance_id
                    )

            # Delete sync record
            db(db.ollama_model_routes.id == sync_record.id).delete()
            db.commit()

            logger.info(f"Removed model {model_id} route from AILB")
            return True

        except Exception as e:
            logger.error(f"Failed to remove model {model_id} route: {e}")
            return False

    def get_model_route_status(self, model_id: int) -> dict[str, Any]:
        """Get sync status for a specific Ollama model route.

        Args:
            model_id: ID of the Ollama model

        Returns:
            Dict with sync status details

        """
        db = self.db

        sync_record = db(db.ollama_model_routes.model_id == model_id).select().first()
        if not sync_record:
            return {"synced": False, "status": "not_synced", "message": "No sync record found"}

        model = db(db.ollama_models.id == model_id).select().first()
        if not model:
            return {"synced": False, "status": "model_not_found", "message": "Model not found"}

        return {
            "synced": sync_record.sync_status == "synced",
            "status": sync_record.sync_status,
            "route_id": sync_record.ailb_route_id,
            "deployment_id": sync_record.deployment_id,
            "last_synced": sync_record.last_synced.isoformat() if sync_record.last_synced else None,
            "sync_error": sync_record.sync_error,
            "model_name": model.model_name,
            "model_tag": model.model_tag,
        }
