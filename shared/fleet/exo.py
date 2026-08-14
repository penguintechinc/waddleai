"""``ExoFleetBackend`` — external-only API plugin (spec §10.1, plan Task 8).

EXO (github.com/exo-explore/exo) is GPLv3-licensed. To stay on the correct
side of that license's network boundary, **no EXO source is vendored,
imported, or otherwise linked anywhere in this codebase** — this backend is
a pure HTTP client (``aiohttp``, already a pinned dependency) against an
already-running EXO cluster's OpenAI-compatible API. WaddleAI never builds,
packages, or distributes EXO; it only sends requests to an endpoint an org
admin has already stood up and registered, exactly like any other external
inference server. That network-only boundary is why a GPLv3 dependency is
tolerable here per house policy (Apache-2.0/MIT preferred, GPL-family
tolerable for non-linked services) where it would not be for an in-process
library dependency.

EXO manages its own peer-to-peer device topology internally and does not
expose a stable "list constituent devices" API on its OpenAI-compatible
surface, so this backend treats one registered cluster endpoint as **one**
routable ``NodeInfo`` — one logical node per registered EXO cluster, not one
per physical device in that cluster. ``management_scope`` is always
``register_and_route``: WaddleAI never provisions or tears down EXO
clusters, only routes to and health-checks an endpoint an admin already
runs (§10.1's ``ManagementScope`` contract).
"""

import logging
from typing import Any

import aiohttp

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
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _fleet_v2_enabled(org_id: Any) -> bool:
    """Fail-safe-OFF check of the ``waddleai.fleet_v2`` PostHog flag.

    Gates capacity-creating actions only (``provision``) — health/list/route
    calls against an already-registered backend stay available even if the
    flag flips off later, matching the fail-safe-degrade house rule.
    """
    try:
        return is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id or "server"), default=False)
    except Exception as exc:  # pragma: no cover - is_feature_enabled already self-guards
        logger.warning("Feature flag %s evaluation failed, treating as OFF: %s", _FLAG_KEY, exc)
        return False


@register(BackendType.EXO)
class ExoFleetBackend(InferenceFleetBackend):
    """Routes to and health-checks an externally-run EXO cluster.

    Constructed by ``shared.fleet.registry.build_backend`` from a
    ``fleet_backends`` row: ``config`` must carry ``endpoint_url`` (the
    cluster's OpenAI-compatible base URL); ``credentials``, if present, is
    used as a bearer token on every request. Both come from the
    provider-credential pattern (``credentials_ref`` decrypted by the
    registry) — this class never reads environment variables for secrets.
    """

    type = BackendType.EXO
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
        # EXO is never WaddleAI-lifecycled — forced regardless of the row's
        # stored management_scope, since provisioning an EXO cluster is
        # meaningless (§10.1).
        self.management_scope = ManagementScope.REGISTER_AND_ROUTE
        self._org_id = self.config.get("org_id")

    @property
    def _base_url(self) -> str:
        """The registered EXO cluster's base URL, or raise if unconfigured."""
        base_url = self.config.get("endpoint_url")
        if not base_url:
            raise ValueError("EXO backend config missing required 'endpoint_url'")
        return str(base_url).rstrip("/")

    @property
    def _node_id(self) -> str:
        """A stable identifier for this cluster registration."""
        return str(self.config.get("name") or self._base_url)

    def _headers(self) -> dict[str, str]:
        """Bearer-auth header when a credential is configured, else empty."""
        if self.credentials:
            return {"Authorization": f"Bearer {self.credentials}"}
        return {}

    async def _list_models(self) -> list[str]:
        """List model ids the cluster currently reports as served.

        Calls the OpenAI-compatible ``/v1/models`` endpoint (path
        overridable via ``config['models_path']`` for clusters fronted by a
        gateway that renames it). Raises on any non-200 or transport error —
        callers decide whether that means "unhealthy" or "fail the call".
        """
        path = self.config.get("models_path", "/v1/models")
        url = f"{self._base_url}{path}"
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            async with session.get(url, timeout=_REQUEST_TIMEOUT) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"EXO cluster {self._base_url} {path} returned HTTP {response.status}"
                    )
                data = await response.json()
                return [m["id"] for m in data.get("data", []) if m.get("id")]

    async def provision(self, spec: ProvisionSpec) -> list[NodeInfo]:
        """Validate the already-configured EXO endpoint and return it as one node.

        This does not create anything — EXO clusters are provisioned by
        their own operators, never by WaddleAI. "Provisioning" here means
        confirming the registered endpoint answers, so the API-layer create
        flow (Task 12, out of scope here) fails fast on a dead registration
        instead of silently accepting it.
        """
        if not _fleet_v2_enabled(self._org_id):
            raise RuntimeError(f"{_FLAG_KEY} is disabled; EXO backend registration is gated off")
        try:
            models = await self._list_models()
        except Exception as exc:
            raise RuntimeError(f"EXO endpoint {self._base_url} unreachable: {exc}") from exc
        return [
            NodeInfo(
                node_id=spec.name or self._node_id,
                node_uid=None,
                kind="external",
                loaded_models=models,
                vram_total_mb=0,
                vram_free_mb=0,
                healthy=True,
            )
        ]

    async def deprovision(self, node_id: str) -> None:
        """Deregister the endpoint. No-op — WaddleAI holds no EXO-side state to tear down."""
        logger.info(
            "EXO backend deregister requested for node_id=%s (no-op, external cluster)", node_id
        )
        return None

    async def health(self) -> FleetHealth:
        """Health-check the registered EXO endpoint."""
        try:
            models = await self._list_models()
        except Exception as exc:
            logger.warning("EXO health check failed for %s: %s", self._base_url, exc)
            return FleetHealth(
                backend_id=self.fleet_backend_id,
                healthy=False,
                node_count=1,
                detail={"healthy_nodes": 0, "node_id": self._node_id, "error": str(exc)},
            )
        return FleetHealth(
            backend_id=self.fleet_backend_id,
            healthy=True,
            node_count=1,
            detail={"healthy_nodes": 1, "node_id": self._node_id, "loaded_models": models},
        )

    async def list_nodes(self) -> list[NodeInfo]:
        """Return the single logical node this EXO cluster registration represents."""
        try:
            models = await self._list_models()
            healthy = True
        except Exception as exc:
            logger.warning("EXO list_nodes failed for %s: %s", self._base_url, exc)
            models = []
            healthy = False
        return [
            NodeInfo(
                node_id=self._node_id,
                node_uid=None,
                kind="external",
                loaded_models=models,
                vram_total_mb=0,
                vram_free_mb=0,
                healthy=healthy,
            )
        ]

    async def place_model(self, model: str, constraints: dict[str, Any]) -> ModelPlacement:
        """Confirm ``model`` is currently served by the EXO cluster.

        EXO handles its own model loading internally; WaddleAI cannot push a
        model onto it, only check availability — consistent with
        ``register_and_route`` semantics.
        """
        models = await self._list_models()
        if model not in models:
            raise RuntimeError(
                f"EXO cluster {self._base_url} is not currently serving model {model!r}"
            )
        return ModelPlacement(model=model, node_id=self._node_id, status="placed")

    async def endpoints_for(self, model: str) -> list[Endpoint]:
        """Return the cluster endpoint if it currently serves ``model``, else empty."""
        try:
            models = await self._list_models()
        except Exception as exc:
            logger.warning("EXO endpoints_for failed for %s: %s", self._base_url, exc)
            return []
        if model not in models:
            return []
        return [
            Endpoint(url=self._base_url, node_id=self._node_id, loaded_models=models, healthy=True)
        ]
