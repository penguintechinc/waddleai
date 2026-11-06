"""Leader-guarded lifecycle loop for vLLM deployments (Plan C, C3).

Drives ``VllmRayBackend.reconcile`` for every live deployment, refreshes GPU inventory, mirrors
``SERVING``/``DEGRADED`` state into ``connection_links`` and ``model_configs`` (so the proxy can
route to the deployment), recomputes AUTO prices daily, and finishes model pulls by deploying the
weights once they land. Exactly one management replica runs it, chosen by a Postgres advisory lock.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Protocol

from shared.fleet.enums import DeploymentStatus, LocalDeploymentKind, PriceMode
from shared.fleet.pricing import (
    PricingInputsUnavailable,
    apply_pricing,
    load_pricing_inputs,
)
from shared.fleet.registry import build_backend
from shared.fleet.throughput import observed_aggregate_tokens_per_s, prune_generation_samples
from shared.fleet.vllm_pricing import estimate_aggregate_tokens_per_s, resolve_params_b
from shared.fleet.vllm_ray.tco import TcoResult
from shared.observability.vllm_metrics import record_deployment_counts, record_inventory
from shared.routing.prefer_local import create_prefer_local_rule
from shared.routing.providers import LOCAL_PROVIDERS

logger = logging.getLogger(__name__)

S = DeploymentStatus
ADVISORY_LOCK_KEY = 7270011
PRICE_RECALCULATED = "price recalculated"
_LIVE = (S.PENDING, S.PULLING, S.STARTING, S.WARMING, S.SERVING, S.DEGRADED)
_COUNTER_KEYS = (
    "deployments_examined",
    "status_changes",
    "inventory_refreshes",
    "links_upserted",
    "links_removed",
    "reconcile_errors",
    "prices_recalculated",
    "samples_pruned",
)
SAMPLE_RETENTION_HOURS = 72
OBSERVED_WINDOW_HOURS = 24

PreferLocalFn = Callable[..., Any]  # async or sync (the real factory is sync)


class LeaderGuard(Protocol):
    """Decides whether this process may run the reconcile loop."""

    async def acquire(self) -> bool:
        """True when this process is (or becomes) the leader."""
        ...


class AlwaysLeader:
    """Leader guard for tests and single-replica deployments."""

    async def acquire(self) -> bool:
        """Always the leader."""
        return True


class PgAdvisoryLeaderGuard:
    """Leader election via a session-level Postgres advisory lock held for the process lifetime."""

    def __init__(self, engine: Any) -> None:
        """Bind the SQLAlchemy engine; the lock connection is opened lazily and kept."""
        self._engine = engine
        self._conn: Any = None
        self._held = False

    def _try_lock(self) -> bool:
        """Take (once) the advisory lock on a dedicated connection; False on any failure."""
        if self._held:
            return True
        try:
            from sqlalchemy import text

            if self._engine.dialect.name != "postgresql":
                logger.warning("vllm reconciler leader lock needs PostgreSQL; not leading")
                return False
            self._conn = self._engine.connect()
            got = bool(
                self._conn.execute(
                    text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
                ).scalar()
            )
            if not got:
                self._conn.close()
                self._conn = None
            self._held = got
            return got
        except Exception:
            logger.warning("vllm reconciler leader lock failed", exc_info=True)
            return False

    async def acquire(self) -> bool:
        """Try the advisory lock off the event loop; never raises."""
        return await asyncio.to_thread(self._try_lock)


class UsageSource(Protocol):
    """Observed AGGREGATE (deployment-total) throughput source for AUTO price recomputation."""

    def observed_aggregate_tps(
        self,
        db: Any,
        kind: int,
        deployment_id: int,
        window_s: int,
        estimate_aggregate_tokens_per_s: float | None = None,
    ) -> float | None:
        """Sustained aggregate output tokens/s of the deployment, or None when unmeasurable."""
        ...


class SampleUsageSource:
    """Default source: sustained aggregate rate (p90 of busy 5-minute buckets of samples).

    Delegates to ``shared.fleet.throughput.observed_aggregate_tokens_per_s`` (a deployment
    TOTAL, never a per-request rate), which enforces the principal/busy-bucket thresholds and
    ignores a rate at/below the deployment-total estimate (observed may only lower the price)
    and rejects one above 10x it; ``None`` keeps the estimate.
    """

    def observed_aggregate_tps(
        self,
        db: Any,
        kind: int,
        deployment_id: int,
        window_s: int,
        estimate_aggregate_tokens_per_s: float | None = None,
    ) -> float | None:
        """Observed aggregate tokens/s for the window, or None when not trustworthy."""
        return observed_aggregate_tokens_per_s(
            db,
            kind,
            deployment_id,
            window_hours=max(1, window_s // 3600),
            estimate_aggregate_tokens_per_s=estimate_aggregate_tokens_per_s,
        )


def _now() -> datetime:
    """Naive UTC timestamp for DAL columns."""
    return datetime.utcnow()


def link_name(dep: Any) -> str:
    """Org-scoped connection link name (the column is globally unique)."""
    return f"vllm-{dep.org_id}-{dep.slug}"


def _capabilities(dep: Any) -> dict[str, Any]:
    """The ``management_capabilities`` the proxy connector reads (deployment, price, status)."""
    return {
        "org_id": dep.org_id,
        "local_deployment_kind": int(LocalDeploymentKind.VLLM),
        "local_deployment_id": dep.id,
        "unit_price_input": str(dep.price_input_per_1m),
        "unit_price_output": str(dep.price_output_per_1m),
        "fleet_status": int(dep.status),
    }


def _find_link(db: Any, dep: Any) -> Any | None:
    """The deployment's link by id, else by its org-scoped name."""
    if dep.connection_link_id:
        row = db(db.connection_links.id == dep.connection_link_id).select().first()
        if row is not None:
            return row
    return db(db.connection_links.name == link_name(dep)).select().first()


