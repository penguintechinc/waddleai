"""Leader-guarded status loop for llama.cpp RPC deployments (Plan F, F6).

Reads each multi-node deployment's worker StatefulSets and head Deployment, derives one status
and a plain-language message (``starting workers 1/3``, ``worker x down (2/3 up)``), mirrors a
serving deployment into its connection link so the proxy can route to it, and restarts the head
once every worker is back (llama-server only learns of its ``--rpc`` peers at startup). It never
deletes a row and never writes usage: a worker dying mid-generation fails that request through the
proxy's normal upstream-error path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from shared.fleet.enums import LlamaCppTopology, LocalDeploymentKind
from shared.observability.vllm_metrics import record_rpc_load_seconds, record_rpc_workers_up

from . import fleet_secrets
from .llamacpp_manager import LlamaCppManager, RpcObservation
from .vllm_reconciler import LeaderGuard

logger = logging.getLogger(__name__)

RPC_ADVISORY_LOCK_KEY = 7270012  # distinct from the vLLM loop's key: locks are per session
_WATCHED = ("deploying", "running", "degraded", "error")
_COUNTER_KEYS = (
    "deployments_examined",
    "status_changes",
    "head_restarts",
    "links_synced",
    "reconcile_errors",
)
Observer = Callable[[Any], RpcObservation]


@dataclass(slots=True, frozen=True)
class RpcHealth:
    """Derived status of one RPC deployment: a status word, its message and the worker counts."""

    status: str
    message: str
    workers_total: int
    workers_up: int
    head_ready: bool


def derive_rpc_status(
    *,
    workers_total: int,
    workers_up: int,
    head_ready: bool,
    was_running: bool,
    head_reloading: bool,
    failed_message: str | None,
    workers_down: list[str],
    head_gpus: int = 0,
    nodes: int | None = None,
) -> RpcHealth:
    """Map what Kubernetes reports onto the deployment timeline (pure).

    ``pending -> deploying (starting workers k/N) -> deploying (loading model) -> running ->
    degraded (worker down) -> deploying (reloading model) -> running``; a terminal Kubernetes
    failure (CrashLoopBackOff, ProgressDeadlineExceeded) is ``error``.
    """

    def health(status: str, message: str) -> RpcHealth:
        return RpcHealth(status, message, workers_total, workers_up, head_ready)

    if failed_message:
        return health("error", failed_message)
    if workers_up < workers_total:
        if was_running or head_ready:
            names = ", ".join(sorted(workers_down)) or "unknown"
            return health("degraded", f"worker {names} down ({workers_up}/{workers_total} up)")
        return health("deploying", f"starting workers {workers_up}/{workers_total}")
    if head_ready:
        gpus = workers_total + head_gpus
        where = f" on {nodes} nodes" if nodes else ""
        return health("running", f"serving across {gpus} GPUs{where}")
    if head_reloading or was_running:
        return health("deploying", "reloading model")
    return health("deploying", "loading model")


def _capabilities(dep: Any) -> dict[str, Any]:
    """The ``management_capabilities`` the llama.cpp connector reads (deployment id and price)."""
    return {
        "org_id": dep.owner_org_id,
        "local_deployment_kind": int(LocalDeploymentKind.LLAMACPP),
        "local_deployment_id": dep.id,
        "unit_price_input": str(dep.price_input_per_1m),
        "unit_price_output": str(dep.price_output_per_1m),
    }


class LlamaCppRpcReconciler:
    """Reconciles every RPC deployment of every flag-enabled org on the elected leader."""

    def __init__(
        self,
        db: Any,
        *,
        guard: LeaderGuard,
        manager_factory: Callable[[Any], LlamaCppManager] = LlamaCppManager,
        observer: Observer | None = None,
        restarter: Callable[[str, str], Awaitable[int]] = fleet_secrets.bump_restart,
        interval_s: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Wire collaborators; the observer, restarter and every time source are injectable."""
        self.db = db
        self.guard = guard
        self._manager = manager_factory(db)
        self.observer: Observer = observer or self._manager.rpc_observe
        self.restarter = restarter
        self.interval_s = interval_s
        self.clock = clock
        self.sleep = sleep
        self._started_at: dict[int, float] = {}
        self._reloading: set[int] = set()

    def _rows(self) -> list[Any]:
        """Blocking select of RPC deployments worth watching (run via ``to_thread``)."""
        table = self.db.llamacpp_deployments
        query = (table.topology == int(LlamaCppTopology.RPC_MULTI_NODE)) & table.status.belongs(
            _WATCHED
        )
        return list(self.db(query).select())

    def _write(self, dep: Any, health: RpcHealth) -> bool:
        """Persist a status change only if the row still has the status we observed (blocking).

        The snapshot is seconds old; an unconditional write could resurrect a deployment that was
        removed or redeployed meanwhile. Returns False when the row moved on (nothing written).
        """
        table = self.db.llamacpp_deployments
        changed = self.db((table.id == dep.id) & (table.status == dep.status)).update(
            status=health.status, status_message=health.message
        )
        self.db.commit()
        return bool(changed)

    def _sync_link(self, dep: Any, serving: bool) -> bool:
        """Point the key link at the head when serving, disable it otherwise; True if changed."""
        table = self.db.connection_links
        link = self.db(table.name == f"llamacpp-{dep.public_id}").select().first()
        if link is None:
            return False
        if not serving:
            if link.enabled:
                self.db(table.id == link.id).update(enabled=False)
                self.db.commit()
                return True
            return False
        values = {
            "provider": "llamacpp",
            "endpoint_url": dep.endpoint_url or "",
            "model_list": [dep.model_name],
            "enabled": True,
            "management_capabilities": _capabilities(dep),
            "owner_org_id": dep.owner_org_id,
        }
        if all(getattr(link, key, None) == value for key, value in values.items()):
            return False
        if not self._still_live(dep):
            return False  # removed/stopped since we observed it: never route to it
        self.db(table.id == link.id).update(**values)
        self.db.commit()
        if not self._still_live(dep):
            # a /remove landed between the check and the write: converge back to disabled
            self.db(table.id == link.id).update(enabled=False)
            self.db.commit()
            return False
        return True

    def _still_live(self, dep: Any) -> bool:
        """True while the row exists and still holds a serving status (re-read, never cached)."""
        table = self.db.llamacpp_deployments
        row = self.db((table.id == dep.id) & table.status.belongs(("running", "degraded"))).select()
        return row.first() is not None

    async def _enabled(self, dep: Any) -> bool:
        """True when the deployment's org has both GPU Fleet flags on (unowned rows never)."""
        from ..api.v1.fleet import _vllm_ray_enabled

        return dep.owner_org_id is not None and _vllm_ray_enabled(dep.owner_org_id)

    async def _one(self, dep: Any, counts: dict[str, int], up_by_name: dict[str, int]) -> None:
        """Observe and reconcile one deployment; every failure is counted, never raised."""
        counts["deployments_examined"] += 1
        try:
            seen = await asyncio.to_thread(self.observer, dep)
        except Exception:
            counts["reconcile_errors"] += 1
            logger.warning("rpc observation failed for %s", dep.public_id, exc_info=True)
            return
        up_by_name[str(dep.name)] = seen.workers_up
        was_running = dep.status in ("running", "degraded")
        reloading = int(dep.id) in self._reloading
        if reloading and seen.head_ready:
            self._reloading.discard(int(dep.id))
            reloading = False
        recovered = dep.status == "degraded" and seen.workers_up == seen.workers_total
        if recovered and not reloading and not seen.failed_message:
            if await self._restart_head(dep, counts):
                reloading = True
                seen = RpcObservation(
                    seen.workers_total, seen.workers_up, [], False, seen.failed_message
                )
        nodes = len({w["node_uid"] for w in dep.rpc_workers or []} | {dep.rpc_head_node_uid})
        health = derive_rpc_status(
            workers_total=seen.workers_total,
            workers_up=seen.workers_up,
            head_ready=seen.head_ready,
            was_running=was_running,
            head_reloading=reloading,
            failed_message=seen.failed_message,
            workers_down=seen.workers_down,
            head_gpus=int(dep.gpu_count or 0),
            nodes=nodes,
        )
        try:
            await self._apply(dep, health, counts)
        except Exception:
            counts["reconcile_errors"] += 1
            logger.warning("rpc reconcile apply failed for %s", dep.public_id, exc_info=True)

    async def _restart_head(self, dep: Any, counts: dict[str, int]) -> bool:
        """Bump the head's restart generation so it reloads with every worker; True if bumped."""
        base = dep.k8s_daemonset_name
        try:
            if not base:
                raise RuntimeError("RPC deployment has no k8s object name recorded")
            await self.restarter("Deployment", str(base))
        except Exception:
            counts["reconcile_errors"] += 1
            logger.error("head restart failed for %s", dep.public_id)
            return False
        self._reloading.add(int(dep.id))
        self._started_at[int(dep.id)] = self.clock()
        counts["head_restarts"] += 1
        logger.info("llamacpp.rpc.head_restarted deployment=%s", dep.public_id)
        return True

    async def _apply(self, dep: Any, health: RpcHealth, counts: dict[str, int]) -> None:
        """Write a changed status, record load time on first serving, and sync the link."""
        dep_id = int(dep.id)
        changed = health.status != dep.status or health.message != dep.status_message
        if changed:
            if not await asyncio.to_thread(self._write, dep, health):
                logger.info("llamacpp.rpc.status skipped: %s changed concurrently", dep.public_id)
                return
            counts["status_changes"] += 1
            logger.info(
                "llamacpp.rpc.status deployment=%s %s -> %s: %s",
                dep.public_id,
                dep.status,
                health.status,
                health.message,
            )
        if health.status == "deploying":
            self._started_at.setdefault(dep_id, self.clock())
        elif health.status == "running":
            started = self._started_at.pop(dep_id, None)
            if dep.status != "running":
                if started is None:
                    logger.info("load time not recorded for %s (restart)", dep.public_id)
                else:
                    record_rpc_load_seconds(max(0.0, self.clock() - started))
        if await asyncio.to_thread(self._sync_link, dep, health.status == "running"):
            counts["links_synced"] += 1

    async def tick(self) -> dict[str, int]:
        """One reconcile pass; a no-op unless this process is the leader."""
        counts = dict.fromkeys(_COUNTER_KEYS, 0)
        if not await self.guard.acquire():
            return counts
        up_by_name: dict[str, int] = {}
        for dep in await asyncio.to_thread(self._rows):
            if await self._enabled(dep):
                await self._one(dep, counts, up_by_name)
        record_rpc_workers_up(up_by_name)
        return counts

    async def run_forever(self) -> None:
        """``tick`` then sleep; logs every exception and exits cleanly on cancellation."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("llamacpp rpc reconcile tick failed", exc_info=True)
            try:
                await self.sleep(self.interval_s)
            except asyncio.CancelledError:
                return
