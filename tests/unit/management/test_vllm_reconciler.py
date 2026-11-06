"""C3: the leader-guarded vLLM lifecycle loop, link/offer sync and daily AUTO re-pricing."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine

from services.management.app.services.vllm_reconciler import (
    AlwaysLeader,
    PgAdvisoryLeaderGuard,
    VllmReconciler,
    unlink_deployment,
)
from tests.unit.management.vllm_harness import (
    BACKEND_CONFIG,
    FakeBackend,
    seed_backend,
    seed_deployment,
)


class FakeGuard:
    """A leader guard with a scripted answer."""

    def __init__(self, leader: bool) -> None:
        """Script the leader answer."""
        self.leader = leader

    async def acquire(self) -> bool:
        """The scripted answer."""
        return self.leader


class FakeUsage:
    """A usage source returning a fixed observed throughput and recording its calls."""

    def __init__(self, tps: float | None) -> None:
        """Script the observed throughput."""
        self.tps = tps
        self.calls: list[tuple[int, int]] = []

    def p50_output_tps(self, db: Any, kind: int, deployment_id: int, window_s: int) -> float | None:
        """Record the call and return the scripted value."""
        self.calls.append((kind, deployment_id))
        return self.tps


class Clock:
    """A settable monotonic clock."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """The current fake time."""
        return self.now


def _reconciler(db, backend=None, *, clock=None, **kw) -> tuple[VllmReconciler, FakeBackend, Clock]:
    backend = backend or FakeBackend()
    clock = clock or Clock()
    kw.setdefault("guard", AlwaysLeader())
    rec = VllmReconciler(db, backend_factory=lambda _db, _row: backend, clock=clock, **kw)
    return rec, backend, clock


def _offer(db, providers, name="model-7b", **over):
    """Insert a model_configs offer row."""
    values = {"cost_per_token": {}, "max_tokens": 1, "context_length": 1, "capabilities": []}
    db.model_configs.insert(model_name=name, preferred_providers=providers, **{**values, **over})
    db.commit()


def _n(backend: FakeBackend) -> int:
    """Number of Serve reconcile calls the fake backend saw."""
    return len([c for c in backend.calls if c[0] == "reconcile"])


def _tick(rec: VllmReconciler) -> dict[str, int]:
    return asyncio.run(rec.tick())


def _link(db, name_part: str | None = None):
    rows = list(db(db.connection_links.id > 0).select())
    return rows if name_part is None else [r for r in rows if name_part in r.name]


@pytest.fixture
def env(vllm_db, both_flags):
    """A real DB with flags on."""
    return vllm_db


def test_tick_not_leader_does_nothing(env) -> None:
    """A non-leader returns zero counts and never builds a backend."""
    backend = seed_backend(env)
    seed_deployment(env, backend, status=4)
    built: list[Any] = []
    rec = VllmReconciler(env, guard=FakeGuard(False), backend_factory=lambda *a: built.append(a))
    counts = _tick(rec)
    assert set(counts.values()) == {0} and built == []


def test_tick_reconciles_each_nonterminal_once(env) -> None:
    """3 live + STOPPED + FAILED -> 3 reconciles, deployments_examined == 3."""
    b = seed_backend(env)
    for i, status in enumerate((3, 4, 5, 7, 8)):
        seed_deployment(env, b, name=f"d{i}", status=status)
    rec, backend, _ = _reconciler(env)
    counts = _tick(rec)
    assert counts["deployments_examined"] == 3
    assert len([c for c in backend.calls if c[0] == "reconcile"]) == 3


def test_tick_respects_deployment_interval(env) -> None:
    """+10 s: no reconcile; +16 s: reconciled again."""
    b = seed_backend(env)
    seed_deployment(env, b, status=5)
    rec, backend, clock = _reconciler(env)
    _tick(rec)
    clock.now = 10
    _tick(rec)
    assert _n(backend) == 1
    clock.now = 16
    _tick(rec)
    assert _n(backend) == 2


def test_tick_inventory_every_60s(env) -> None:
    """Ticks at 0,15,30,45,60 collect inventory twice."""
    seed_backend(env)
    rec, backend, clock = _reconciler(env)
    for t in (0, 15, 30, 45, 60):
        clock.now = t
        _tick(rec)
    assert backend.inventory_calls == 2


def test_tick_skips_org_when_flags_off(env, monkeypatch) -> None:
    """With the second flag off nothing is built or reconciled."""
    b = seed_backend(env)
    seed_deployment(env, b, status=5)
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_VLLM_RAY", "0")
    rec, backend, _ = _reconciler(env)
    assert set(_tick(rec).values()) == {0} and backend.calls == []


def test_one_failing_deployment_does_not_stop_others(env, caplog) -> None:
    """A reconcile exception is logged and counted; the others still run."""
    b = seed_backend(env)
    bad = seed_deployment(env, b, name="bad", status=5)
    seed_deployment(env, b, name="good", status=5)
    backend = FakeBackend()
    real = backend.reconcile

    async def flaky(dep):
        if dep.id == bad.id:
            raise RuntimeError("ray down")
        return await real(dep)

    backend.reconcile = flaky  # type: ignore[method-assign]
    rec, _, _ = _reconciler(env, backend)
    with caplog.at_level(logging.WARNING):
        counts = _tick(rec)
    assert counts["reconcile_errors"] == 1 and counts["deployments_examined"] == 2
    assert "reconcile failed" in caplog.text


def test_serving_with_expose_creates_link_and_offer(env) -> None:
    """SERVING + expose: an enabled org-owned link, vllm-only offer, link id on the row."""
    b = seed_backend(env)
    dep = seed_deployment(env, b, status=4)
    rec, backend, _ = _reconciler(env)
    backend.reconcile_results[dep.id] = [5]
    counts = _tick(rec)
    link = _link(env)[0]
    assert (link.provider, link.enabled, link.owner_org_id) == ("vllm", True, 1)
    assert link.endpoint_url == dep.endpoint_url and link.model_list == ["model-7b"]
    caps = link.management_capabilities
    assert (caps["local_deployment_kind"], caps["local_deployment_id"]) == (1, dep.id)
    assert caps["unit_price_input"] and caps["fleet_status"] == 5
    offer = env(env.model_configs.model_name == "model-7b").select().first()
    assert offer.preferred_providers == ["vllm"] and offer.context_length == 4096
    row = env(env.vllm_deployments.id == dep.id).select().first()
    assert row.connection_link_id == link.id and counts["links_upserted"] == 1


def test_serving_without_expose_creates_nothing_enabled(env) -> None:
    """expose_via_proxy=False: no enabled link and no offer."""
    b = seed_backend(env)
    dep = seed_deployment(env, b, status=4, expose_via_proxy=False)
    rec, backend, _ = _reconciler(env)
    backend.reconcile_results[dep.id] = [5]
    _tick(rec)
    assert [r for r in _link(env) if r.enabled] == []
    assert env(env.model_configs.id > 0).count() == 0


def test_link_name_is_org_scoped(env) -> None:
    """Two orgs with the same deployment name get two distinct links."""
    b1, b2 = seed_backend(env), seed_backend(env, org_id=2, name="o2")
    seed_deployment(env, b1, name="same", status=5, slug="s-1")
    seed_deployment(env, b2, org_id=2, name="same", status=5, slug="s-2", served_model_name="m2")
    rec, _, _ = _reconciler(env)
    _tick(rec)
    assert sorted(r.name for r in _link(env)) == ["vllm-1-s-1", "vllm-2-s-2"]


def test_existing_commercial_model_config_untouched(env) -> None:
    """A same-named commercial offer is never modified; vLLM adds no competing row."""
    b = seed_backend(env)
    seed_deployment(env, b, status=5)
    _offer(env, ["openai"], max_tokens=8)
    rec, _, _ = _reconciler(env)
    _tick(rec)
    rows = list(env(env.model_configs.id > 0).select())
    assert len(rows) == 1 and rows[0].preferred_providers == ["openai"] and rows[0].max_tokens == 8


def test_degraded_keeps_link_enabled(env) -> None:
    """DEGRADED keeps the link enabled and rewrites fleet_status."""
    b = seed_backend(env)
    dep = seed_deployment(env, b, status=5)
    rec, backend, clock = _reconciler(env)
    _tick(rec)
    backend.reconcile_results[dep.id] = [6]
    clock.now = 20
    _tick(rec)
    link = _link(env)[0]
    assert link.enabled is True and link.management_capabilities["fleet_status"] == 6


@pytest.mark.parametrize("final", [7, 8])
def test_failed_and_stopped_disable_link_and_remove_offer(env, final) -> None:
    """FAILED/STOPPED disable the link and remove the offer."""
    b = seed_backend(env)
    dep = seed_deployment(env, b, status=5)
    rec, backend, clock = _reconciler(env)
    _tick(rec)
    assert env(env.model_configs.id > 0).count() == 1
    backend.reconcile_results[dep.id] = [final]
    clock.now = 20
    counts = _tick(rec)
    assert _link(env)[0].enabled is False and env(env.model_configs.id > 0).count() == 0
    assert counts["links_removed"] == 1


def test_prefer_local_rule_created_once_id_persisted(env) -> None:
    """The prefer-local rule function runs once; its id is stored in prefer_local."""
    b = seed_backend(env)
    dep = seed_deployment(
        env, b, status=5, prefer_local={"fallback_model": "gpt-4o", "match": {"team": "x"}}
    )
    calls: list[Any] = []

    async def make_rule(db, org_id, d, fallback, match):
        calls.append((org_id, fallback, match))
        return 77

    rec, _, clock = _reconciler(env, prefer_local_fn=make_rule)
    _tick(rec)
    clock.now = 100
    _tick(rec)
    assert calls == [(1, "gpt-4o", {"team": "x"})]
    row = env(env.vllm_deployments.id == dep.id).select().first()
    assert row.prefer_local["rule_id"] == 77


def test_price_change_resyncs_link_capabilities(env) -> None:
    """A changed price reaches the link on the next tick."""
    b = seed_backend(env)
    dep = seed_deployment(env, b, status=5, price_mode=2)
    rec, _, clock = _reconciler(env)
    _tick(rec)
    env(env.vllm_deployments.id == dep.id).update(price_output_per_1m="42")
    env.commit()
    clock.now = 20
    _tick(rec)
    assert Decimal(_link(env)[0].management_capabilities["unit_price_output"]) == Decimal("42")


def test_unlink_removes_link_offer_rule_only_if_owned(env) -> None:
    """Delete cleanup removes the link and a vllm-owned offer, keeps a shared one."""
    b = seed_backend(env)
    dep = seed_deployment(
        env, b, status=5, prefer_local={"fallback_model": "g", "match": {}, "rule_id": 5}
    )
    rec, _, _ = _reconciler(env)
    _tick(rec)
    asyncio.run(unlink_deployment(env, dep))
    assert _link(env) == [] and env(env.model_configs.id > 0).count() == 0
    assert "rule_id" not in env(env.vllm_deployments.id == dep.id).select().first().prefer_local
    _offer(env, ["openai", "vllm"])
    asyncio.run(unlink_deployment(env, dep))
    assert env(env.model_configs.id > 0).count() == 1


def test_delete_in_flight_leaves_no_orphans(env) -> None:
    """After the delete path the link count and vllm offers are zero."""
    b = seed_backend(env)
    dep = seed_deployment(env, b, status=5)
    rec, _, _ = _reconciler(env)
    _tick(rec)
    asyncio.run(unlink_deployment(env, dep))
    assert _link(env) == []
    assert env(env.model_configs.model_name == "model-7b").count() == 0


def _auto(env, **over):
    b = seed_backend(env)
    dep = seed_deployment(env, b, status=5, price_mode=1, **over)
    return b, dep


def test_auto_price_recomputed_daily_from_observed_p50(env) -> None:
    """Daily recompute uses the observed throughput, stores it, upserts the rate, logs an event."""
    _, dep = _auto(env)
    usage = FakeUsage(40.0)
    rec, _, clock = _reconciler(env, usage_source=usage)
    assert _tick(rec)["prices_recalculated"] == 1
    clock.now = 86399
    assert _tick(rec)["prices_recalculated"] == 0
    clock.now = 86400
    assert _tick(rec)["prices_recalculated"] == 1
    row = env(env.vllm_deployments.id == dep.id).select().first()
    assert float(row.observed_tokens_per_s) == 40.0
    assert row.price_breakdown["throughput_source"] == "observed"
    rate = (
        env(
            (env.token_conversion_rates.provider == "vllm")
            & (env.token_conversion_rates.model == f"deployment/{dep.public_id}")
        )
        .select()
        .first()
    )
    assert rate.input_rate == pytest.approx(0.001 * 1e6 / float(row.price_input_per_1m), rel=1e-4)
    assert rate.output_rate == pytest.approx(0.001 * 1e6 / float(row.price_output_per_1m), rel=1e-4)
    events = env(env.vllm_deployment_events.message == "price recalculated").count()
    assert events == 2 and usage.calls[0] == (1, dep.id)


def test_auto_price_recompute_without_observation_uses_estimate(env) -> None:
    """No observation keeps the estimate source."""
    _, dep = _auto(env)
    rec, _, _ = _reconciler(env, usage_source=FakeUsage(None))
    _tick(rec)
    row = env(env.vllm_deployments.id == dep.id).select().first()
    assert row.price_breakdown["throughput_source"] == "estimate"
    assert row.observed_tokens_per_s is None


def test_manual_price_never_recomputed(env) -> None:
    """MANUAL deployments are never touched."""
    b = seed_backend(env)
    dep = seed_deployment(env, b, status=5, price_mode=2, price_output_per_1m="77")
    rec, _, _ = _reconciler(env, usage_source=FakeUsage(40.0))
    assert _tick(rec)["prices_recalculated"] == 0
    assert (
        Decimal(str(env(env.vllm_deployments.id == dep.id).select().first().price_output_per_1m))
        == 77
    )


def test_price_recompute_error_keeps_old_price_counts_error(env) -> None:
    """A compute error (no cost config for the class) leaves the price and counts an error."""
    b = seed_backend(env, config={**BACKEND_CONFIG, "worker_groups": []})
    dep = seed_deployment(env, b, status=5, price_mode=1, price_output_per_1m="5.0")
    rec, _, _ = _reconciler(env, usage_source=FakeUsage(40.0))
    counts = _tick(rec)
    assert counts["reconcile_errors"] == 1 and counts["prices_recalculated"] == 0
    assert (
        Decimal(str(env(env.vllm_deployments.id == dep.id).select().first().price_output_per_1m))
        == 5
    )


def test_run_forever_survives_exceptions_and_cancels_cleanly(env) -> None:
    """A failing tick is logged and the loop keeps going; cancellation exits quietly."""
    rec, _, _ = _reconciler(env)
    ticks = {"n": 0}

    async def boom() -> dict[str, int]:
        ticks["n"] += 1
        raise RuntimeError("db gone")

    async def sleeper(_s: float) -> None:
        if ticks["n"] >= 2:
            raise asyncio.CancelledError

    rec.tick = boom  # type: ignore[method-assign]
    rec.sleep = sleeper
    asyncio.run(rec.run_forever())
    assert ticks["n"] == 2


def test_pg_guard_false_on_non_postgres_never_raises(tmp_path) -> None:
    """A sqlite engine yields False, not an exception."""
    guard = PgAdvisoryLeaderGuard(create_engine(f"sqlite:///{tmp_path / 'g.db'}"))
    assert asyncio.run(guard.acquire()) is False
    broken = PgAdvisoryLeaderGuard(object())
    assert asyncio.run(broken.acquire()) is False


def test_pull_poller_is_consulted_for_pending_rows(env) -> None:
    """PENDING/PULLING rows go through the pull poller before a Serve reconcile."""
    b = seed_backend(env)
    seed_deployment(env, b, status=2)
    seen: list[int] = []

    async def poller(db, backend, dep):
        seen.append(dep.id)
        return False

    rec, backend, _ = _reconciler(env, pull_poller=poller)
    _tick(rec)
    assert len(seen) == 1 and [c for c in backend.calls if c[0] == "reconcile"] == []


# --- org isolation of the daily AUTO re-pricing (security review) ----------------------------

LABEL = "waddleai.penguintech.io/gpu-classes"


def _org_config(kwh: float) -> dict[str, Any]:
    return {**BACKEND_CONFIG, "tco": {"kwh_usd": kwh}}


def _local_rows(db, org_backend_ids: dict[int, int]):
    """One running AUTO Ollama and llama.cpp deployment per org, owned via fleet_backend_id."""
    selector = {LABEL: "nvidia-rtx4090"}
    for org, backend_id in org_backend_ids.items():
        common = {"status": "running", "price_mode": 1, "fleet_backend_id": backend_id}
        db.ollama_deployments.insert(
            name=f"ollama_deployments-{org}",
            endpoint_url="http://o",
            gpu_config={"node_selector": selector, "gpu_count": 1},
            public_id=f"{org:08d}-0000-0000-0000-000000000000",
            **common,
        )
        db.llamacpp_deployments.insert(
            name=f"llamacpp_deployments-{org}",
            model_name="m7b",
            node_selector=selector,
            gpu_count=1,
            deployment_type="kubernetes",
            public_id=f"{org:08d}-0000-0000-0000-000000000001",
            **common,
        )
    db.commit()


@pytest.fixture
def two_orgs(env):
    """Org 1 (cheap power) and org 2 (expensive power), each with a vllm_ray backend and params."""
    for name in ("ollama_deployments-1", "ollama_deployments-2"):
        env.model_registry.insert(
            name=name, role="chat", license="mit", origin="o", params_b=7,
            public_id=f"1000000{name[-1]}-0000-0000-0000-000000000000", is_utility=False,
        )  # fmt: skip
    env.model_registry.insert(
        name="m7b", role="chat", license="mit", origin="o", params_b=7,
        public_id="20000000-0000-0000-0000-000000000000", is_utility=False,
    )  # fmt: skip
    b1 = seed_backend(env, org_id=1, name="b1", config=_org_config(0.05))
    b2 = seed_backend(env, org_id=2, name="b2", config=_org_config(0.90))
    _local_rows(env, {1: b1, 2: b2})
    return env, b1, b2


def _price(db, table: str, org: int) -> Decimal:
    row = db(getattr(db, table).name == f"{table}-{org}").select().first()
    return Decimal(str(row.price_output_per_1m))


def test_local_recompute_prices_each_org_with_its_own_config(two_orgs) -> None:
    """Org 1 and org 2 deployments are priced from their own org's TCO inputs, not each other's."""
    env, _b1, _b2 = two_orgs
    rec, _, _ = _reconciler(env, usage_source=FakeUsage(None))
    counts = _tick(rec)
    assert counts["prices_recalculated"] == 4
    for table in ("ollama_deployments", "llamacpp_deployments"):
        cheap, dear = _price(env, table, 1), _price(env, table, 2)
        assert 0 < cheap < dear
        row1 = env(getattr(env, table).name == f"{table}-1").select().first()
        assert row1.price_breakdown["inputs"]["kwh_usd"] == 0.05
        row2 = env(getattr(env, table).name == f"{table}-2").select().first()
        assert row2.price_breakdown["inputs"]["kwh_usd"] == 0.90


def test_local_recompute_skips_row_with_no_owning_org(two_orgs, caplog) -> None:
    """A deployment with no fleet_backend_id is skipped with a warning, never priced."""
    env, _b1, _b2 = two_orgs
    env.ollama_deployments.insert(
        name="orphan", endpoint_url="http://x", status="running", price_mode=1,
        gpu_config={"node_selector": {LABEL: "nvidia-rtx4090"}, "gpu_count": 1},
        public_id="30000000-0000-0000-0000-000000000000",
    )  # fmt: skip
    env.commit()
    rec, _, _ = _reconciler(env, usage_source=FakeUsage(None))
    with caplog.at_level(logging.WARNING):
        _tick(rec)
    orphan = env(env.ollama_deployments.name == "orphan").select().first()
    assert orphan.price_breakdown is None and Decimal(str(orphan.price_output_per_1m)) == 0
    assert "no owning backend" in caplog.text


def test_local_recompute_only_first_backend_of_an_org_prices(two_orgs) -> None:
    """A second vllm_ray backend in the same org does not re-price (priced once, by the first)."""
    env, b1, _b2 = two_orgs
    seed_backend(env, org_id=1, name="b1b", config=_org_config(0.77))
    rec, _, _ = _reconciler(env, usage_source=FakeUsage(None))
    _tick(rec)
    row = env(env.ollama_deployments.name == "ollama_deployments-1").select().first()
    assert row.price_breakdown["inputs"]["kwh_usd"] == 0.05


def test_vllm_deployments_of_another_org_on_a_backend_are_ignored(env) -> None:
    """A row whose org differs from its backend's org is never reconciled by that backend."""
    b1 = seed_backend(env, org_id=1)
    seed_deployment(env, b1, org_id=2, name="alien", status=5)
    rec, backend, _ = _reconciler(env)
    _tick(rec)
    assert [c for c in backend.calls if c[0] == "reconcile"] == []


