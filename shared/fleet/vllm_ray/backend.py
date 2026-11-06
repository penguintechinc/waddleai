"""``VllmRayBackend``: vLLM-on-Ray-Serve GPU fleet backend (full lifecycle).

One Ray Serve application per ``vllm_deployments`` row. Every change is applied by
PUTting the FULL desired application set to the Ray head (Serve semantics), and the
row status is reconciled back from Serve's own status. Nothing here ever logs or
returns a credential: the Ray token lives only inside the HTTP client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

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
from shared.fleet.enums import DeploymentStatus, InventoryState
from shared.fleet.registry import register
from shared.fleet.vllm_ray.inventory import GpuRecord, InventoryCollector, upsert_inventory
from shared.fleet.vllm_ray.ray_client import HttpRayServeClient, RayClientError, RayServeClient
from shared.fleet.vllm_ray.serve_config import _ALLOWED_NCCL, build_serve_app

logger = logging.getLogger(__name__)

ENV_CONFIG_FILE = "WADDLEAI_VLLM_RAY_BACKEND_CONFIG"
LINK_AUTH_REFUSAL = "enable Cilium mutual auth (SPIRE) or Ray internal TLS to deploy across nodes"
_NODE_HEALTHY_WINDOW_S = 180.0
_HEALTH_PROBE_APP = "waddleai-health-probe"
_REJECTED = "Ray Serve rejected the deployment; see deployment events."
_REJECTED_OP = "Ray Serve rejected the request; see backend logs."
_UNSUBMITTED = (DeploymentStatus.PENDING, DeploymentStatus.PULLING)
S = DeploymentStatus


class LinkAuthRefused(RuntimeError):  # noqa: N818 - public name fixed by the spec
    """A multi-node layout was refused because no link-level authentication is enabled."""


_load_histogram: Any = None


def _observe_model_load_seconds(seconds: float) -> None:
    """Record ``waddleai_vllm_model_load_seconds`` on the process OTel meter (never raises)."""
    global _load_histogram
    try:
        if _load_histogram is None:
            from shared.observability.metrics import get_meter

            _load_histogram = get_meter().create_histogram(
                "waddleai_vllm_model_load_seconds",
                unit="s",
                description="Seconds from deployment start to first SERVING",
            )
        _load_histogram.record(seconds)
    except Exception:  # telemetry failure must never fail a reconcile
        logger.warning("could not record model load histogram", exc_info=True)


async def _default_canary(url: str, served_model: str) -> bool:
    """Probe ``/models`` for the served id, then a 4-token chat completion; True on both 200."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
            models = await http.get(f"{url}/models")
            if models.status_code != 200:
                return False
            ids = [m.get("id") for m in models.json().get("data", [])]
            if served_model not in ids:
                return False
            chat = await http.post(
                f"{url}/chat/completions",
                json={
                    "model": served_model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 4,
                },
            )
            return chat.status_code == 200
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


def _load_defaults() -> dict[str, Any]:
    """Load the chart-mounted ConfigMap JSON (AM9) named by env; ``{}`` when unset."""
    path = os.environ.get(ENV_CONFIG_FILE)
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise RuntimeError("vLLM Ray config file unreadable") from None
    if not isinstance(data, dict):
        raise RuntimeError("vLLM Ray config file unreadable")
    return data


def _ray_token(credentials: dict[str, str] | str | None) -> str | None:
    """Extract ``ray_token`` from a credentials dict, JSON string, or bare token string."""
    if isinstance(credentials, dict):
        return credentials.get("ray_token")
    if isinstance(credentials, str) and credentials.strip():
        try:
            parsed = json.loads(credentials)
        except ValueError:
            return credentials
        return parsed.get("ray_token") if isinstance(parsed, dict) else credentials
    return None