def _owns_offer_row(db: Any, dep: Any) -> bool:
    """True when this deployment has already published its offer."""
    table = db.model_configs
    return db(table.deployment_public_id == str(dep.public_id)).count() > 0


def _remove_offer(db: Any, dep: Any) -> bool:
    """Delete the ``model_configs`` offer ONLY when this deployment owns it; True if removed.

    Looked up by ``deployment_public_id``, so an offer another deployment, org or a commercial
    provider still uses (or any unscoped legacy row) is never touched.
    """
    table = db.model_configs
    rows = db(table.deployment_public_id == str(dep.public_id)).select()
    for row in rows:
        db(table.id == row.id).delete()
    return len(rows) > 0


def offer_blocked(db: Any, org_id: int, name: str) -> bool:
    """True when ``name`` is already offered to this org by someone else.

    Blocks on this org's own offer (a different deployment's) or a global offer served by any
    non-local (commercial) provider. Other orgs' offers never block: offers are per org.
    """
    for row in db(db.model_configs.model_name == name).select():
        if row.owner_org_id == org_id:
            return True
        if row.owner_org_id is None and not set(row.preferred_providers or []) <= LOCAL_PROVIDERS:
            return True
    return False


def _sync(db: Any, dep: Any) -> tuple[int | None, bool, bool]:
    """Apply ``dep``'s status to its link/offer. Returns (link_id, changed, removed)."""
    status = int(dep.status)
    link = _find_link(db, dep)
    if status in (S.FAILED, S.STOPPED) or not dep.expose_via_proxy:
        removed = _remove_offer(db, dep)
        if link is not None and link.enabled:
            db(db.connection_links.id == link.id).update(enabled=False)
            return int(link.id), True, removed
        return (int(link.id) if link is not None else None), False, removed
    if status not in (S.SERVING, S.DEGRADED):
        return (int(link.id) if link is not None else None), False, False

    caps = _capabilities(dep)
    values: dict[str, Any] = {
        "provider": "vllm",
        "endpoint_url": dep.endpoint_url or "",
        "model_list": [dep.served_model_name],
        "enabled": True,
        "management_capabilities": caps,
        "owner_org_id": dep.org_id,
    }
    changed = False
    if link is None:
        link_id = int(db.connection_links.insert(name=link_name(dep), **values))
        changed = True
    else:
        link_id = int(link.id)
        current = {k: getattr(link, k, None) for k in values}
        if any(current[k] != values[k] for k in values):
            db(db.connection_links.id == link_id).update(**values)
            changed = True
    if dep.connection_link_id != link_id:
        db(db.vllm_deployments.id == dep.id).update(connection_link_id=link_id)
        dep.connection_link_id = link_id
    if not _owns_offer_row(db, dep):
        if offer_blocked(db, dep.org_id, dep.served_model_name):
            logger.warning(
                "model offer %r already exists for org %s and is not owned by deployment %s; "
                "not publishing",
                dep.served_model_name,
                dep.org_id,
                dep.id,
            )
        else:
            db.model_configs.insert(
                model_name=dep.served_model_name,
                preferred_providers=["vllm"],
                cost_per_token={},
                max_tokens=int(dep.max_model_len),
                context_length=int(dep.max_model_len),
                capabilities=[],
                enabled=True,
                owner_org_id=dep.org_id,
                deployment_public_id=str(dep.public_id),
            )
            changed = True
    return link_id, changed, False