# --- deployment-scoped offers (security review) -----------------------------------------------


def test_offer_carries_org_and_deployment_scope(env) -> None:
    """The offer is stamped with the owning org and the deployment's public_id."""
    b = seed_backend(env)
    dep = seed_deployment(env, b, status=5)
    rec, _, _ = _reconciler(env)
    _tick(rec)
    offer = env(env.model_configs.model_name == "model-7b").select().first()
    assert offer.owner_org_id == 1 and str(offer.deployment_public_id) == str(dep.public_id)


def test_second_deployment_with_same_served_name_never_takes_over_the_offer(env, caplog) -> None:
    """A later deployment never takes another deployment's offer; its unlink is a no-op."""
    b = seed_backend(env)
    first = seed_deployment(env, b, name="a", status=5, slug="a-1")
    second = seed_deployment(env, b, name="b", status=5, slug="b-1")
    rec, _, _ = _reconciler(env)
    with caplog.at_level(logging.WARNING):
        _tick(rec)
    offers = list(env(env.model_configs.id > 0).select())
    assert len(offers) == 1 and str(offers[0].deployment_public_id) == str(first.public_id)
    assert "offer" in caplog.text
    asyncio.run(unlink_deployment(env, second))
    assert env(env.model_configs.id > 0).count() == 1  # A's offer survives B's deletion
    asyncio.run(unlink_deployment(env, first))
    assert env(env.model_configs.id > 0).count() == 0


def test_unlink_never_deletes_an_offer_owned_by_someone_else(env) -> None:
    """Even a vllm-only offer is kept when it belongs to another deployment or is unscoped."""
    b = seed_backend(env)
    dep = seed_deployment(env, b, status=5)
    _offer(
        env, ["vllm"], deployment_public_id="99999999-9999-9999-9999-999999999999", owner_org_id=2
    )
    asyncio.run(unlink_deployment(env, dep))
    assert env(env.model_configs.id > 0).count() == 1
    env(env.model_configs.id > 0).delete()
    _offer(env, ["vllm"])  # legacy, unscoped
    asyncio.run(unlink_deployment(env, dep))
    assert env(env.model_configs.id > 0).count() == 1
