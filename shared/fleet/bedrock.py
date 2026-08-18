"""``BedrockFleetBackend`` — AWS Bedrock cloud backend (spec §10.1, plan Task 11).

Professional-gated cloud backend built on ``boto3`` (Apache-2.0), already a
hash-pinned dependency of every service in this repo (see
``shared.utils.llm_connectors.BedrockConnector`` for the existing
runtime/inference client this backend's sibling module uses) — no new
dependency was added. All blocking ``boto3`` calls run via
``asyncio.to_thread`` so the event loop is never blocked (§10.1).

Per-backend ``management_scope`` (set by the registry from the owning
``fleet_backends`` row, per admin choice — Q#9):

- ``register_and_route``: read-only against Bedrock's control plane — list,
  health-check, and route to provisioned throughputs an org already manages
  outside WaddleAI. ``provision``/``deprovision`` are refused.
- ``full_lifecycle``: WaddleAI owns the lifecycle — ``provision`` calls
  ``create_provisioned_model_throughput``, ``deprovision`` calls
  ``delete_provisioned_model_throughput``. Idle-teardown wiring (plan
  Task 9) is a separate, not-yet-landed controller; this backend exposes
  the ``deprovision``/``provision`` primitives that controller will call,
  but does not itself run an idle sweep.

Cloud endpoints surface as ``NodeInfo(kind="cloud")`` and are counted as
managed nodes for Pro metering (§2.4) by the caller that aggregates
``list_nodes()`` across backends — this module only reports them.
"""

import asyncio
import json
import logging
from typing import Any

import boto3

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
_INSERVICE_STATUS = "InService"


def _fleet_v2_enabled(org_id: Any) -> bool:
    """Fail-safe-OFF check of the ``waddleai.fleet_v2`` PostHog flag."""
    try:
        return is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id or "server"), default=False)
    except Exception as exc:  # pragma: no cover - is_feature_enabled already self-guards
        logger.warning("Feature flag %s evaluation failed, treating as OFF: %s", _FLAG_KEY, exc)
        return False


