"""Tests for NerEngine: warm-up ordering, load shedding, deadlines, crash recovery."""

# ruff: noqa: D103

from __future__ import annotations

import asyncio
import dataclasses
import os
import signal
import socket
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import engine_fakes as fk
import pytest

from services.classification.app import engine as engine_mod
from services.classification.app.engine import (
    EngineAnalysisError,
    EngineOverloaded,
    EngineTimeout,
    EngineUnavailable,
    EngineWorkerFailure,
    NerEngine,
    RawEntity,
    _worker_init,
    _worker_probe,
)
from shared.security import ner_filter


def _engine(**kw: Any) -> NerEngine:
    base: dict[str, Any] = {
        "workers": 2,
        "max_queue_depth": 4,
        "worker": fk.fake_worker,
        "initializer": fk.init_good,
        "probe": fk.probe_good,
        "start_timeout_s": 60.0,
    }
    base.update(kw)
    return NerEngine(**base)


@pytest.fixture
async def engine() -> AsyncIterator[NerEngine]:
    """Started fake-backed engine, closed after the test."""
    eng = _engine()
    await eng.start()
    yield eng
    await eng.aclose()


async def test_start_marks_ready_only_after_all_workers_warm() -> None:
    eng = _engine(initializer=fk.init_slow)
    assert eng.ready is False
    task = asyncio.create_task(eng.start())
    await asyncio.sleep(0.1)
    assert eng.ready is False  # workers still inside the slow initializer
    await task
    assert eng.ready is True
    assert eng.backend == "presidio"
    assert eng.fingerprint == "presidio|fake_model|1.0|presidio=2.2"
    await eng.aclose()
    assert eng.ready is False


async def test_start_with_missing_model_stays_not_ready_no_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WADDLEAI_NER_ALLOW_DOWNLOAD", raising=False)
    calls: list[object] = []

    def _no_net(self: socket.socket, *a: object) -> None:
        calls.append(a)
        raise AssertionError("network call attempted")

    monkeypatch.setattr(socket.socket, "connect", _no_net)
    # Real initializer, in-process, so the socket guard covers the model path too.
    # Module state it writes is reset by monkeypatch so no other test sees it.
    monkeypatch.setattr(engine_mod, "_barrier", None)
    monkeypatch.setattr(engine_mod, "_info", engine_mod.WorkerInfo("none", "", "", ""))
    monkeypatch.setattr(ner_filter, "_strict_worker_filter", None)
    barrier = _FakeBarrier()
    _worker_init("definitely_not_an_installed_model", barrier)
    info = _worker_probe(1.0)
    assert info.backend == "none"
    assert calls == []

    eng = _engine(initializer=fk.init_missing, probe=fk.probe_missing)
    await eng.start()
    assert eng.ready is False
    with pytest.raises(EngineUnavailable):
        await eng.analyze("hello", "en", 1.0)
    await eng.aclose()


class _FakeBarrier:
    """Single-party stand-in for multiprocessing.Barrier."""

    def wait(self, timeout: float | None = None) -> int:
        return 0


async def test_unstarted_engine_is_unavailable() -> None:
    with pytest.raises(EngineUnavailable):
        await _engine().analyze("x", "en", 1.0)


async def test_start_failure_leaves_not_ready() -> None:
    eng = _engine(workers=2, initializer=fk.init_raise)
    await eng.start()  # initializer failure breaks the pool -> swallowed, not ready
    assert eng.ready is False
    await eng.aclose()


async def test_analyze_returns_spans_only(engine: NerEngine) -> None:
    out = await engine.analyze("hello", "en", 10.0)
    assert out == [RawEntity("PERSON", 0, 5, 0.9)]
    assert all("Alice" not in repr(e) for e in out)


def test_raw_entity_has_no_text_attribute() -> None:
    names = {f.name for f in dataclasses.fields(RawEntity)}
    assert names == {"entity_type", "start", "end", "score"}
    assert not hasattr(RawEntity("PERSON", 0, 1, 0.5), "text")
    with pytest.raises(AttributeError):
        RawEntity("PERSON", 0, 1, 0.5).text = "x"  # type: ignore[attr-defined]  # slots forbids it