async def sync_deployment_link(db: Any, dep: Any) -> int | None:
    """Mirror a deployment's state into its connection link and local offer."""
    link_id, _changed, _removed = await asyncio.to_thread(_sync, db, dep)
    return link_id


def _unlink(db: Any, dep: Any) -> None:
    """Remove the link, the vLLM-owned offer, and the prefer-local rule id."""
    link = _find_link(db, dep)
    if link is not None:
        db(db.connection_links.id == link.id).delete()
    _remove_offer(db, dep)
    prefer = dep.prefer_local
    if isinstance(prefer, dict) and "rule_id" in prefer:
        remaining = {k: v for k, v in prefer.items() if k != "rule_id"}
        db(db.vllm_deployments.id == dep.id).update(prefer_local=remaining)


async def unlink_deployment(db: Any, dep: Any) -> None:
    """Delete-path cleanup: link, vLLM-owned offer, and the recorded prefer-local rule id."""
    await asyncio.to_thread(_unlink, db, dep)


class VllmReconciler:
    """Reconciles every vLLM deployment of every flag-enabled org on the elected leader."""

    def __init__(
        self,
        db: Any,
        *,
        guard: LeaderGuard,
        backend_factory: Callable[[Any, Any], Any] = build_backend,
        prefer_local_fn: PreferLocalFn | None = None,
        deployment_interval_s: float = 15.0,
        inventory_interval_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        usage_source: UsageSource | None = None,
        price_interval_s: float = 86400.0,
        pull_poller: Callable[[Any, Any, Any], Awaitable[bool]] | None = None,
    ) -> None:
        """Wire the loop's collaborators; every time source is injectable for tests."""
        self.db = db
        self.guard = guard
        self.backend_factory = backend_factory
        self.prefer_local_fn: PreferLocalFn = prefer_local_fn or create_prefer_local_rule
        self.deployment_interval_s = deployment_interval_s
        self.inventory_interval_s = inventory_interval_s
        self.clock = clock
        self.sleep = sleep
        self.usage_source: UsageSource = usage_source or SampleUsageSource()
        self.price_interval_s = price_interval_s
        self.pull_poller = pull_poller
        self._last_reconciled: dict[int, float] = {}
        self._last_inventory: dict[int, float] = {}
        self._last_priced: dict[tuple[int, int], float] = {}
        self._last_pruned: float | None = None
        self._last_counts: float | None = None
        self._last_local_scan: dict[int, float] = {}

    def _select(self, build: Callable[[Any], Any]) -> list[Any]:
        """Blocking DAL select (run via ``asyncio.to_thread``)."""
        return list(self.db(build(self.db)).select())

    async def _backend_rows(self) -> list[Any]:
        """Enabled vllm_ray backends whose org has both flags on."""
        from ..api.v1.fleet import _vllm_ray_enabled

        rows = await asyncio.to_thread(
            self._select,
            lambda db: (
                (db.fleet_backends.type == "vllm_ray") & (db.fleet_backends.status != "disabled")
            ),
        )
        return [r for r in rows if _vllm_ray_enabled(r.org_id)]

    async def tick(self) -> dict[str, int]:
        """One reconcile pass; a no-op unless this process is the leader."""
        counts = dict.fromkeys(_COUNTER_KEYS, 0)
        if not await self.guard.acquire():
            return counts
        for brow in await self._backend_rows():
            try:
                backend = self.backend_factory(self.db, brow)
            except Exception:
                logger.warning("vllm backend %s could not be built", brow.id, exc_info=True)
                counts["reconcile_errors"] += 1
                continue
            await self._inventory(backend, brow, counts)
            deps = await asyncio.to_thread(self._backend_deployments, brow)
            for dep in deps:
                await self._one(backend, brow, dep, counts)
            await self._recompute_local_kinds(brow, counts)
        await self._record_counts()
        await self._prune_samples(counts)
        return counts

    async def _record_counts(self) -> None:
        """Publish the per-status deployment gauge, at most once per deployment interval."""
        now = self.clock()
        if self._last_counts is not None and now - self._last_counts < self.deployment_interval_s:
            return
        self._last_counts = now
        rows = await asyncio.to_thread(
            lambda: list(self.db(self.db.vllm_deployments.id > 0).select())
        )
        by_status: dict[int, int] = {}
        for row in rows:
            by_status[int(row.status)] = by_status.get(int(row.status), 0) + 1
        record_deployment_counts(by_status)

    async def _prune_samples(self, counts: dict[str, int]) -> None:
        """Delete generation samples older than 72h, at most once per price interval (daily)."""
        now = self.clock()
        if self._last_pruned is not None and now - self._last_pruned < self.price_interval_s:
            return
        self._last_pruned = now
        try:
            counts["samples_pruned"] += int(
                await asyncio.to_thread(prune_generation_samples, self.db, SAMPLE_RETENTION_HOURS)
            )
        except Exception:
            logger.warning("generation sample prune failed", exc_info=True)
            counts["reconcile_errors"] += 1

    async def _recompute_local_kinds(self, brow: Any, counts: dict[str, int]) -> None:
        """Daily AUTO recompute for THIS org's running Ollama and llama.cpp deployments.

        They price with the org's own vllm_ray backend pricing config (one formula for every
        local kind, AM10); ``apply_pricing`` upserts the per-deployment rate row. Ownership is
        derived only through ``fleet_backend_id -> fleet_backends.org_id``: a row with no
        derivable owning org is skipped with a warning and never priced with another org's
        config. Only the org's lowest-id vllm_ray backend runs this, so each row is priced once.
        """
        now = self.clock()
        last_scan = self._last_local_scan.get(int(brow.id))
        if last_scan is not None and now - last_scan < self.deployment_interval_s:
            return  # the backend/row selects below are interval-gated, not per-tick
        self._last_local_scan[int(brow.id)] = now
        first = await asyncio.to_thread(
            self._select,
            lambda db: (
                (db.fleet_backends.org_id == brow.org_id) & (db.fleet_backends.type == "vllm_ray")
            ),
        )
        if not first or min(int(r.id) for r in first) != int(brow.id):
            return
        owned_ids = {
            int(r.id)
            for r in await asyncio.to_thread(
                self._select, lambda db: db.fleet_backends.org_id == brow.org_id
            )
        }
        config = brow.config or {}
        try:
            inputs, costs = load_pricing_inputs(config)
        except ValueError:
            logger.warning("backend %s pricing config invalid", brow.id, exc_info=True)
            counts["reconcile_errors"] += 1
            return
        for kind, table in (
            (int(LocalDeploymentKind.OLLAMA), "ollama_deployments"),
            (int(LocalDeploymentKind.LLAMACPP), "llamacpp_deployments"),
        ):
            rows = await asyncio.to_thread(self._running_auto_rows, table, owned_ids)
            for row in rows:
                owner = row.fleet_backend_id
                if owner is None:
                    logger.warning(
                        "%s %s has no owning backend; skipping price recompute", table, row.id
                    )
                    continue
                if int(owner) not in owned_ids:
                    continue  # another org's deployment: never priced with this org's config
                await self._recompute_local(kind, table, row, inputs, costs, counts)

    def _running_auto_rows(self, table_name: str, owned_ids: set[int]) -> list[Any]:
        """Running AUTO-priced rows of a local-deployment table owned by ``owned_ids`` backends.

        Ownership is filtered in SQL so a tick never loads (or prices) another org's rows; rows
        with no owning backend are still returned so the caller can warn about them.
        """
        table = getattr(self.db, table_name)
        owner = table.fleet_backend_id
        query = (
            (table.status == "running")
            & (table.price_mode == int(PriceMode.AUTO_TCO))
            & (owner.belongs(sorted(owned_ids)) | (owner == None))  # noqa: E711
        )
        return list(self.db(query).select())

    async def _recompute_local(
        self, kind: int, table: str, row: Any, inputs: Any, costs: Any, counts: dict[str, int]
    ) -> None:
        """Recompute one Ollama/llama.cpp deployment's AUTO price once per interval."""
        key = (kind, int(row.id))
        now = self.clock()
        last = self._last_priced.get(key)
        if last is not None and now - last < self.price_interval_s:
            return
        self._last_priced[key] = now
        view = SimpleNamespace(**{k: row[k] for k in row.keys()})
        try:
            estimate = await asyncio.to_thread(
                estimate_aggregate_tokens_per_s, self.db, kind, view, costs, inputs
            )
            observed = await asyncio.to_thread(
                self.usage_source.observed_aggregate_tps,
                self.db,
                kind,
                int(row.id),
                OBSERVED_WINDOW_HOURS * 3600,
                estimate,
            )
        except Exception:
            # A failed lookup is NOT "no observation": keep the last good price (skip recompute).
            logger.warning("throughput lookup failed for %s %s", table, row.id, exc_info=True)
            counts["reconcile_errors"] += 1
            return
        try:
            await apply_pricing(self.db, kind, view, inputs, costs, observed)
        except (ValueError, PricingInputsUnavailable):
            logger.warning("local price recompute failed for %s %s", table, row.id, exc_info=True)
            counts["reconcile_errors"] += 1
            return

        def _persist() -> None:
            self.db(getattr(self.db, table).id == row.id).update(
                price_input_per_1m=view.price_input_per_1m,
                price_output_per_1m=view.price_output_per_1m,
                price_breakdown=view.price_breakdown,
                observed_tokens_per_s=view.observed_tokens_per_s,
            )

        await asyncio.to_thread(_persist)
        counts["prices_recalculated"] += 1

    def _backend_deployments(self, brow: Any) -> list[Any]:
        """This backend's deployments, restricted to the backend's own org."""
        table = self.db.vllm_deployments
        query = (table.fleet_backend_id == brow.id) & (table.org_id == brow.org_id)
        return list(self.db(query).select())

    async def _inventory(self, backend: Any, brow: Any, counts: dict[str, int]) -> None:
        """Collect GPU inventory when the interval has elapsed (first tick always)."""
        now = self.clock()
        last = self._last_inventory.get(brow.id)
        if last is not None and now - last < self.inventory_interval_s:
            return
        self._last_inventory[brow.id] = now
        try:
            record_inventory(list(await backend.collect_inventory() or []))
            counts["inventory_refreshes"] += 1
        except Exception:
            logger.warning("vllm inventory refresh failed for backend %s", brow.id, exc_info=True)
            counts["reconcile_errors"] += 1

    async def _one(self, backend: Any, brow: Any, dep: Any, counts: dict[str, int]) -> None:
        """Reconcile, sync, prefer-local and price one deployment; failures are contained."""
        status = int(dep.status)
        try:
            if status in _LIVE:
                await self._reconcile(backend, dep, counts)
            elif status in (S.STOPPED, S.FAILED):
                _lid, _chg, removed = await asyncio.to_thread(_sync, self.db, dep)
                counts["links_removed"] += int(removed)
            await self._after_reconcile(backend, brow, dep, counts)
        except Exception:
            logger.warning("vllm deployment %s reconcile failed", dep.id, exc_info=True)
            counts["reconcile_errors"] += 1

    async def _reconcile(self, backend: Any, dep: Any, counts: dict[str, int]) -> None:
        """Run the backend reconcile when due; finish pulls first."""
        now = self.clock()
        last = self._last_reconciled.get(dep.id)
        if last is not None and now - last < self.deployment_interval_s:
            return
        self._last_reconciled[dep.id] = now
        counts["deployments_examined"] += 1
        before = int(dep.status)
        if before in (int(S.PENDING), int(S.PULLING)) and self.pull_poller is not None:
            if not await self.pull_poller(self.db, backend, dep):
                return
        after = int(await backend.reconcile(dep))
        if after != before:
            counts["status_changes"] += 1

    async def _after_reconcile(
        self, backend: Any, brow: Any, dep: Any, counts: dict[str, int]
    ) -> None:
        """Sync link/offer, create the prefer-local rule once, and recompute AUTO price."""
        link_id, changed, removed = await asyncio.to_thread(_sync, self.db, dep)
        counts["links_upserted"] += int(changed and int(dep.status) in (S.SERVING, S.DEGRADED))
        counts["links_removed"] += int(removed)
        if int(dep.status) in (S.SERVING, S.DEGRADED):
            await self._prefer_local(dep)
            await self._recompute(brow, dep, counts)

    async def _prefer_local(self, dep: Any) -> None:
        """Create the prefer-local rule exactly once and persist its id."""
        prefer = dep.prefer_local
        if not isinstance(prefer, dict) or prefer.get("rule_id") is not None:
            return
        args = (self.db, dep.org_id, dep, prefer.get("fallback_model"), prefer.get("match") or {})
        if inspect.iscoroutinefunction(self.prefer_local_fn):
            rule_id = await self.prefer_local_fn(*args)
        else:
            rule_id = await asyncio.to_thread(self.prefer_local_fn, *args)
        merged = {**prefer, "rule_id": rule_id}

        def _persist() -> None:
            self.db(self.db.vllm_deployments.id == dep.id).update(prefer_local=merged)

        await asyncio.to_thread(_persist)
        dep.prefer_local = merged

    async def _recompute(self, brow: Any, dep: Any, counts: dict[str, int]) -> None:
        """Daily AUTO price recompute for a SERVING deployment (MANUAL is never touched)."""
        if int(dep.status) != int(S.SERVING) or dep.price_mode != int(PriceMode.AUTO_TCO):
            return
        key = (int(LocalDeploymentKind.VLLM), int(dep.id))
        now = self.clock()
        last = self._last_priced.get(key)
        if last is not None and now - last < self.price_interval_s:
            return
        self._last_priced[key] = now
        try:
            result = await _recompute_auto_prices(self.db, brow, dep, self.usage_source)
        except Exception:
            logger.warning("price recompute failed for deployment %s", dep.id, exc_info=True)
            counts["reconcile_errors"] += 1
            return
        if result is None:
            counts["reconcile_errors"] += 1
            return
        counts["prices_recalculated"] += 1
        await asyncio.to_thread(_sync, self.db, dep)

    async def run_forever(self) -> None:
        """``tick`` then sleep; logs every exception and exits cleanly on cancellation."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("vllm reconcile tick failed", exc_info=True)
            try:
                await self.sleep(1)
            except asyncio.CancelledError:
                return


async def _recompute_auto_prices(
    db: Any, backend_row: Any, dep: Any, usage_source: UsageSource | None = None
) -> TcoResult | None:
    """Recompute and persist one AUTO deployment's price; None (price unchanged) on error."""
    source = usage_source or SampleUsageSource()
    config = backend_row.config or {}
    try:
        params_b = await asyncio.to_thread(resolve_params_b, db, dep)
        _inputs, costs = load_pricing_inputs(config)
        estimate = estimate_aggregate_tokens_per_s(
            db, int(LocalDeploymentKind.VLLM), dep, costs, _inputs
        )
        observed = await asyncio.to_thread(
            source.observed_aggregate_tps,
            db,
            int(LocalDeploymentKind.VLLM),
            int(dep.id),
            OBSERVED_WINDOW_HOURS * 3600,
            estimate,
        )
        result = await _price_row(db, dep, config, observed, params_b)
    except (ValueError, PricingInputsUnavailable):
        logger.warning("AUTO price recompute rejected for deployment %s", dep.id, exc_info=True)
        return None
    return result


async def _price_row(
    db: Any,
    dep: Any,
    config: dict[str, Any],
    observed: float | None,
    params_b: float | None,
) -> TcoResult | None:
    """Price through the shared helper, persist price columns and write the event."""
    from shared.fleet.vllm_pricing import price_deployment

    priced = await price_deployment(db, dep, config, observed, params_b)

    def _persist() -> None:
        values: dict[str, Any] = {
            "price_input_per_1m": priced.price_input_per_1m,
            "price_output_per_1m": priced.price_output_per_1m,
            "price_breakdown": priced.price_breakdown,
            # NULL when no observation set the price -- never leave a stale value behind
            "observed_tokens_per_s": priced.observed_tokens_per_s,
            "updated_at": _now(),
        }
        db(db.vllm_deployments.id == dep.id).update(**values)
        db.vllm_deployment_events.insert(
            deployment_id=dep.id, ts=_now(), state=int(dep.status), message=PRICE_RECALCULATED
        )

    await asyncio.to_thread(_persist)
    dep.price_input_per_1m = priced.price_input_per_1m
    dep.price_output_per_1m = priced.price_output_per_1m
    return priced.result
