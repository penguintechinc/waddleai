"""``VertexAIFleetBackend`` — Google Vertex AI cloud backend (spec §10.1, plan Task 10).

Professional-gated cloud backend against the Vertex AI REST API
(``aiplatform.googleapis.com``) via ``httpx`` — already a hash-pinned
dependency of every service in this repo. The plan names
``google-cloud-aiplatform`` (Apache-2.0) as the reference SDK, but it pulls
a large transitive dependency tree (``google-cloud-storage``,
``google-cloud-resource-manager``, ``shapely``, ``proto-plus``, ...) for
functionality this backend does not need — creating/deleting endpoints and
listing/deploying models. Rather than adding that SDK and hash-pinning its
full tree, this backend talks to the same REST surface directly with
``httpx`` (async-native, already used throughout this service) plus
``PyJWT`` (also already pinned) to mint OAuth2 access tokens from a
service-account key via the standard JWT-bearer flow (RFC 7523) — no new
dependency was added for either piece.

Per-backend ``management_scope`` (set by the registry from the owning
``fleet_backends`` row, per admin choice — Q#9):

- ``register_and_route``: ``list_nodes``/``health``/``endpoints_for``/
  ``place_model`` reflect endpoints an org already deployed outside
  WaddleAI. ``provision``/``deprovision`` are refused.
- ``full_lifecycle``: ``provision`` creates a Vertex endpoint and deploys
  the requested model to it; ``deprovision`` undeploys every model on the
  endpoint and deletes it. Idle-teardown wiring (plan Task 9) is a separate,
  not-yet-landed controller; this backend exposes the primitives that
  controller will call, but does not itself run an idle sweep.

Cloud endpoints surface as ``NodeInfo(kind="cloud")`` and are counted as
managed nodes for Pro metering (§2.4) by the caller that aggregates
``list_nodes()`` across backends — this module only reports them.
"""

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import jwt

from shared.fleet.base import (
    BackendType,
    Endpoint,
    FleetHealth,
    InferenceFleetBackend,
    ManagementScope,
    ModelPlacement,
    NodeInfo,
    ProvisionSpec,
)
from shared.fleet.registry import register
from shared.utils.feature_flags import is_feature_enabled

logger = logging.getLogger(__name__)

_FLAG_KEY = "waddleai.fleet_v2"
_OAUTH_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_REQUIRED_SA_FIELDS = ("client_email", "private_key", "token_uri")
_TOKEN_EXPIRY_SAFETY_MARGIN_S = 60


def _fleet_v2_enabled(org_id: Any) -> bool:
    """Fail-safe-OFF check of the ``waddleai.fleet_v2`` PostHog flag."""
    try:
        return is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id or "server"), default=False)
    except Exception as exc:  # pragma: no cover - is_feature_enabled already self-guards
        logger.warning("Feature flag %s evaluation failed, treating as OFF: %s", _FLAG_KEY, exc)
        return False


