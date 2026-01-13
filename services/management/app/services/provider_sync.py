"""
WaddleAI Provider Sync Service

Synchronizes AI providers from WaddleAI to MarchProxy AILB.
Handles route creation, updates, and deletion.
"""

import json
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Any
from enum import Enum

from ..grpc.client import AILBModuleClient, RouteConfig, RateLimitConfig

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    """Sync status states"""
    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"
    DELETED = "deleted"


@dataclass
class SyncResult:
    """Result of a sync operation"""
    success: bool
    provider_id: int
    route_id: Optional[str] = None
    status: SyncStatus = SyncStatus.PENDING
    message: str = ""
    error: Optional[str] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()


class ProviderSyncService:
    """
    Synchronizes WaddleAI providers to MarchProxy AILB.

    Handles:
    - Converting WaddleAI provider configs to AILB routes
    - Tracking sync status
    - Handling sync failures and retries
    """

    def __init__(self, db, ailb_client: Optional[AILBModuleClient] = None):
        self.db = db
        self.ailb_client = ailb_client
        self._instance_id = ""

    def set_ailb_client(self, client: AILBModuleClient):
        """Set the AILB client"""
        self.ailb_client = client

    def set_instance_id(self, instance_id: str):
        """Set the AILB instance ID to sync to"""
        self._instance_id = instance_id

    def _generate_config_hash(self, provider) -> str:
        """Generate hash of provider config for change detection"""
        config_data = {
            "name": provider.name,
            "provider_type": provider.provider_type,
            "endpoint_url": provider.endpoint_url,
            "model_list": provider.model_list or [],
            "rate_limits": provider.rate_limits or {},
            "priority": provider.priority,
            "enabled": provider.enabled
        }
        config_str = json.dumps(config_data, sort_keys=True)
        return hashlib.sha256(config_str.encode()).hexdigest()[:16]

    def _provider_to_route(self, provider) -> RouteConfig:
        """Convert WaddleAI provider to AILB route configuration"""
        # Get API key from connection_links if available
        api_key = provider.api_key if hasattr(provider, 'api_key') else ""

        route = RouteConfig(
            route_id=f"waddleai-{provider.id}",
            destination_pattern=provider.endpoint_url,
            priority=provider.priority or 100,
            metadata={
                "waddleai_provider_id": str(provider.id),
                "provider_type": provider.provider_type,
                "provider_name": provider.name,
                "models": json.dumps(provider.model_list or []),
                "config_hash": self._generate_config_hash(provider)
            }
        )

        # Set protocol based on endpoint
        if provider.endpoint_url:
            if provider.endpoint_url.startswith("https://"):
                route.protocol = "PROTOCOL_HTTPS"
            else:
                route.protocol = "PROTOCOL_HTTP"

        # Add authentication headers
        headers = {}
        if api_key:
            if provider.provider_type in ["openai", "azure_openai"]:
                headers["Authorization"] = f"Bearer {api_key}"
            elif provider.provider_type == "anthropic":
                headers["x-api-key"] = api_key
                headers["anthropic-version"] = "2024-01-01"
            elif provider.provider_type == "cohere":
                headers["Authorization"] = f"Bearer {api_key}"
            elif provider.provider_type == "gemini":
                # Gemini uses query param, but can also use header
                headers["x-goog-api-key"] = api_key

        route.headers = headers

        return route

    def sync_provider(self, provider_id: int) -> SyncResult:
        """
        Sync a single provider to AILB.

        Args:
            provider_id: ID of the provider to sync

        Returns:
            SyncResult with success status and details
        """
        db = self.db

        # Get provider
        provider = db(db.ai_providers.id == provider_id).select().first()
        if not provider:
            return SyncResult(
                success=False,
                provider_id=provider_id,
                status=SyncStatus.FAILED,
                error="Provider not found"
            )

        # Check if provider is enabled for AILB sync
        if not provider.ailb_sync_enabled:
            return SyncResult(
                success=False,
                provider_id=provider_id,
                status=SyncStatus.PENDING,
                message="AILB sync not enabled for this provider"
            )

        # Get or create sync record
        sync_record = db(db.marchproxy_ailb_sync.provider_id == provider_id).select().first()

        try:
            # Generate route config
            route = self._provider_to_route(provider)
            config_hash = self._generate_config_hash(provider)

            # Check if config has changed
            if sync_record and sync_record.config_hash == config_hash:
                logger.info(f"Provider {provider_id} config unchanged, skipping sync")
                return SyncResult(
                    success=True,
                    provider_id=provider_id,
                    route_id=sync_record.ailb_route_id,
                    status=SyncStatus.SYNCED,
                    message="Config unchanged"
                )

            # Sync to AILB
            if self.ailb_client and self.ailb_client.is_connected():
                result = self.ailb_client.update_routes(
                    routes=[route],
                    instance_id=self._instance_id
                )
                if not result.get("success"):
                    raise Exception(result.get("message", "Unknown error"))

            # Update or create sync record
            route_id = route.route_id
            now = datetime.utcnow()

            if sync_record:
                db(db.marchproxy_ailb_sync.id == sync_record.id).update(
                    ailb_route_id=route_id,
                    sync_status="synced",
                    last_synced=now,
                    config_hash=config_hash,
                    sync_error=None
                )
            else:
                db.marchproxy_ailb_sync.insert(
                    provider_id=provider_id,
                    ailb_instance_id=self._instance_id,
                    ailb_route_id=route_id,
                    sync_status="synced",
                    last_synced=now,
                    config_hash=config_hash
                )

            db.commit()

            logger.info(f"Successfully synced provider {provider_id} to AILB")
            return SyncResult(
                success=True,
                provider_id=provider_id,
                route_id=route_id,
                status=SyncStatus.SYNCED,
                message="Provider synced successfully"
            )

        except Exception as e:
            logger.error(f"Failed to sync provider {provider_id}: {e}")

            # Update sync record with error
            if sync_record:
                db(db.marchproxy_ailb_sync.id == sync_record.id).update(
                    sync_status="failed",
                    sync_error=str(e)
                )
            else:
                db.marchproxy_ailb_sync.insert(
                    provider_id=provider_id,
                    ailb_instance_id=self._instance_id,
                    sync_status="failed",
                    sync_error=str(e)
                )

            db.commit()

            return SyncResult(
                success=False,
                provider_id=provider_id,
                status=SyncStatus.FAILED,
                error=str(e)
            )

    def sync_all_providers(self) -> Dict[int, SyncResult]:
        """
        Sync all enabled providers to AILB.

        Returns:
            Dict mapping provider_id to SyncResult
        """
        db = self.db
        results = {}

        # Get all enabled providers with AILB sync enabled
        providers = db(
            (db.ai_providers.enabled == True) &
            (db.ai_providers.ailb_sync_enabled == True)
        ).select()

        for provider in providers:
            results[provider.id] = self.sync_provider(provider.id)

        return results

    def remove_provider(self, provider_id: int) -> bool:
        """
        Remove a provider route from AILB.

        Args:
            provider_id: ID of the provider to remove

        Returns:
            True if successful
        """
        db = self.db

        sync_record = db(db.marchproxy_ailb_sync.provider_id == provider_id).select().first()
        if not sync_record:
            return True  # Nothing to remove

        try:
            # Delete route from AILB
            if self.ailb_client and self.ailb_client.is_connected():
                if sync_record.ailb_route_id:
                    self.ailb_client.delete_route(
                        route_id=sync_record.ailb_route_id,
                        instance_id=self._instance_id
                    )

            # Update sync record
            db(db.marchproxy_ailb_sync.id == sync_record.id).update(
                sync_status="deleted"
            )
            db.commit()

            logger.info(f"Removed provider {provider_id} from AILB")
            return True

        except Exception as e:
            logger.error(f"Failed to remove provider {provider_id}: {e}")
            return False

    def verify_sync_status(self, provider_id: int) -> Dict[str, Any]:
        """
        Verify provider sync status with AILB.

        Args:
            provider_id: ID of the provider to verify

        Returns:
            Dict with sync status details
        """
        db = self.db

        sync_record = db(db.marchproxy_ailb_sync.provider_id == provider_id).select().first()
        if not sync_record:
            return {
                "synced": False,
                "status": "not_found",
                "message": "No sync record found"
            }

        provider = db(db.ai_providers.id == provider_id).select().first()
        if not provider:
            return {
                "synced": False,
                "status": "provider_not_found",
                "message": "Provider not found"
            }

        # Check config hash
        current_hash = self._generate_config_hash(provider)
        config_changed = sync_record.config_hash != current_hash

        return {
            "synced": sync_record.sync_status == "synced",
            "status": sync_record.sync_status,
            "route_id": sync_record.ailb_route_id,
            "last_synced": sync_record.last_synced.isoformat() if sync_record.last_synced else None,
            "config_changed": config_changed,
            "sync_error": sync_record.sync_error,
            "needs_resync": config_changed or sync_record.sync_status != "synced"
        }

    def get_pending_syncs(self) -> List[int]:
        """Get list of provider IDs that need syncing"""
        db = self.db

        # Providers without sync record
        synced_ids = [r.provider_id for r in db(db.marchproxy_ailb_sync.id > 0).select()]
        all_providers = db(
            (db.ai_providers.enabled == True) &
            (db.ai_providers.ailb_sync_enabled == True)
        ).select()

        pending = []
        for provider in all_providers:
            if provider.id not in synced_ids:
                pending.append(provider.id)
                continue

            # Check if config changed
            sync_record = db(db.marchproxy_ailb_sync.provider_id == provider.id).select().first()
            if sync_record:
                current_hash = self._generate_config_hash(provider)
                if sync_record.config_hash != current_hash or sync_record.sync_status != "synced":
                    pending.append(provider.id)

        return pending

    def sync_virtual_key(self, key_id: int) -> bool:
        """
        Sync a virtual key's rate limits to AILB.

        Args:
            key_id: ID of the virtual key

        Returns:
            True if successful
        """
        db = self.db

        key = db(db.virtual_keys.id == key_id).select().first()
        if not key:
            return False

        try:
            if self.ailb_client and self.ailb_client.is_connected():
                rate_limit = RateLimitConfig(
                    limit_id=f"waddleai-key-{key_id}",
                    target=key.key_prefix,
                    requests_per_minute=key.rpm_limit or 60,
                    burst_size=max(10, (key.rpm_limit or 60) // 6),
                    enabled=key.enabled,
                    metadata={
                        "waddleai_key_id": str(key_id),
                        "tpm_limit": str(key.tpm_limit or 10000),
                        "user_id": str(key.user_id),
                        "organization_id": str(key.organization_id)
                    }
                )
                self.ailb_client.set_rate_limit(rate_limit, self._instance_id)

            # Update sync status
            db(db.virtual_keys.id == key_id).update(
                ailb_sync_status="synced"
            )
            db.commit()

            logger.info(f"Synced virtual key {key_id} to AILB")
            return True

        except Exception as e:
            logger.error(f"Failed to sync virtual key {key_id}: {e}")
            db(db.virtual_keys.id == key_id).update(
                ailb_sync_status="failed"
            )
            db.commit()
            return False

    def sync_all_virtual_keys(self) -> Dict[int, bool]:
        """Sync all enabled virtual keys to AILB"""
        db = self.db
        results = {}

        keys = db(db.virtual_keys.enabled == True).select()
        for key in keys:
            results[key.id] = self.sync_virtual_key(key.id)

        return results
