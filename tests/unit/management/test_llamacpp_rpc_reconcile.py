"""Plan F, F6: RPC status derivation, reconcile loop, head restart on recovery, link sync."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from services.management.app import build_llamacpp_rpc_reconciler
from services.management.app.services.llamacpp_manager import LlamaCppManager, RpcObservation
from services.management.app.services.llamacpp_rpc_reconcile import (
    RPC_ADVISORY_LOCK_KEY,
    LlamaCppRpcReconciler,
    derive_rpc_status,
)
from services.management.app.services.vllm_reconciler import AlwaysLeader, PgAdvisoryLeaderGuard
from shared.observability import vllm_metrics

# total, up, head_ready, was_running, reloading, failed, down, expect_status, expect_fragment
CASES = [
    (3, 0, False, False, False, None, ["w0", "w1", "w2"], "deploying", "starting workers 0/3"),
    (3, 3, False, False, False, None, [], "deploying", "loading model"),
    (3, 3, True, False, False, None, [], "running", "serving across 3 GPUs"),
    (3, 2, True, True, False, None, ["w1"], "degraded", "worker w1 down (2/3 up)"),
    (3, 3, False, True, True, None, [], "deploying", "reloading model"),
    (
        3,
        2,
        False,
        False,
        False,
        "CrashLoopBackOff on w2",
        ["w2"],
        "error",
        "CrashLoopBackOff on w2",
    ),
    (3, 3, False, True, False, None, [], "deploying", "reloading model"),
    (3, 1, False, True, False, None, ["w1", "w2"], "degraded", "worker w1, w2 down (1/3 up)"),
    (3, 2, True, False, False, None, ["w0"], "degraded", "worker w0 down (2/3 up)"),
    (3, 2, True, True, False, None, [], "degraded", "worker unknown down"),
]


@pytest.mark.parametrize("case", CASES, ids=[f"{c[7]}-{i}" for i, c in enumerate(CASES)])
def test_derive_rpc_status_timeline(case):
    """Every timeline row maps to its status and message."""
    total, up, ready, was_running, reloading, failed, down, status, fragment = case
    health = derive_rpc_status(
        workers_total=total, workers_up=up, head_ready=ready, was_running=was_running,
        head_reloading=reloading, failed_message=failed, workers_down=down,
    )  # fmt: skip
    assert health.status == status and fragment in health.message
    assert (health.workers_total, health.workers_up, health.head_ready) == (total, up, ready)


def test_derive_serving_message_counts_head_gpus_and_nodes():
    """Serving reports workers + head GPUs and the node count."""
    health = derive_rpc_status(
        workers_total=2, workers_up=2, head_ready=True, was_running=False, head_reloading=False,
        failed_message=None, workers_down=[], head_gpus=2, nodes=2,
    )  # fmt: skip
    assert health.message == "serving across 4 GPUs on 2 nodes"


class Clock:
    """A manual monotonic clock."""

    def __init__(self) -> None:
        """Start at t=100."""
        self.now = 100.0

    def __call__(self) -> float:
        """Current time."""
        return self.now


class Observed:
    """A scripted observer: ``seen`` is returned (or raised) for every call."""

    def __init__(self, seen: RpcObservation | Exception) -> None:
        """Remember the scripted answer."""
        self.seen = seen
        self.calls = 0

    def __call__(self, dep: Any) -> RpcObservation:
        """Return or raise the scripted observation."""
        self.calls += 1
        if isinstance(self.seen, Exception):
            raise self.seen
        return self.seen


def obs(total=3, up=3, down=(), head=True, failed=None) -> RpcObservation:
    """An observation."""
    return RpcObservation(total, up, list(down), head, failed)


class Restarts:
    """Records head restarts; ``fail`` scripts an error."""

    def __init__(self) -> None:
        """No restarts yet."""
        self.calls: list[tuple[str, str]] = []
        self.fail = False

    async def __call__(self, kind: str, name: str) -> int:
        """Record (or fail) one restart bump."""
        if self.fail:
            raise RuntimeError("apiserver unavailable")
        self.calls.append((kind, name))
        return len(self.calls)


@pytest.fixture
def world(vllm_db, both_flags):
    """A real sqlite DB with one RPC row, its key link and a reconciler factory."""

    def make_row(status="running", **over):
        """Make row."""
        values = dict(
            name="qwen-rpc", deployment_type="kubernetes", status=status, model_name="m120",
            endpoint_url="http://b-svc.waddleai:8080",
            k8s_daemonset_name="waddleai-llamacpp-qwen-rpc",
            public_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", owner_org_id=1, topology=2,
            gpu_count=2, rpc_head_node_uid="uid-a", tensor_split="0.25,0.25,0.25,0.25",
            rpc_workers=[
                {"node_uid": "uid-b", "gpu_class": "c", "gpu_count": 1, "tensor_split_share": 0.25},
                {"node_uid": "uid-b", "gpu_class": "c", "gpu_count": 1, "tensor_split_share": 0.25},
            ],
            price_input_per_1m="1.5", price_output_per_1m="7.5",
        )  # fmt: skip
        values.update(over)
        row_id = vllm_db.llamacpp_deployments.insert(**values)
        vllm_db.connection_links.insert(
            name=f"llamacpp-{values['public_id']}", provider="llamacpp", endpoint_url="",
            enabled=False, owner_org_id=values["owner_org_id"],
        )  # fmt: skip
        vllm_db.commit()
        return row_id

    def reconciler(observer, restarts=None, clock=None, guard=None):
        """Reconciler."""
        restarts = restarts or Restarts()
        rec = LlamaCppRpcReconciler(
            vllm_db, guard=guard or AlwaysLeader(), observer=observer, restarter=restarts,
            clock=clock or Clock(),
        )  # fmt: skip
        return rec, restarts

    def row(row_id):
        """Row."""
        return vllm_db(vllm_db.llamacpp_deployments.id == row_id).select().first()

    def link(row_id):
        """Link."""
        return (
            vllm_db(vllm_db.connection_links.owner_org_id == row(row_id).owner_org_id)
            .select()
            .first()
        )

    return SimpleNamespace(db=vllm_db, make_row=make_row, reconciler=reconciler, row=row, link=link)


async def test_serving_row_marks_running_and_enables_link_with_capabilities(world):
    """A ready head and all workers: running, link enabled with the llama.cpp capabilities."""
    row_id = world.make_row(status="deploying")
    rec, _ = world.reconciler(Observed(obs()))
    counts = await rec.tick()
    row = world.row(row_id)
    assert (row.status, row.status_message) == ("running", "serving across 5 GPUs on 2 nodes")
    assert counts["status_changes"] == 1 and counts["links_synced"] == 1
    link = world.link(row_id)
    assert link.enabled is True and link.endpoint_url == "http://b-svc.waddleai:8080"
    caps = link.management_capabilities
    assert (caps["local_deployment_kind"], caps["local_deployment_id"]) == (3, row_id)
    assert (caps["unit_price_input"], caps["unit_price_output"]) == ("1.500000", "7.500000")
    again = await rec.tick()
    assert again["status_changes"] == 0 and again["links_synced"] == 0


async def test_worker_pod_dies_marks_degraded_keeps_row_and_writes_no_usage(world):
    """Review focus 1: degraded names the worker, the row stays, the link stops routing."""
    row_id = world.make_row(status="running")
    world.db(world.db.connection_links.id > 0).update(enabled=True)
    rec, restarts = world.reconciler(Observed(obs(up=2, down=["pod-1"])))
    counts = await rec.tick()
    row = world.row(row_id)
    assert row is not None and row.status == "degraded"
    assert "worker pod-1 down (2/3 up)" in row.status_message
    assert counts["status_changes"] == 1 and restarts.calls == []
    assert world.link(row_id).enabled is False
    assert world.db(world.db.token_usage.id > 0).count() == 0


async def test_recovery_restarts_head_once_then_serves(world):
    """All workers back after degraded: one head restart, 'reloading model', then running."""
    row_id = world.make_row(status="degraded")
    clock = Clock()
    seen = Observed(obs(up=3, head=True))
    rec, restarts = world.reconciler(seen, clock=clock)
    first = await rec.tick()
    base = "waddleai-llamacpp-qwen-rpc"
    assert restarts.calls == [("Deployment", base)] and first["head_restarts"] == 1
    assert (world.row(row_id).status, world.row(row_id).status_message) == (
        "deploying", "reloading model",
    )  # fmt: skip
    clock.now += 30
    second = await rec.tick()  # head still reports ready for a moment: no second bump
    assert second["head_restarts"] == 0 and restarts.calls == [("Deployment", base)]
    seen.seen = obs(up=3, head=True)  # head restarted and is ready again
    rec._reloading.add(row_id)  # the restart took effect; the next ready observation clears it
    await rec.tick()
    assert world.row(row_id).status == "running"


async def test_head_restart_failure_is_counted_not_raised(world):
    """A failed restart bump keeps the row degraded and counts a reconcile error."""
    row_id = world.make_row(status="degraded")
    restarts = Restarts()
    restarts.fail = True
    rec, _ = world.reconciler(Observed(obs(up=3)), restarts=restarts)
    counts = await rec.tick()
    assert counts["reconcile_errors"] == 1 and counts["head_restarts"] == 0
    assert world.row(row_id).status == "running" or world.row(row_id).status == "degraded"


async def test_kubernetes_failure_is_error_and_recovers(world):
    """CrashLoopBackOff -> error (row kept); once clear the deployment recovers."""
    row_id = world.make_row(status="deploying")
    seen = Observed(obs(up=2, head=False, failed="CrashLoopBackOff on pod-2", down=["pod-2"]))
    rec, _ = world.reconciler(seen)
    await rec.tick()
    assert world.row(row_id).status == "error"
    assert "CrashLoopBackOff on pod-2" in world.row(row_id).status_message
    seen.seen = obs()
    await rec.tick()
    assert world.row(row_id).status == "running"


async def test_observation_error_counted_and_row_untouched(world):
    """An unreadable cluster never changes a row; the error is counted."""
    row_id = world.make_row(status="running")
    rec, _ = world.reconciler(Observed(RuntimeError("k8s down")))
    counts = await rec.tick()
    assert counts == {
        "deployments_examined": 1, "status_changes": 0, "head_restarts": 0,
        "links_synced": 0, "reconcile_errors": 1,
    }  # fmt: skip
    assert world.row(row_id).status == "running"


async def test_non_leader_does_nothing(world):
    """A process that is not the leader examines nothing."""

    class Follower:
        async def acquire(self) -> bool:
            """Never the leader."""
            return False

    world.make_row()
    observed = Observed(obs())
    rec, _ = world.reconciler(observed, guard=Follower())
    counts = await rec.tick()
    assert observed.calls == 0 and counts["deployments_examined"] == 0


async def test_flag_off_and_unowned_and_stopped_rows_are_skipped(world, monkeypatch):
    """Rows of flag-off orgs, unowned rows, and pending/stopped rows are never examined."""
    world.make_row(status="stopped", public_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", name="s")
    world.make_row(status="pending", public_id="cccccccc-cccc-cccc-cccc-cccccccccccc", name="p")
    world.make_row(owner_org_id=None, public_id="dddddddd-dddd-dddd-dddd-dddddddddddd", name="u")
    world.make_row(status="running", public_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", name="ok")
    observed = Observed(obs())
    rec, _ = world.reconciler(observed)
    assert (await rec.tick())["deployments_examined"] == 1
    monkeypatch.delenv("WADDLEAI_FLAG_FLEET_VLLM_RAY")
    observed.calls = 0
    assert (await rec.tick())["deployments_examined"] == 0 and observed.calls == 0


async def test_load_time_recorded_once_and_skipped_after_restart(world, caplog, monkeypatch):
    """Deploying -> running records the load time; a restart (no start time) logs and skips."""
    recorded: list[float] = []
    monkeypatch.setattr(
        "services.management.app.services.llamacpp_rpc_reconcile.record_rpc_load_seconds",
        recorded.append,
    )
    row_id = world.make_row(status="deploying")
    clock = Clock()
    seen = Observed(obs(up=1, head=False))
    rec, _ = world.reconciler(seen, clock=clock)
    await rec.tick()
    clock.now += 37.5
    seen.seen = obs()
    await rec.tick()
    assert recorded == [37.5]
    rec2, _ = world.reconciler(Observed(obs()), clock=clock)
    world.db(world.db.llamacpp_deployments.id == row_id).update(status="deploying")
    with caplog.at_level(logging.INFO):
        await rec2.tick()
    assert recorded == [37.5] and "load time not recorded" in caplog.text


async def test_workers_up_gauge_snapshot_replaced_each_tick(world):
    """The per-deployment gauge reports ready workers and drops deployments that vanish."""
    world.make_row(status="running")
    rec, _ = world.reconciler(Observed(obs(up=2, down=["x"])))
    await rec.tick()
    assert vllm_metrics._rpc_workers_up == {"qwen-rpc": 2}
    world.db(world.db.llamacpp_deployments.id > 0).delete()
    world.db.commit()
    await rec.tick()
    assert vllm_metrics._rpc_workers_up == {}


async def test_link_missing_is_not_an_error(world):
    """A row without its key link still reconciles its status."""
    row_id = world.make_row(status="deploying")
    world.db(world.db.connection_links.id > 0).delete()
    rec, _ = world.reconciler(Observed(obs()))
    counts = await rec.tick()
    assert world.row(row_id).status == "running" and counts["links_synced"] == 0


async def test_run_forever_logs_tick_failures_and_cancels_cleanly(world, caplog):
    """The loop survives a failing tick and exits when cancelled."""
    rec, _ = world.reconciler(Observed(obs()))
    ticks = 0

    async def boom() -> dict[str, int]:
        """Boom."""
        nonlocal ticks
        ticks += 1
        raise RuntimeError("tick failed")

    rec.tick = boom  # type: ignore[method-assign]

    async def sleep(_s: float) -> None:
        """Sleep."""
        await asyncio.sleep(0)

    rec.sleep = sleep
    with caplog.at_level(logging.WARNING):
        task = asyncio.ensure_future(rec.run_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert ticks >= 2 and "tick failed" in caplog.text and task.done()


def test_builder_disabled_no_engine_and_guard_key(monkeypatch, tmp_path):
    """The builder honours its kill switch, needs an engine, and uses its own lock key."""
    from sqlalchemy import create_engine

    app = SimpleNamespace(logger=logging.getLogger("rpc-startup"))
    monkeypatch.setenv("WADDLEAI_LLAMACPP_RPC_RECONCILER", "0")
    assert build_llamacpp_rpc_reconciler(app) is None
    monkeypatch.delenv("WADDLEAI_LLAMACPP_RPC_RECONCILER")
    monkeypatch.setattr("services.management.app.extensions.db", SimpleNamespace(engine=None))
    assert build_llamacpp_rpc_reconciler(app) is None
    engine = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    monkeypatch.setattr("services.management.app.extensions.db", SimpleNamespace(engine=engine))
    rec = build_llamacpp_rpc_reconciler(app)
    assert isinstance(rec, LlamaCppRpcReconciler) and isinstance(rec.guard, PgAdvisoryLeaderGuard)
    assert rec.guard._key == RPC_ADVISORY_LOCK_KEY and rec.guard._key != 7270011


def _pod(name, role, ready=True, waiting=None, init_waiting=None):
    """A Kubernetes pod stand-in."""

    def status(reason):
        """Status."""
        state = SimpleNamespace(waiting=SimpleNamespace(reason=reason) if reason else None)
        return SimpleNamespace(state=state)

    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={"waddleai.penguintech.io/llamacpp-role": role}),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Ready", status="True" if ready else "False")],
            container_statuses=[status(waiting)],
            init_container_statuses=[status(init_waiting)],
        ),
    )


def _observe(monkeypatch, *, pods, statefulsets, head_conditions=()):
    """Run ``LlamaCppManager.rpc_observe`` against scripted Kubernetes list/read answers."""
    from unittest.mock import MagicMock

    apps, core = MagicMock(), MagicMock()
    apps.list_namespaced_stateful_set.return_value = SimpleNamespace(items=statefulsets)
    core.list_namespaced_pod.return_value = SimpleNamespace(items=pods)
    apps.read_namespaced_deployment.return_value = SimpleNamespace(
        status=SimpleNamespace(conditions=list(head_conditions))
    )
    mod = "services.management.app.services.llamacpp_manager"
    monkeypatch.setattr(f"{mod}.get_k8s_apps_client", lambda: apps)
    monkeypatch.setattr(f"{mod}.get_k8s_core_client", lambda: core)
    dep = SimpleNamespace(
        name="qwen-rpc",
        k8s_daemonset_name="waddleai-llamacpp-qwen-rpc",
        k8s_namespace="waddleai",
        public_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    return LlamaCppManager(db=None).rpc_observe(dep), apps, core


def _sts(name, replicas):
    """A StatefulSet stand-in."""
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name), spec=SimpleNamespace(replicas=replicas)
    )


def test_rpc_observe_counts_ready_workers_and_names_missing_pods(monkeypatch):
    """A not-yet-created pod and an unready pod both count as down, by expected name."""
    seen, apps, core = _observe(
        monkeypatch,
        pods=[
            _pod("b-rpc-0-0", "worker"),
            _pod("b-rpc-0-1", "worker", ready=False),
            _pod("b", "head"),
        ],
        statefulsets=[_sts("b-rpc-0", 2), _sts("b-rpc-1", 1)],
    )
    assert (seen.workers_total, seen.workers_up) == (3, 1)
    assert seen.workers_down == ["b-rpc-0-1", "b-rpc-1-0"] and seen.head_ready is True
    assert seen.failed_message is None
    selector = apps.list_namespaced_stateful_set.call_args.kwargs["label_selector"]
    assert selector == "waddleai.penguintech.io/llamacpp-deployment=waddleai-llamacpp-qwen-rpc"


def test_rpc_observe_reports_terminal_failures(monkeypatch):
    """CrashLoopBackOff in a container or init container, and ProgressDeadlineExceeded -> failed."""
    seen, *_ = _observe(
        monkeypatch,
        pods=[_pod("b-rpc-0-0", "worker", ready=False, waiting="CrashLoopBackOff")],
        statefulsets=[_sts("b-rpc-0", 1)],
    )
    assert seen.failed_message == "CrashLoopBackOff on b-rpc-0-0"
    seen, *_ = _observe(
        monkeypatch,
        pods=[_pod("b", "head", ready=False, init_waiting="ImagePullBackOff")],
        statefulsets=[],
    )
    assert seen.failed_message == "ImagePullBackOff on b" and seen.head_ready is False
    seen, *_ = _observe(
        monkeypatch,
        pods=[],
        statefulsets=[],
        head_conditions=[SimpleNamespace(reason="ProgressDeadlineExceeded")],
    )
    assert seen.failed_message == "ProgressDeadlineExceeded on waddleai-llamacpp-qwen-rpc"


async def test_write_is_conditional_on_the_observed_status(world):
    """M5 regression: a row removed while we observed must not be resurrected by a stale write."""
    row_id = world.make_row(status="running")

    def observer_that_races(dep: Any) -> RpcObservation:
        """Observe 'degraded' while a concurrent /remove stops the row."""
        world.db(world.db.llamacpp_deployments.id == row_id).update(status="stopped")
        world.db.commit()
        return obs(up=2, down=["pod-1"])

    rec, _ = world.reconciler(observer_that_races)
    counts = await rec.tick()
    assert world.row(row_id).status == "stopped"  # not overwritten with 'degraded'
    assert counts["status_changes"] == 0 and counts["reconcile_errors"] == 0


async def test_apply_failure_is_counted_and_never_aborts_the_tick(world, monkeypatch):
    """M5 regression: one row whose write/link sync blows up cannot abort the pass."""
    first = world.make_row(status="deploying")
    second = world.make_row(
        status="deploying",
        name="other",
        k8s_daemonset_name="waddleai-llamacpp-other",
        public_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )
    rec, _ = world.reconciler(Observed(obs()))
    real = rec._sync_link

    def flaky(dep: Any, serving: bool) -> bool:
        """Fail for the first row only."""
        if int(dep.id) == first:
            raise RuntimeError("link table locked")
        return real(dep, serving)

    monkeypatch.setattr(rec, "_sync_link", flaky)
    counts = await rec.tick()
    assert counts["reconcile_errors"] == 1 and counts["deployments_examined"] == 2
    assert world.row(second).status == "running"  # the next row was still reconciled


async def test_missing_head_deployment_is_error_status_and_disables_link(world):
    """M7 regression: 'head deployment missing' -> status error, link stops routing."""
    row_id = world.make_row(status="running")
    world.db(world.db.connection_links.id > 0).update(enabled=True)
    rec, _ = world.reconciler(Observed(obs(up=3, head=False, failed="head deployment missing")))
    counts = await rec.tick()
    row = world.row(row_id)
    assert (row.status, row.status_message) == ("error", "head deployment missing")
    assert counts["status_changes"] == 1 and world.link(row_id).enabled is False


async def test_recovery_restart_refuses_a_row_without_an_object_name(world):
    """L8 regression: no name-only guess for the head restart target; counted, nothing bumped."""
    world.make_row(status="degraded", k8s_daemonset_name=None)
    rec, restarts = world.reconciler(Observed(obs()))
    counts = await rec.tick()
    assert restarts.calls == [] and counts["reconcile_errors"] >= 1 and counts["head_restarts"] == 0