@register(BackendType.BEDROCK)
class BedrockFleetBackend(InferenceFleetBackend):
    """Manages/routes to AWS Bedrock provisioned-throughput capacity.

    Constructed by ``shared.fleet.registry.build_backend``: ``credentials``
    (decrypted ``credentials_ref``), when present, must be a JSON object
    with ``aws_access_key_id``/``aws_secret_access_key`` (and optionally
    ``aws_session_token``) — the provider-credential pattern, never an env
    var. When absent, boto3's own default credential chain applies (e.g. an
    IRSA/instance-profile role already attached to this workload) — that is
    boto3's ambient resolution, not a WaddleAI-invented fallback.
    """

    type = BackendType.BEDROCK
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
        self.region = self.config.get("region", "us-east-1")
        self._aws_creds = self._parse_credentials(credentials)
        self._org_id = self.config.get("org_id")
        self._client: Any = None
        self._client_lock = asyncio.Lock()

    @staticmethod
    def _parse_credentials(credentials: str | None) -> dict[str, str] | None:
        """Parse the decrypted ``credentials_ref`` JSON into boto3 kwargs.

        Returns ``None`` when no explicit credential is configured, meaning
        "use boto3's ambient credential chain" rather than an error.
        """
        if not credentials:
            return None
        try:
            data = json.loads(credentials)
        except (TypeError, ValueError) as exc:
            raise ValueError("Bedrock credentials must be a JSON object") from exc
        if "aws_access_key_id" not in data or "aws_secret_access_key" not in data:
            raise ValueError(
                "Bedrock credentials JSON missing 'aws_access_key_id'/'aws_secret_access_key'"
            )
        return data

    def _build_client(self, service_name: str) -> Any:
        """Construct a boto3 client for ``service_name`` (blocking; call via to_thread)."""
        kwargs: dict[str, Any] = {"region_name": self.region}
        if self._aws_creds:
            kwargs["aws_access_key_id"] = self._aws_creds["aws_access_key_id"]
            kwargs["aws_secret_access_key"] = self._aws_creds["aws_secret_access_key"]
            if self._aws_creds.get("aws_session_token"):
                kwargs["aws_session_token"] = self._aws_creds["aws_session_token"]
        return boto3.client(service_name, **kwargs)

    async def _get_client(self) -> Any:
        """Lazily construct and cache the ``bedrock`` control-plane client off the event loop."""
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = await asyncio.to_thread(self._build_client, "bedrock")
        return self._client

    @staticmethod
    def _node_info_from_summary(summary: dict[str, Any]) -> NodeInfo:
        """Map a ``provisionedModelSummaries[]`` entry to a ``NodeInfo``."""
        status = summary.get("status", "")
        return NodeInfo(
            node_id=summary.get("provisionedModelName", summary.get("provisionedModelArn", "")),
            node_uid=summary.get("provisionedModelArn"),
            kind="cloud",
            loaded_models=[summary["modelId"]] if summary.get("modelId") else [],
            vram_total_mb=0,
            vram_free_mb=0,
            healthy=status == _INSERVICE_STATUS,
        )

    async def list_nodes(self) -> list[NodeInfo]:
        """List every provisioned-throughput model as a ``NodeInfo`` (both scopes)."""
        client = await self._get_client()

        def _list() -> list[dict[str, Any]]:
            response = client.list_provisioned_model_throughputs()
            return list(response.get("provisionedModelSummaries", []))

        summaries = await asyncio.to_thread(_list)
        return [self._node_info_from_summary(s) for s in summaries]

    async def provision(self, spec: ProvisionSpec) -> list[NodeInfo]:
        """Create provisioned throughput for a model (``full_lifecycle`` only)."""
        if not _fleet_v2_enabled(self._org_id):
            raise RuntimeError(f"{_FLAG_KEY} is disabled; Bedrock provisioning is gated off")
        if self.management_scope != ManagementScope.FULL_LIFECYCLE:
            raise PermissionError(
                f"Bedrock backend management_scope={self.management_scope.value}; "
                "provisioning requires full_lifecycle"
            )
        model_id = spec.constraints.get("model_id")
        if not model_id:
            raise ValueError("Bedrock provision requires constraints['model_id']")
        model_units = int(spec.constraints.get("model_units", 1))
        commitment_duration = spec.constraints.get("commitment_duration")

        client = await self._get_client()

        def _create() -> dict[str, Any]:
            kwargs: dict[str, Any] = {
                "modelUnits": model_units,
                "provisionedModelName": spec.name,
                "modelId": model_id,
            }
            if commitment_duration:
                kwargs["commitmentDuration"] = commitment_duration
            return client.create_provisioned_model_throughput(**kwargs)

        response = await asyncio.to_thread(_create)
        arn = response.get("provisionedModelArn")
        if not arn:
            raise RuntimeError("Bedrock create_provisioned_model_throughput returned no ARN")
        return [
            NodeInfo(
                node_id=spec.name,
                node_uid=arn,
                kind="cloud",
                loaded_models=[model_id],
                vram_total_mb=0,
                vram_free_mb=0,
                # Newly created capacity is not InService yet — callers poll
                # health()/list_nodes() for readiness rather than assuming it here.
                healthy=False,
            )
        ]

    async def deprovision(self, node_id: str) -> None:
        """Delete provisioned throughput named ``node_id`` (``full_lifecycle`` only).

        No-op if it no longer exists.
        """
        if self.management_scope != ManagementScope.FULL_LIFECYCLE:
            raise PermissionError(
                f"Bedrock backend management_scope={self.management_scope.value}; "
                "deprovisioning requires full_lifecycle"
            )
        client = await self._get_client()

        def _delete() -> None:
            try:
                client.delete_provisioned_model_throughput(provisionedModelId=node_id)
            except client.exceptions.ResourceNotFoundException:
                logger.info("Bedrock provisioned model %s already gone (no-op)", node_id)

        await asyncio.to_thread(_delete)

    async def health(self) -> FleetHealth:
        """Aggregate health across every tracked provisioned-throughput model."""
        try:
            nodes = await self.list_nodes()
        except Exception as exc:
            logger.warning("Bedrock health check failed: %s", exc)
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
        """Return the healthy provisioned-throughput node currently serving ``model``."""
        nodes = await self.list_nodes()
        for node in nodes:
            if model in node.loaded_models and node.healthy:
                return ModelPlacement(model=model, node_id=node.node_id, status="placed")
        raise RuntimeError(f"No Bedrock provisioned throughput currently serving model {model!r}")

    async def endpoints_for(self, model: str) -> list[Endpoint]:
        """Return provisioned-throughput nodes serving ``model`` (ARN as ``Endpoint.url``)."""
        nodes = await self.list_nodes()
        return [
            Endpoint(
                url=n.node_uid or n.node_id,
                node_id=n.node_id,
                loaded_models=n.loaded_models,
                healthy=n.healthy,
            )
            for n in nodes
            if model in n.loaded_models
        ]