async def test_queue_over_max_depth_raises_overloaded_immediately() -> None:
    eng = _engine(max_queue_depth=2, workers=1)
    await eng.start()
    busy = [asyncio.create_task(eng.analyze("sleep", "en", 10.0)) for _ in range(2)]
    await asyncio.sleep(0.2)
    t0 = asyncio.get_running_loop().time()
    with pytest.raises(EngineOverloaded):
        await eng.analyze("hello", "en", 10.0)
    assert asyncio.get_running_loop().time() - t0 < 0.5  # shed, not queued behind the sleepers
    assert eng._inflight == 2  # the shed call never took a slot
    await asyncio.gather(*busy)
    await asyncio.sleep(0.1)
    assert eng._inflight == 0
    await eng.aclose()


async def test_deadline_exceeded_raises_timeout(engine: NerEngine) -> None:
    with pytest.raises(EngineTimeout):
        await engine.analyze("sleep", "en", 0.2)


async def test_analysis_exception_maps_to_analysis_error(engine: NerEngine) -> None:
    with pytest.raises(EngineAnalysisError) as ei:
        await engine.analyze("boom", "en", 10.0)
    assert "secret-pii" not in str(ei.value)
    assert ei.value.__cause__ is None
    assert ei.value.__suppress_context__ is True


async def test_worker_backend_unavailable_maps_to_unavailable() -> None:
    eng = _engine(worker=fk.unavailable_worker)
    await eng.start()
    with pytest.raises(EngineUnavailable):
        await eng.analyze("x", "en", 10.0)
    await eng.aclose()


async def test_broken_process_pool_is_rebuilt_and_next_call_succeeds(
    engine: NerEngine,
) -> None:
    before = engine._executor
    with pytest.raises(EngineWorkerFailure):
        await engine.analyze("die", "en", 30.0)  # real worker os._exit(1)
    assert engine._executor is not before
    assert engine.ready is True
    out = await engine.analyze("hello", "en", 30.0)
    assert out == [RawEntity("PERSON", 0, 5, 0.9)]


class _Recorder:
    """Records every ProcessPoolExecutor the engine builds."""

    def __init__(self, real: type[Any]) -> None:
        self.real = real
        self.made: list[Any] = []

    def __call__(self, *a: Any, **k: Any) -> Any:
        ex = self.real(*a, **k)
        self.made.append(ex)
        return ex


