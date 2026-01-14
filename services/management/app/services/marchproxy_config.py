"""
WaddleAI MarchProxy Configuration Generator

Generates MarchProxy-compatible import configurations for:
- AI provider routes (OpenAI, Anthropic, etc.)
- Ollama model-specific routing
- Virtual key rate limits
"""

import json
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class MarchProxyConfigGenerator:
    """
    Generates MarchProxy AILB import configurations.

    Output format is compatible with MarchProxy's service import API:
    POST /api/v1/services/import
    """

    def __init__(self, db):
        self.db = db

    def generate_full_config(self) -> Dict[str, Any]:
        """
        Generate complete MarchProxy AILB configuration.

        Returns JSON-compatible dict for import API.
        """
        config = {
            "version": "1.0",
            "generated_at": datetime.utcnow().isoformat(),
            "managed_by": "waddleai",
            "ailb": {
                "module_type": "AILB",
                "providers": self._generate_providers(),
                "routes": self._generate_routes(),
                "rate_limits": self._generate_rate_limits(),
                "virtual_keys": self._generate_virtual_keys()
            }
        }

        return config

    def _generate_providers(self) -> List[Dict[str, Any]]:
        """Generate provider configurations"""
        db = self.db
        providers = db(
            (db.ai_providers.enabled == True) &
            (db.ai_providers.ailb_sync_enabled == True)
        ).select()

        provider_list = []

        for provider in providers:
            provider_config = {
                "id": f"waddleai-{provider.id}",
                "name": provider.name,
                "type": provider.provider_type,
                "endpoint": provider.endpoint_url,
                "priority": provider.priority or 100,
                "enabled": provider.enabled,
                "metadata": {
                    "waddleai_provider_id": str(provider.id),
                    "managed_by": "waddleai"
                }
            }

            # Add API key if present
            if provider.api_key:
                provider_config["auth"] = {
                    "type": "api_key",
                    "key": provider.api_key
                }

            # Add model list
            if provider.model_list:
                provider_config["models"] = provider.model_list

            # Add rate limits
            if provider.rate_limits:
                provider_config["rate_limits"] = provider.rate_limits

            provider_list.append(provider_config)

        return provider_list

    def _generate_routes(self) -> List[Dict[str, Any]]:
        """Generate all routing rules"""
        routes = []

        # Add standard provider routes
        routes.extend(self._generate_provider_routes())

        # Add Ollama model-specific routes
        routes.extend(self._generate_ollama_model_routes())

        return routes

    def _generate_provider_routes(self) -> List[Dict[str, Any]]:
        """Generate routes for standard AI providers"""
        db = self.db
        providers = db(
            (db.ai_providers.enabled == True) &
            (db.ai_providers.ailb_sync_enabled == True) &
            (db.ai_providers.provider_type != 'ollama')
        ).select()

        routes = []

        for provider in providers:
            route = {
                "id": f"waddleai-provider-{provider.id}",
                "name": f"{provider.name} Route",
                "protocol": "https" if provider.endpoint_url.startswith("https") else "http",
                "path_pattern": "/v1/*",
                "priority": provider.priority or 100,
                "destination": {
                    "type": "provider",
                    "provider_id": f"waddleai-{provider.id}",
                    "endpoint": provider.endpoint_url
                },
                "metadata": {
                    "waddleai_provider_id": str(provider.id),
                    "provider_type": provider.provider_type
                }
            }

            # Add header-based routing for specific providers
            if provider.provider_type == "anthropic":
                route["match_headers"] = {
                    "anthropic-version": "*"
                }
            elif provider.provider_type == "openai":
                route["match_headers"] = {
                    "Authorization": "Bearer *"
                }

            routes.append(route)

        return routes

    def _generate_ollama_model_routes(self) -> List[Dict[str, Any]]:
        """
        Generate model-specific routes for Ollama deployments.

        Each model gets its own route with header/body matching.
        """
        db = self.db
        routes = []

        # Get all active Ollama deployments
        deployments = db(
            db.ollama_deployments.status.belongs(['running', 'pending'])
        ).select()

        for deployment in deployments:
            # Get all models on this deployment
            models = db(db.ollama_models.deployment_id == deployment.id).select()

            for model in models:
                route = {
                    "id": f"ollama-{deployment.id}-{model.model_name}",
                    "name": f"Ollama {model.model_name} on {deployment.name}",
                    "protocol": "http" if deployment.endpoint_url.startswith("http://") else "https",
                    "path_pattern": "/v1/chat/completions",
                    "priority": 200,  # Higher priority than generic routes
                    "destination": {
                        "type": "ollama",
                        "deployment_id": deployment.id,
                        "endpoint": deployment.endpoint_url
                    },
                    "match_conditions": {
                        # Match on model name in request body
                        "body_json": {
                            "model": model.model_name
                        },
                        # Or match on custom header
                        "headers": {
                            "X-Ollama-Model": model.model_name
                        }
                    },
                    "metadata": {
                        "waddleai_deployment_id": str(deployment.id),
                        "waddleai_model_id": str(model.id),
                        "model_name": model.model_name,
                        "model_tag": model.model_tag or "latest",
                        "deployment_name": deployment.name,
                        "routing_type": "model-specific"
                    }
                }

                routes.append(route)

        return routes

    def _generate_rate_limits(self) -> List[Dict[str, Any]]:
        """Generate rate limit configurations"""
        db = self.db
        limits = []

        # Get all enabled virtual keys with rate limits
        keys = db(db.virtual_keys.enabled == True).select()

        for key in keys:
            if key.rpm_limit or key.tpm_limit:
                limit = {
                    "id": f"waddleai-key-{key.id}",
                    "target": key.key_prefix or f"wa-{key.id}",
                    "type": "virtual_key",
                    "limits": {},
                    "metadata": {
                        "waddleai_key_id": str(key.id),
                        "user_id": str(key.user_id),
                        "organization_id": str(key.organization_id)
                    }
                }

                if key.rpm_limit:
                    limit["limits"]["requests_per_minute"] = key.rpm_limit
                    limit["limits"]["burst_size"] = max(10, key.rpm_limit // 6)

                if key.tpm_limit:
                    limit["limits"]["tokens_per_minute"] = key.tpm_limit

                if key.budget_limit_daily:
                    limit["limits"]["cost_per_day_usd"] = key.budget_limit_daily

                if key.budget_limit_monthly:
                    limit["limits"]["cost_per_month_usd"] = key.budget_limit_monthly

                limits.append(limit)

        return limits

    def _generate_virtual_keys(self) -> List[Dict[str, Any]]:
        """Generate virtual key configurations"""
        db = self.db
        keys_list = []

        keys = db(db.virtual_keys.enabled == True).select()

        for key in keys:
            key_config = {
                "id": f"waddleai-key-{key.id}",
                "name": key.name,
                "key_prefix": key.key_prefix or f"wa-{key.id}",
                "enabled": key.enabled,
                "metadata": {
                    "waddleai_key_id": str(key.id),
                    "user_id": str(key.user_id),
                    "organization_id": str(key.organization_id)
                }
            }

            # Add allowed models/providers
            if key.allowed_models:
                key_config["allowed_models"] = key.allowed_models

            if key.allowed_providers:
                key_config["allowed_providers"] = key.allowed_providers

            # Add budget limits
            if key.budget_limit_daily or key.budget_limit_monthly:
                key_config["budget_limits"] = {}
                if key.budget_limit_daily:
                    key_config["budget_limits"]["daily_usd"] = key.budget_limit_daily
                if key.budget_limit_monthly:
                    key_config["budget_limits"]["monthly_usd"] = key.budget_limit_monthly

            # Add expiration
            if key.expires_at:
                key_config["expires_at"] = key.expires_at.isoformat()

            keys_list.append(key_config)

        return keys_list

    def generate_ollama_routing_table(self) -> Dict[str, str]:
        """
        Generate Ollama model-to-endpoint routing table.

        Returns a simple mapping of model names to endpoints for
        use in routing decisions.

        Example:
        {
            "llama3.2": "http://node-1:11434",
            "mistral": "http://node-2:11434",
            "codellama": "http://node-1:11434"
        }
        """
        db = self.db
        routing_table = {}

        deployments = db(
            db.ollama_deployments.status.belongs(['running', 'pending'])
        ).select()

        for deployment in deployments:
            models = db(db.ollama_models.deployment_id == deployment.id).select()

            for model in models:
                # Use full model name with tag
                model_key = f"{model.model_name}:{model.model_tag}" if model.model_tag else model.model_name
                routing_table[model_key] = deployment.endpoint_url

        return routing_table

    def export_to_file(self, output_path: str) -> bool:
        """
        Export MarchProxy configuration to JSON file.

        Args:
            output_path: Path to write JSON config

        Returns:
            True if successful
        """
        try:
            config = self.generate_full_config()

            with open(output_path, 'w') as f:
                json.dump(config, f, indent=2)

            logger.info(f"Exported MarchProxy config to {output_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to export config: {e}")
            return False

    def generate_model_routing_config(self) -> Dict[str, Any]:
        """
        Generate model-aware routing configuration for MarchProxy.

        This includes routing rules that inspect the request body
        to determine which Ollama deployment to route to based on
        the requested model.

        Example routing logic:
        - Request with {"model": "llama3.2"} → Route to deployment with llama3.2
        - Request with {"model": "mistral"} → Route to deployment with mistral
        """
        db = self.db

        # Build model-to-deployment mapping
        model_routes = {}

        deployments = db(
            db.ollama_deployments.status.belongs(['running', 'pending'])
        ).select()

        for deployment in deployments:
            models = db(db.ollama_models.deployment_id == deployment.id).select()

            for model in models:
                model_key = model.model_name
                if model_key not in model_routes:
                    model_routes[model_key] = []

                model_routes[model_key].append({
                    "deployment_id": deployment.id,
                    "deployment_name": deployment.name,
                    "endpoint": deployment.endpoint_url,
                    "model_id": model.id,
                    "model_tag": model.model_tag or "latest",
                    "priority": 1  # Could be based on deployment health, load, etc.
                })

        # Generate routing config
        routing_config = {
            "version": "1.0",
            "routing_strategy": "model-aware",
            "model_routes": model_routes,
            "fallback_strategy": "round_robin",
            "health_check": {
                "enabled": True,
                "interval_seconds": 30,
                "unhealthy_threshold": 3
            }
        }

        return routing_config
