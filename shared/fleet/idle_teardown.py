"""Idle lifecycle control for cost-bearing fleet backends.

Only ``full_lifecycle`` targets may be stopped or recreated here. Activity is
kept in Valkey when available, with an in-process fallback solely for local
development/tests; production callers should always provide Valkey.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from shared.fleet.base import BackendType, InferenceFleetBackend, ManagementScope, ProvisionSpec

_ACTIVITY_KEY = "waddleai:fleet:activity"


class AuditLog(Protocol):
    """Minimal audit sink accepted by :class:`IdleTeardownController`."""

    def __call__(self, **event: Any) -> Awaitable[None] | None:
        """Receive an audit event emitted by the teardown manager.

        May be sync or async; keyword fields describe the event.
        """


class IdleTeardownController:
    """Stops idle lifecycle-managed nodes and recreates capacity on demand.

    The controller is deliberately backend-agnostic: a scheduler supplies the
    live backends to :meth:`sweep`, and the request/placement path calls
    :meth:`ensure_deployed` before routing a cold model.  Audit failures are
    never allowed to hide a lifecycle failure; callers receive the original
    failure and the controller preserves the activity timestamp for retry.
    """

    def __init__(
        self,
        db: Any,
        registry: Any,
        audit_log: AuditLog | Callable[..., Any] | Any,
        *,
        valkey: Any | None = None,
        idle_seconds: int = 900,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Configure idle teardown with its backend, timeout and audit sink.

        Defines when idle nodes are removed and how it is recorded.
        """
        if idle_seconds <= 0:
            raise ValueError("idle_seconds must be positive")
        self.db = db
        self.registry = registry
        self.audit_log = audit_log
        self.valkey = valkey
        self.idle_seconds = idle_seconds
        self._clock = clock
        self._local_activity: dict[str, float] = {}

    @staticmethod
    def _key(backend_id: int, node_id: str) -> str:
        return f"{_ACTIVITY_KEY}:{backend_id}:{node_id}"

    async def record_activity(self, backend: InferenceFleetBackend, node_id: str) -> None:
        """Record successful use of a node, resetting its idle window."""
        timestamp = self._clock()
        key = self._key(backend.fleet_backend_id, node_id)
        self._local_activity[key] = timestamp
        if self.valkey is not None:
            await self.valkey.set(key, str(timestamp), ex=self.idle_seconds * 2)

    async def sweep(self, backends: list[InferenceFleetBackend]) -> list[tuple[int, str]]:
        """Tear down lifecycle-managed nodes past their activity window.

        Failed teardown operations are audit-logged and left eligible for the
        next sweep. A backend with no activity record is treated as recently
        discovered; its timer starts now rather than deleting unknown capacity.
        """
        stopped: list[tuple[int, str]] = []
        for backend in backends:
            if backend.management_scope != ManagementScope.FULL_LIFECYCLE:
                continue
            if backend.type == BackendType.VLLM_RAY:
                continue  # vLLM deployments are stopped/deleted individually (AM19d)
            for node in await backend.list_nodes():
                last_active = await self._last_activity(backend, node.node_id)
                if last_active is None:
                    await self.record_activity(backend, node.node_id)
                    continue
                if self._clock() - last_active < self.idle_seconds:
                    continue
                try:
                    await backend.deprovision(node.node_id)
                except Exception as exc:
                    await self._audit(
                        action="idle_teardown_failed",
                        backend_id=backend.fleet_backend_id,
                        node_id=node.node_id,
                        error=str(exc),
                    )
                    continue
                await self._clear_activity(backend, node.node_id)
                await self._audit(
                    action="idle_teardown",
                    backend_id=backend.fleet_backend_id,
                    node_id=node.node_id,
                )
                stopped.append((backend.fleet_backend_id, node.node_id))
        return stopped

    async def ensure_deployed(
        self,
        backend: InferenceFleetBackend,
        model: str,
        spec: ProvisionSpec | None = None,
    ) -> bool:
        """Return whether a lifecycle target was redeployed for ``model``.

        Registered/external targets are intentionally never provisioned. If a
        healthy endpoint already serves the model, its activity timestamp is
        refreshed and no lifecycle API call occurs.
        """
        endpoints = await backend.endpoints_for(model)
        if endpoints:
            for endpoint in endpoints:
                if endpoint.healthy:
                    await self.record_activity(backend, endpoint.node_id)
            return False
        if backend.management_scope != ManagementScope.FULL_LIFECYCLE:
            return False
        if backend.type == BackendType.VLLM_RAY:
            return False  # no on-demand provisioning for vLLM deployments (AM19d)
        provision_spec = spec or ProvisionSpec(
            name=f"on-demand-{model.replace('/', '-').replace(':', '-')}",
            models=[model],
            mode="on_demand",
            constraints={"model": model},
        )
        nodes = await backend.provision(provision_spec)
        for node in nodes:
            await self.record_activity(backend, node.node_id)
            await self._audit(
                action="redeploy_on_demand",
                backend_id=backend.fleet_backend_id,
                node_id=node.node_id,
                model=model,
            )
        return True

    async def _last_activity(self, backend: InferenceFleetBackend, node_id: str) -> float | None:
        key = self._key(backend.fleet_backend_id, node_id)
        if self.valkey is not None:
            raw = await self.valkey.get(key)
            if raw is not None:
                try:
                    return float(raw.decode() if isinstance(raw, bytes) else raw)
                except (TypeError, ValueError):
                    return None
        return self._local_activity.get(key)

    async def _clear_activity(self, backend: InferenceFleetBackend, node_id: str) -> None:
        key = self._key(backend.fleet_backend_id, node_id)
        self._local_activity.pop(key, None)
        if self.valkey is not None:
            await self.valkey.delete(key)

    async def _audit(self, **event: Any) -> None:
        sink = self.audit_log
        try:
            result = sink(**event) if callable(sink) else sink.write(**event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Lifecycle correctness must not depend on observability being up.
            # The primary action is still surfaced to the caller/scheduler.
            return