@pytest.fixture
def pools(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder(ProcessPoolExecutor)
    monkeypatch.setattr(engine_mod, "ProcessPoolExecutor", rec)
    return rec


async def _until(pred: Any, limit: float = 20.0) -> None:
    end = asyncio.get_running_loop().time() + limit
    while not pred():
        assert asyncio.get_running_loop().time() < end, "condition not reached"
        await asyncio.sleep(0.05)


async def test_concurrent_victims_share_one_rebuild(pools: _Recorder) -> None:
    eng = _engine()
    await eng.start()
    results = await asyncio.gather(
        eng.analyze("die", "en", 30.0),
        eng.analyze("sleep", "en", 30.0),
        return_exceptions=True,
    )
    assert all(isinstance(r, EngineWorkerFailure) for r in results)
    assert len(pools.made) == 2  # initial pool + exactly ONE rebuild for both victims
    assert (await eng.analyze("hello", "en", 30.0))[0].entity_type == "PERSON"
    await eng.aclose()


async def test_idle_worker_death_is_recovered(pools: _Recorder) -> None:
    eng = _engine()
    await eng.start()
    first = eng._executor
    assert first is not None
    pid = next(iter(first._processes))
    os.kill(pid, signal.SIGKILL)  # dies while IDLE
    await _until(lambda: bool(getattr(first, "_broken", False)))
    with pytest.raises(EngineWorkerFailure):
        await eng.analyze("hello", "en", 30.0)  # submit() raises BrokenProcessPool
    assert eng._executor is not first and len(pools.made) == 2
    assert eng.ready is True
    assert (await eng.analyze("hello", "en", 30.0))[0].entity_type == "PERSON"
    await eng.aclose()


async def test_cancel_during_rebuild_leaves_no_orphan_pool(pools: _Recorder) -> None:
    eng = _engine(initializer=fk.init_slow)
    await eng.start()
    victim = asyncio.create_task(eng.analyze("die", "en", 30.0))
    await _until(lambda: eng._rebuild_lock.locked())
    victim.cancel()
    with pytest.raises(asyncio.CancelledError):
        await victim
    # Shielded: the rebuild finishes on its own and installs a working pool.
    await _until(lambda: eng.ready and eng._executor is pools.made[-1])
    assert (await eng.analyze("hello", "en", 30.0))[0].entity_type == "PERSON"
    await eng.aclose()
    assert all(ex._shutdown_thread for ex in pools.made)


async def test_cancelled_spawn_tears_down_new_pool(pools: _Recorder) -> None:
    eng = _engine(initializer=fk.init_slow)
    task = asyncio.create_task(eng._spawn())
    await _until(lambda: len(pools.made) == 1 and bool(pools.made[0]._processes))
    procs = list(pools.made[0]._processes.values())  # captured: cleared on shutdown
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pools.made[0]._shutdown_thread
    await _until(lambda: not any(p.is_alive() for p in procs))
    assert eng.ready is False


async def test_failed_rebuild_is_retried_on_a_later_call(
    monkeypatch: pytest.MonkeyPatch, pools: _Recorder
) -> None:
    eng = _engine(initializer=fk.init_flaky, retry_cooldown_s=60.0)
    await eng.start()
    assert eng.ready
    monkeypatch.setenv("FK_FAIL", "1")
    with pytest.raises(EngineWorkerFailure):
        await eng.analyze("die", "en", 30.0)  # rebuild fails: initializer explodes
    assert eng.ready is False
    made = len(pools.made)
    with pytest.raises(EngineUnavailable):
        await eng.analyze("hello", "en", 1.0)  # inside cooldown: no new attempt
    assert len(pools.made) == made
    monkeypatch.delenv("FK_FAIL")
    eng._last_attempt = float("-inf")  # cooldown elapsed
    assert (await eng.analyze("hello", "en", 30.0))[0].entity_type == "PERSON"
    assert eng.ready
    await eng.aclose()


async def test_malformed_worker_payload_maps_to_analysis_error(engine: NerEngine) -> None:
    with pytest.raises(EngineAnalysisError):
        await engine.analyze("bad", "en", 10.0)


async def test_slot_freed_after_timeout_and_cancel() -> None:
    eng = _engine(workers=1, max_queue_depth=1)
    await eng.start()
    with pytest.raises(EngineTimeout):
        await eng.analyze("sleep", "en", 0.2)
    assert eng._inflight == 1  # the abandoned job still occupies the pool...
    with pytest.raises(EngineOverloaded):
        await eng.analyze("hello", "en", 1.0)
    await _until(lambda: eng._inflight == 0)  # ...and frees its slot when it finishes
    task = asyncio.create_task(eng.analyze("sleep", "en", 30.0))
    await _until(lambda: eng._inflight == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _until(lambda: eng._inflight == 0)
    assert (await eng.analyze("hello", "en", 30.0))[0].entity_type == "PERSON"
    await eng.aclose()


async def test_real_strict_worker_is_picklable_and_degrades_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real ner_analyze_strict + real initializer in spawned children (picklability proof)."""
    monkeypatch.delenv("WADDLEAI_NER_ALLOW_DOWNLOAD", raising=False)
    monkeypatch.setenv("NER_SPACY_MODEL", "definitely_not_an_installed_model")
    eng = NerEngine(workers=1, spacy_model="definitely_not_an_installed_model", start_timeout_s=120)
    await eng.start()
    assert eng.ready is False and eng.backend == "none"
    await eng.aclose()


def test_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        NerEngine(workers=0)