@register(BackendType.VERTEX_AI)
class VertexAIFleetBackend(InferenceFleetBackend):
    """Manages/routes to Google Vertex AI prediction endpoints.

    Constructed by ``shared.fleet.registry.build_backend``: ``credentials``
    (decrypted ``credentials_ref``) must be a GCP service-account key JSON
    document; ``config`` carries ``project_id`` (falls back to the key's own
    ``project_id``) and ``location`` (default ``us-central1``). Both come
    from the provider-credential pattern — this class never reads
    environment variables for secrets.
    """

    type = BackendType.VERTEX_AI
    management_scope = ManagementScope.REGISTER_AND_ROUTE

    def __init__(
        self,
        db: Any,
        *,
        config: dict[str, Any] | None = None,
        credentials: str | None = None,
    ) -> None:
        """Bind to a ``fleet_backends`` row's config/credentials (registry contract)."""
        self.db = db
        self.config = config or {}
        self.credentials = credentials
        self._service_account = self._parse_service_account(credentials)
        self.project_id: str | None = self.config.get("project_id") or (
            self._service_account.get("project_id") if self._service_account else None
        )
        self.location = self.config.get("location", "us-central1")
        self._org_id = self.config.get("org_id")
        self._access_token: str | None = None
        self._token_expiry: float = 0.0
        self._token_lock = asyncio.Lock()

    @staticmethod
    def _parse_service_account(credentials: str | None) -> dict[str, Any] | None:
        """Parse the decrypted ``credentials_ref`` JSON into a service-account dict."""
        if not credentials:
            return None
        try:
            data = json.loads(credentials)
        except (TypeError, ValueError) as exc:
            raise ValueError("Vertex AI credentials must be a JSON service-account key") from exc
        missing = [field for field in _REQUIRED_SA_FIELDS if field not in data]
        if missing:
            raise ValueError(f"Vertex AI service-account credentials missing fields: {missing}")
        return data

    async def _get_access_token(self) -> str:
        """Mint (and cache) an OAuth2 access token via the JWT-bearer flow (RFC 7523)."""
        if self._access_token and time.time() < self._token_expiry - _TOKEN_EXPIRY_SAFETY_MARGIN_S:
            return self._access_token
        async with self._token_lock:
            token_still_valid = time.time() < self._token_expiry - _TOKEN_EXPIRY_SAFETY_MARGIN_S
            if self._access_token is not None and token_still_valid:
                return self._access_token
            if self._service_account is None:
                raise ValueError("Vertex AI backend has no credentials configured")
            sa = self._service_account
            now = int(time.time())
            claims = {
                "iss": sa["client_email"],
                "scope": _OAUTH_SCOPE,
                "aud": sa["token_uri"],
                "iat": now,
                "exp": now + 3600,
            }
            assertion = await asyncio.to_thread(
                jwt.encode, claims, sa["private_key"], algorithm="RS256"
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    sa["token_uri"],
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    },
                )
            if response.status_code != 200:
                raise RuntimeError(f"Vertex AI token exchange failed: HTTP {response.status_code}")
            payload = response.json()
            access_token: str = payload["access_token"]
            self._access_token = access_token
            self._token_expiry = time.time() + payload.get("expires_in", 3600)
            return access_token

    @property
    def _base_url(self) -> str:
        """The project/location-scoped Vertex AI REST base URL."""
        if not self.project_id:
            raise ValueError("Vertex AI backend config missing 'project_id'")
        return (
            f"https://{self.location}-aiplatform.googleapis.com/v1/"
            f"projects/{self.project_id}/locations/{self.location}"
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Issue an authenticated Vertex AI REST call."""
        token = await self._get_access_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            url = f"{self._base_url}{path}"
            return await client.request(method, url, headers=headers, **kwargs)

    async def list_nodes(self) -> list[NodeInfo]:
        """List deployed Vertex AI endpoints as ``NodeInfo`` (both scopes)."""
        response = await self._request("GET", "/endpoints")
        if response.status_code != 200:
            raise RuntimeError(f"Vertex AI list endpoints failed: HTTP {response.status_code}")
        data = response.json()
        nodes = []
        for endpoint in data.get("endpoints", []):
            deployed = endpoint.get("deployedModels", [])
            model_ids = [dm["model"].rsplit("/", 1)[-1] for dm in deployed if dm.get("model")]
            nodes.append(
                NodeInfo(
                    node_id=endpoint.get("name", "").rsplit("/", 1)[-1],
                    node_uid=endpoint.get("name"),
                    kind="cloud",
                    loaded_models=model_ids,
                    vram_total_mb=0,
                    vram_free_mb=0,
                    healthy=True,
                )
            )
        return nodes

    async def provision(self, spec: ProvisionSpec) -> list[NodeInfo]:
        """Create an endpoint and deploy the requested model (``full_lifecycle`` only)."""
        if not _fleet_v2_enabled(self._org_id):
            raise RuntimeError(f"{_FLAG_KEY} is disabled; Vertex AI provisioning is gated off")
        if self.management_scope != ManagementScope.FULL_LIFECYCLE:
            raise PermissionError(
                f"Vertex AI backend management_scope={self.management_scope.value}; "
                "provisioning requires full_lifecycle"
            )
        model_resource = spec.constraints.get("model")
        if not model_resource:
            raise ValueError(
                "Vertex AI provision requires constraints['model'] (model resource name)"
            )

        create_resp = await self._request("POST", "/endpoints", json={"displayName": spec.name})
        if create_resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Vertex AI endpoint creation failed: HTTP {create_resp.status_code}"
            )
        endpoint_name = create_resp.json().get("name")
        if not endpoint_name:
            raise RuntimeError("Vertex AI endpoint creation returned no resource name")
        endpoint_id = endpoint_name.rsplit("/", 1)[-1]

        deploy_resp = await self._request(
            "POST",
            f"/endpoints/{endpoint_id}:deployModel",
            json={"deployedModel": {"model": model_resource, "displayName": spec.name}},
        )
        if deploy_resp.status_code not in (200, 201):
            raise RuntimeError(f"Vertex AI model deploy failed: HTTP {deploy_resp.status_code}")

        return [
            NodeInfo(
                node_id=endpoint_id,
                node_uid=endpoint_name,
                kind="cloud",
                loaded_models=[model_resource.rsplit("/", 1)[-1]],
                vram_total_mb=0,
                vram_free_mb=0,
                healthy=True,
            )
        ]

    async def deprovision(self, node_id: str) -> None:
        """Undeploy every model on and delete the endpoint (``full_lifecycle`` only).

        No-op if the endpoint no longer exists.
        """
        if self.management_scope != ManagementScope.FULL_LIFECYCLE:
            raise PermissionError(
                f"Vertex AI backend management_scope={self.management_scope.value}; "
                "deprovisioning requires full_lifecycle"
            )
        get_resp = await self._request("GET", f"/endpoints/{node_id}")
        if get_resp.status_code == 404:
            return
        if get_resp.status_code != 200:
            raise RuntimeError(f"Vertex AI endpoint lookup failed: HTTP {get_resp.status_code}")

        for deployed in get_resp.json().get("deployedModels", []):
            undeploy_resp = await self._request(
                "POST",
                f"/endpoints/{node_id}:undeployModel",
                json={"deployedModelId": deployed.get("id")},
            )
            if undeploy_resp.status_code not in (200, 201):
                raise RuntimeError(f"Vertex AI undeploy failed: HTTP {undeploy_resp.status_code}")

        delete_resp = await self._request("DELETE", f"/endpoints/{node_id}")
        if delete_resp.status_code not in (200, 204):
            raise RuntimeError(f"Vertex AI endpoint delete failed: HTTP {delete_resp.status_code}")

    async def health(self) -> FleetHealth:
        """Aggregate health across every deployed Vertex AI endpoint."""
        try:
            nodes = await self.list_nodes()
        except Exception as exc:
            logger.warning("Vertex AI health check failed: %s", exc)
            return FleetHealth(
                backend_id=self.fleet_backend_id,
                healthy=False,
                node_count=0,
                detail={"error": str(exc)},
            )
        healthy_count = sum(1 for n in nodes if n.healthy)
        return FleetHealth(
            backend_id=self.fleet_backend_id,
            healthy=healthy_count == len(nodes),
            node_count=len(nodes),
            detail={"healthy_nodes": healthy_count},
        )

    async def place_model(self, model: str, constraints: dict[str, Any]) -> ModelPlacement:
        """Return the healthy endpoint currently serving ``model``."""
        nodes = await self.list_nodes()
        for node in nodes:
            if model in node.loaded_models and node.healthy:
                return ModelPlacement(model=model, node_id=node.node_id, status="placed")
        raise RuntimeError(f"No Vertex AI endpoint currently serving model {model!r}")

    async def endpoints_for(self, model: str) -> list[Endpoint]:
        """Return endpoints currently serving ``model``."""
        nodes = await self.list_nodes()
        return [
            Endpoint(
                url=f"{self._base_url}/endpoints/{n.node_id}",
                node_id=n.node_id,
                loaded_models=n.loaded_models,
                healthy=n.healthy,
            )
            for n in nodes
            if model in n.loaded_models
        ]