def _naive_utcnow() -> datetime:
    """Naive UTC now, matching the naive DateTime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


@register(BackendType.VLLM_RAY)
class VllmRayBackend(InferenceFleetBackend):
    """Manages vLLM deployments as Ray Serve applications on a Ray cluster."""

    type = BackendType.VLLM_RAY
    management_scope = ManagementScope.FULL_LIFECYCLE

    def __init__(
        self,
        db: Any,
        config: dict[str, Any],
        credentials: dict[str, str] | None,
        client: RayServeClient | None = None,
        *,
        canary: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> None:
        """Merge the AM9 defaults file under ``config`` (row wins) and validate required keys."""
        merged: dict[str, Any] = {**_load_defaults(), **(config or {})}
        for key in ("head_url", "serve_base_url"):
            if not merged.get(key):
                raise ValueError(f"vLLM Ray backend config requires {key}")
        merged.setdefault("model_store_path", "/models")
        merged.setdefault("class_map", {})
        merged.setdefault("kv_headroom_pct", 20)
        self.db = db
        self.config = merged
        self._ray_token = _ray_token(credentials)
        self._ray_client: RayServeClient | None = client
        self._canary = canary or _default_canary

    def __repr__(self) -> str:
        """Identify the backend without any credential."""
        return f"VllmRayBackend(head_url={self.config['head_url']}, id={self.fleet_backend_id})"

    def _client(self) -> RayServeClient:
        """Return the injected client, else build (once) the token-authenticated HTTP client."""
        if self._ray_client is None:
            if self._ray_token:
                self._ray_client = HttpRayServeClient(self.config["head_url"], self._ray_token)
            else:
                self._ray_client = HttpRayServeClient.from_env(self.config["head_url"])
        return self._ray_client

    @staticmethod
    def _weights_present(path: str) -> bool:
        """True when the model directory exists in the store (pulled by the HF downloader)."""
        return os.path.isdir(path)

    def _nccl_env(self) -> dict[str, str]:
        """NCCL tuning env for the Serve replicas: config value, else Helm-provided env."""
        nccl = self.config.get("nccl")
        if nccl is not None:
            return {str(k): str(v) for k, v in nccl.items()}
        return {k: os.environ[k] for k in sorted(_ALLOWED_NCCL) if k in os.environ}

    def _link_auth_ok(self) -> bool:
        """True when mutual auth or Ray internal TLS is enabled (AM11; defaults true/true)."""
        link = self.config.get("link_auth") or {}
        return bool(link.get("mutual_auth", True)) or bool(link.get("internal_tls", True))

    def _check_link_auth(self, dep: Any) -> None:
        """Refuse multi-node layouts with no link-level authentication (spec 6.3 layer 1)."""
        if (dep.pipeline_parallel or 1) > 1 and not self._link_auth_ok():
            raise LinkAuthRefused(LINK_AUTH_REFUSAL)

    async def _select(self, query_fn: Callable[[Any], Any]) -> list[Any]:
        """Run a blocking DAL select in a worker thread."""

        def _run() -> list[Any]:
            return list(self.db(query_fn(self.db)).select())

        return await asyncio.to_thread(_run)

    async def _backend_rows(self) -> list[Any]:
        """All deployment rows owned by this backend."""
        return await self._select(
            lambda db: db.vllm_deployments.fleet_backend_id == self.fleet_backend_id
        )

    async def _update(self, dep: Any, **values: Any) -> None:
        """Persist column changes (and ``updated_at``) and mirror them on ``dep``."""
        values["updated_at"] = _naive_utcnow()

        def _run() -> None:
            self.db(self.db.vllm_deployments.id == dep.id).update(**values)

        await asyncio.to_thread(_run)
        for key, value in values.items():
            setattr(dep, key, value)

    async def _event(self, dep: Any, state: int, message: str) -> None:
        """Append a ``vllm_deployment_events`` row (DAL insert commits itself)."""

        def _run() -> None:
            self.db.vllm_deployment_events.insert(
                deployment_id=dep.id, ts=_naive_utcnow(), state=int(state), message=message
            )

        await asyncio.to_thread(_run)

    async def _transition(
        self, dep: Any, new: DeploymentStatus, message: str, *, detail: str | None = ""
    ) -> bool:
        """Move ``dep`` to ``new`` with an event row; unchanged status only refreshes the detail."""
        shown = message if detail == "" else detail
        if dep.status == int(new):
            if shown is not None and shown != dep.status_detail:
                await self._update(dep, status_detail=shown)
            return False
        await self._update(dep, status=int(new), status_detail=shown)
        await self._event(dep, int(new), message)
        logger.info("vllm deployment %s -> %s", dep.serve_app_name, new.name)
        return True

    def _endpoint_url(self, dep: Any) -> str:
        """Route URL of a deployment under the Serve base."""
        return f"{str(self.config['serve_base_url']).rstrip('/')}/d/{dep.slug}/v1"

    async def _full_desired_set(
        self, target: Any | None, exclude: Any | None = None
    ) -> dict[str, Any]:
        """Serve body of every deployable row; ``exclude`` is dropped.

        Rows not yet submitted (PENDING/PULLING: weights may not exist) or FAILED are left
        out unless they are the ``target`` being deployed now.
        """
        store = str(self.config["model_store_path"])
        nccl = self._nccl_env()
        apps: list[dict[str, Any]] = []
        for row in await self._backend_rows():
            if exclude is not None and row.id == exclude.id:
                continue
            is_target = target is not None and row.id == target.id
            if not is_target and row.status in (int(S.PENDING), int(S.PULLING), int(S.FAILED)):
                continue
            apps.append(build_serve_app(row, f"{store}/{row.slug}", nccl))
        return {"applications": apps}

    async def deploy(self, dep: Any) -> None:
        """Deploy ``dep``: refuse unauthenticated multi-node, check weights, PUT the full set."""
        try:
            self._check_link_auth(dep)
        except LinkAuthRefused:
            await self._transition(dep, S.FAILED, LINK_AUTH_REFUSAL)
            raise
        path = f"{self.config['model_store_path']}/{dep.slug}"
        if not await asyncio.to_thread(self._weights_present, path):
            await self._transition(dep, S.FAILED, "weights not in model store")
            raise RuntimeError("weights not in model store")
        await self._update(dep, endpoint_url=self._endpoint_url(dep))
        await self._transition(dep, S.STARTING, "deploying")
        try:
            body = await self._full_desired_set(dep)
            await self._client().put_applications(body)
        except RayClientError as exc:
            await self._transition(dep, S.FAILED, str(exc))
            raise RuntimeError(_REJECTED) from None

    async def _put_after_change(self, dep: Any, *, exclude: Any | None = None) -> None:
        """PUT the full set after a stop/scale; Ray failures surface as a plain RuntimeError."""
        try:
            await self._client().put_applications(await self._full_desired_set(None, exclude))
        except RayClientError as exc:
            logger.warning("ray put failed for %s: %s", dep.serve_app_name, exc)
            raise RuntimeError(_REJECTED_OP) from None

    async def scale(self, dep: Any, replicas_min: int, replicas_max: int) -> None:
        """Update replica bounds and re-PUT the full set."""
        if replicas_min < 0 or replicas_max < 1 or replicas_min > replicas_max:
            raise ValueError("replica bounds must satisfy 0 <= min <= max and max >= 1")
        await self._update(dep, replicas_min=replicas_min, replicas_max=replicas_max)
        await self._put_after_change(dep)

    async def stop(self, dep: Any) -> None:
        """Mark STOPPED and re-PUT (min_replicas drops to 0; the app is kept)."""
        await self._transition(dep, S.STOPPED, "stopped")
        await self._put_after_change(dep)

    async def delete(self, dep: Any) -> None:
        """Remove the Serve app; the endpoint disappears immediately (Review Focus 4)."""
        await self._update(dep, endpoint_url=None)
        await self._transition(dep, S.STOPPED, "deleted")
        remaining = await self._full_desired_set(None, exclude=dep)
        try:
            await self._client().delete_application(dep.serve_app_name, remaining)
        except RayClientError as exc:
            logger.warning("ray delete failed for %s: %s", dep.serve_app_name, exc)
            raise RuntimeError(_REJECTED_OP) from None

    async def _run_canary(self, dep: Any) -> bool:
        """Run the readiness canary; transport errors count as failure, bugs propagate."""
        url = dep.endpoint_url or self._endpoint_url(dep)
        try:
            return bool(await self._canary(url, dep.served_model_name))
        except (RayClientError, httpx.HTTPError, TimeoutError, OSError):
            logger.warning("canary raised for %s", dep.serve_app_name, exc_info=True)
            return False

    async def _serving(self, dep: Any, message: str) -> None:
        """Transition to SERVING, observing the load-time histogram once."""
        started = await self._load_started_at(dep)
        if await self._transition(dep, S.SERVING, message, detail=None) and started is not None:
            _observe_model_load_seconds(max(0.0, (_naive_utcnow() - started).total_seconds()))

    async def _load_started_at(self, dep: Any) -> datetime | None:
        """Latest STARTING event timestamp, else the row creation time."""
        events = await self._select(lambda db: db.vllm_deployment_events.deployment_id == dep.id)
        starts = [e.ts for e in events if e.state == int(S.STARTING) and e.ts is not None]
        created: datetime | None = dep.created_at
        return max(starts) if starts else created

    async def reconcile(self, dep: Any) -> int:
        """Reconcile ``dep.status`` from Ray Serve's status; return the resulting status int."""
        info = await self._client().get_application(dep.serve_app_name)
        serve = str(info.get("status", ""))
        message = str(info.get("message") or "")
        cur = S(dep.status)
        if serve in ("DEPLOYING", "NOT_STARTED"):
            if cur not in (S.SERVING, S.DEGRADED, S.STOPPED):
                await self._transition(dep, S.STARTING, "deploying")
        elif serve == "RUNNING":
            await self._reconcile_running(dep, cur)
        elif serve == "DEPLOY_FAILED" or serve == "UNHEALTHY":
            text = message or serve
            if cur == S.SERVING:
                await self._transition(dep, S.DEGRADED, text)
            elif cur == S.DEGRADED:
                await self._transition(dep, S.DEGRADED, text)
            elif serve == "DEPLOY_FAILED":
                await self._transition(dep, S.FAILED, text)
        elif serve == "NOT_FOUND":
            if cur not in (S.STOPPED, *_UNSUBMITTED):
                await self._transition(dep, S.FAILED, "Serve application missing")
        return int(dep.status)

    async def _reconcile_running(self, dep: Any, cur: DeploymentStatus) -> None:
        """Serve says RUNNING: warm, canary, then SERVING (or recover from DEGRADED)."""
        if cur in (S.SERVING, S.STOPPED, S.FAILED):
            return
        if cur in (S.PENDING, S.PULLING, S.STARTING):
            await self._transition(dep, S.WARMING, "warming up")
        ok = await self._run_canary(dep)
        if cur == S.DEGRADED:
            if ok:
                await self._serving(dep, "recovered")
            return
        if ok:
            await self._serving(dep, "serving")
        else:
            await self._transition(dep, S.WARMING, "canary failed", detail="canary failed")

    async def _serving_row(self, model: str) -> list[Any]:
        """Rows of this backend serving ``model``, status SERVING with an endpoint."""
        rows = await self._backend_rows()
        return [
            r
            for r in rows
            if r.served_model_name == model and r.status == int(S.SERVING) and r.endpoint_url
        ]

    async def endpoints_for(self, model: str) -> list[Endpoint]:
        """Routable endpoints: SERVING rows only (DEGRADED/STOPPED/deleted are excluded)."""
        return [
            Endpoint(
                url=r.endpoint_url,
                node_id=r.serve_app_name,
                loaded_models=[model],
                healthy=True,
            )
            for r in await self._serving_row(model)
        ]

    async def place_model(self, model: str, constraints: dict[str, Any]) -> ModelPlacement:
        """Resolve an already-serving deployment (new placements come from the wizard)."""
        rows = [r for r in await self._backend_rows() if r.served_model_name == model]
        serving = [r for r in rows if r.status == int(S.SERVING)]
        target = serving[0] if serving else (rows[0] if rows else None)
        if target is not None:
            self._check_link_auth(target)
        if not serving:
            raise RuntimeError(
                f"No GPU deployment is serving {model!r}. Deploy it from the GPU Fleet page."
            )
        return ModelPlacement(model=model, node_id=serving[0].serve_app_name, status="placed")

    async def provision(self, spec: ProvisionSpec) -> list[NodeInfo]:
        """Deploy each deployment listed in ``constraints['deployment_public_ids']``."""
        ids = spec.constraints.get("deployment_public_ids")
        if not ids:
            raise ValueError("provision requires constraints['deployment_public_ids']")
        rows = {str(r.public_id): r for r in await self._backend_rows()}
        missing = [str(i) for i in ids if str(i) not in rows]
        if missing:
            raise ValueError(f"unknown deployment: {missing[0]}")
        for public_id in ids:
            await self.deploy(rows[str(public_id)])
        return await self.list_nodes()

    async def deprovision(self, node_id: str) -> None:
        """Unsupported: vLLM deployments are stopped or deleted individually."""
        raise PermissionError(
            "vLLM deployments are stopped or deleted per deployment; deprovision is unsupported"
        )

    async def list_nodes(self) -> list[NodeInfo]:
        """GPU inventory grouped by (node_uid, gpu_class).

        ``loaded_models`` is an approximation: served names of SERVING deployments whose
        gpu_class and vendor match the group.
        """
        inv = await self._select(
            lambda db: db.gpu_inventory.fleet_backend_id == self.fleet_backend_id
        )
        deps = [r for r in await self._backend_rows() if r.status == int(S.SERVING)]
        now = _naive_utcnow()
        groups: dict[tuple[str, str | None], list[Any]] = {}
        for row in inv:
            groups.setdefault((row.node_uid, row.gpu_class), []).append(row)
        nodes: list[NodeInfo] = []
        for (uid, gpu_class), rows in groups.items():
            healthy = any(
                r.state == int(InventoryState.WARM)
                and r.last_seen is not None
                and (now - r.last_seen).total_seconds() <= _NODE_HEALTHY_WINDOW_S
                for r in rows
            )
            vendor = rows[0].vendor
            loaded = [
                d.served_model_name
                for d in deps
                if gpu_class is not None and d.gpu_class == gpu_class and d.vendor == vendor
            ]
            nodes.append(
                NodeInfo(
                    node_id=f"{rows[0].node_name}/{gpu_class or 'unclassified'}",
                    node_uid=uid,
                    kind="k8s",
                    loaded_models=loaded,
                    vram_total_mb=sum(int(r.vram_total_mb or 0) for r in rows),
                    vram_free_mb=sum(
                        int(r.vram_total_mb or 0) - int(r.vram_used_mb or 0)
                        for r in rows
                        if r.schedulable
                    ),
                    healthy=healthy,
                    gpu_vendor=vendor,
                    gpu_class=gpu_class,
                )
            )
        return nodes

    async def health(self) -> FleetHealth:
        """Ray head reachable AND any warm node; never raises."""
        try:
            await self._client().get_application(_HEALTH_PROBE_APP)
        except RayClientError:
            return FleetHealth(self.fleet_backend_id, False, 0, {"error": "ray head unreachable"})
        nodes = await self.list_nodes()
        counts: dict[str, int] = {}
        for row in await self._backend_rows():
            name = S(row.status).name
            counts[name] = counts.get(name, 0) + 1
        return FleetHealth(
            backend_id=self.fleet_backend_id,
            healthy=any(n.healthy for n in nodes),
            node_count=len(nodes),
            detail={"deployments": counts},
        )

    async def collect_inventory(self) -> list[GpuRecord]:
        """Collect + upsert GPU inventory; an unusable (empty) cycle never sweeps nodes COLD."""
        org_id = self.config.get("org_id")
        if org_id is None:
            raise ValueError("vLLM Ray backend config requires org_id for inventory")
        collector = InventoryCollector(self.fleet_backend_id, int(org_id), self.config["class_map"])
        client = self._client()
        authoritative: list[str] | None
        try:
            authoritative = await client.list_alive_node_ids()
        except RayClientError as exc:
            logger.warning(
                "ray node list unavailable for backend %s: %s", self.fleet_backend_id, exc
            )
            authoritative = None
        result = await collector.collect(client, authoritative)
        skip: frozenset[str] = result.skipped_node_uids
        # without the trusted node list the cycle is incomplete: never COLD-sweep on it
        if authoritative is None or (not result.records and not result.complete):
            known = await self._select(
                lambda db: db.gpu_inventory.fleet_backend_id == self.fleet_backend_id
            )
            skip = frozenset(r.node_uid for r in known)
        await upsert_inventory(
            self.db,
            self.fleet_backend_id,
            int(org_id),
            result.records,
            skip_node_uids=skip,
            authoritative_node_ids=authoritative,
        )
        if not result.records:
            logger.warning("no GPU inventory records for backend %s", self.fleet_backend_id)
            return []
        return result.records
