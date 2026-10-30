"""Warm NER process pool with bounded-queue load shedding and crash recovery.

Wraps the strict NER worker in a ``ProcessPoolExecutor`` so the event loop never
blocks on CPU-bound analysis. Only spans (type/offsets/score) leave the workers'
mapping; matched text is never carried, logged, or placed in an exception.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import multiprocessing
import threading
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from multiprocessing.context import BaseContext
from typing import Any

from services.classification.app.obs import get_logger
from shared.security.ner_filter import NERBackendUnavailable, ner_analyze_strict

log = get_logger(__name__)

_WARMUP_TEXT = "warm up"
_START_TIMEOUT_S = 300.0
_RETRY_COOLDOWN_S = 5.0


class EngineError(Exception):
    """Base class for engine failures; messages never carry request text."""


class EngineUnavailable(EngineError):  # noqa: N818 - names fixed by the T4 interface contract
    """The engine has no usable NER backend (not started, degraded, or rebuilding)."""


class EngineOverloaded(EngineError):  # noqa: N818
    """Concurrent work exceeded MAX_QUEUE_DEPTH; the request was load-shed."""


class EngineTimeout(EngineError):  # noqa: N818
    """The analysis did not finish within the caller's deadline."""


class EngineWorkerFailure(EngineError):  # noqa: N818
    """A worker process died mid-call; the pool has been (or is being) rebuilt."""


class EngineAnalysisError(EngineError):  # noqa: N818
    """The worker raised while analysing; only the exception type is recorded."""


@dataclass(slots=True)
class RawEntity:
    """A detected span: type, offsets and score. Deliberately has no text field."""

    entity_type: str
    start: int
    end: int
    score: float


@dataclass(slots=True, frozen=True)
class WorkerInfo:
    """What a warmed worker reports about its backend (used for the fingerprint)."""

    backend: str
    model_name: str
    model_version: str
    presidio_version: str


_barrier: Any = None
_info: WorkerInfo = WorkerInfo("none", "", "", "")


def _dist_version(*names: str) -> str:
    """Return the installed version of the first resolvable distribution, else ''."""
    for name in names:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return ""


def _worker_init(spacy_model: str, barrier: Any) -> None:
    """Build the strict-path NERFilter in this worker and run a 1-token warm-up.

    Seeds the filter ``ner_analyze_strict`` reuses, so the first real request is
    not a cold model load. A missing backend is recorded, never downloaded.
    """
    global _barrier, _info
    from shared.security import ner_filter

    _barrier = barrier
    flt = ner_filter.NERFilter(spacy_model=spacy_model)
    ner_filter._strict_worker_filter = flt
    if flt.available:
        flt.analyze_or_raise(_WARMUP_TEXT, "en")
    _info = WorkerInfo(
        backend=flt.mode,
        model_name=spacy_model if flt.mode == "presidio" else "",
        model_version=(
            _dist_version(spacy_model, spacy_model.replace("_", "-"))
            if flt.mode == "presidio"
            else ""
        ),
        presidio_version=_dist_version("presidio-analyzer") if flt.mode == "presidio" else "",
    )


def _worker_probe(timeout_s: float) -> WorkerInfo:
    """Rendezvous with every other worker, proving each one finished initialising."""
    if _barrier is not None:
        _barrier.wait(timeout_s)
    return _info


class NerEngine:
    """Async facade over a warm, crash-recoverable NER process pool."""

    def __init__(
        self,
        *,
        workers: int = 2,
        max_queue_depth: int = 64,
        spacy_model: str = "en_core_web_lg",
        worker: Callable[[str, str], list[dict[str, Any]]] = ner_analyze_strict,
        initializer: Callable[[str, Any], None] = _worker_init,
        probe: Callable[[float], WorkerInfo] = _worker_probe,
        mp_context: BaseContext | None = None,
        start_timeout_s: float = _START_TIMEOUT_S,
        retry_cooldown_s: float = _RETRY_COOLDOWN_S,
    ) -> None:
        """Configure the engine; no process is spawned until ``start()``."""
        if workers < 1 or max_queue_depth < 1:
            raise ValueError("workers and max_queue_depth must be >= 1")
        self._workers = workers
        self._max_depth = max_queue_depth
        self._model = spacy_model
        self._worker = worker
        self._initializer = initializer
        self._probe = probe
        self._ctx = mp_context or multiprocessing.get_context("spawn")
        self._start_timeout = start_timeout_s
        self._retry_cooldown = retry_cooldown_s
        self._recovery_pending = False
        self._last_attempt = float("-inf")
        self._tasks: set[asyncio.Task[Any]] = set()
        self._executor: ProcessPoolExecutor | None = None
        self._info = WorkerInfo("none", "", "", "")
        self._ready = False
        self._inflight = 0
        self._lock = threading.Lock()
        self._rebuild_lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        """True only once every worker has warmed a usable backend."""
        return self._ready

    @property
    def queue_depth(self) -> int:
        """In-flight NER jobs occupying the pool (feeds the queue-depth gauge)."""
        return self._inflight

    @property
    def backend(self) -> str:
        """Active NER backend reported by the workers ('none' when unavailable)."""
        return self._info.backend

    @property
    def fingerprint(self) -> str:
        """Backend + spaCy model name/version + presidio version, for cache keys."""
        i = self._info
        return f"{i.backend}|{i.model_name}|{i.model_version}|presidio={i.presidio_version}"

    async def start(self) -> None:
        """Spawn and warm the pool; ``ready`` flips only after all workers are warm.

        A missing model leaves the engine not-ready (no download, no raise).
        """
        async with self._rebuild_lock:
            await self._spawn()

    @staticmethod
    def _discard(executor: ProcessPoolExecutor) -> None:
        """Shut a pool down and terminate its workers (they may be parked on the barrier)."""
        procs = list((getattr(executor, "_processes", None) or {}).values())
        executor.shutdown(wait=False, cancel_futures=True)
        for proc in procs:
            with contextlib.suppress(Exception):
                proc.terminate()

    async def _spawn(self) -> bool:
        """Create a fresh pool and warm every worker behind a rendezvous barrier.

        Returns readiness. On failure OR cancellation the half-built pool and its
        workers are torn down before returning/propagating, so none is orphaned.
        """
        self._ready = False
        barrier = self._ctx.Barrier(self._workers)
        executor = ProcessPoolExecutor(
            max_workers=self._workers,
            mp_context=self._ctx,
            initializer=self._initializer,
            initargs=(self._model, barrier),
        )
        loop = asyncio.get_running_loop()
        try:
            futs = [
                loop.run_in_executor(executor, self._probe, self._start_timeout)
                for _ in range(self._workers)
            ]
            infos = await asyncio.wait_for(asyncio.gather(*futs), timeout=self._start_timeout + 5)
        except BaseException as exc:
            self._discard(executor)
            if not isinstance(exc, Exception):
                raise
            log.error("ner_engine_start_failed", error_type=type(exc).__name__)
            old, self._executor = self._executor, None
            if old is not None:
                self._discard(old)
            self._info = WorkerInfo("none", "", "", "")
            return False
        old, self._executor = self._executor, executor
        if old is not None:
            self._discard(old)
        self._info = infos[0]
        self._ready = all(i.backend != "none" for i in infos) and len(set(infos)) == 1
        log.info("ner_engine_started", backend=self._info.backend, ready=self._ready)
        return self._ready

    def _shielded(self, coro: Coroutine[Any, Any, None]) -> Awaitable[None]:
        """Run ``coro`` as a tracked task a caller's cancellation cannot abandon."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return asyncio.shield(task)

    async def analyze(self, text: str, language: str, deadline_s: float) -> list[RawEntity]:
        """Analyse ``text`` off-loop; load-shed when full, honour ``deadline_s``."""
        if self._rebuild_lock.locked():
            async with self._rebuild_lock:
                pass
        if (
            not self._ready
            and self._recovery_pending
            and asyncio.get_running_loop().time() - self._last_attempt >= self._retry_cooldown
        ):
            await self._shielded(self._rebuild(None))
        executor = self._executor
        if not self._ready or executor is None:
            raise EngineUnavailable("NER engine is not ready")
        with self._lock:
            if self._inflight >= self._max_depth:
                raise EngineOverloaded("NER engine queue is full")
            self._inflight += 1
        try:
            cf: Future[list[dict[str, Any]]] = executor.submit(self._worker, text, language)
        except BrokenProcessPool:
            # A worker died while idle (e.g. OOM-kill): same recovery as an in-flight break.
            self._release()
            await self._shielded(self._rebuild(executor))
            raise EngineWorkerFailure("NER worker process died") from None
        except BaseException:
            self._release()
            raise
        # Slot is freed when the worker finishes, not when the caller gives up:
        # a timed-out job still occupies the pool, so it must still count.
        cf.add_done_callback(lambda _f: self._release())
        try:
            raw = await asyncio.wait_for(asyncio.wrap_future(cf), timeout=deadline_s)
        except TimeoutError:
            raise EngineTimeout("NER analysis exceeded its deadline") from None
        except BrokenProcessPool:
            await self._shielded(self._rebuild(executor))
            raise EngineWorkerFailure("NER worker process died") from None
        except NERBackendUnavailable:
            raise EngineUnavailable("NER backend unavailable in worker") from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("ner_engine_analysis_error", error_type=type(exc).__name__)
            raise EngineAnalysisError(f"NER analysis failed ({type(exc).__name__})") from None
        try:
            return [
                RawEntity(
                    entity_type=str(e["entity_type"]),
                    start=int(e["start"]),
                    end=int(e["end"]),
                    score=float(e["score"]),
                )
                for e in raw
            ]
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("ner_engine_bad_payload", error_type=type(exc).__name__)
            raise EngineAnalysisError("NER worker returned a malformed payload") from None

    def _release(self) -> None:
        """Free one concurrency slot (thread-safe; called from pool callback threads)."""
        with self._lock:
            self._inflight -= 1

    async def _rebuild(self, broken: ProcessPoolExecutor | None) -> None:
        """Rebuild the pool once per breakage (victims share it); None = retry a failed rebuild."""
        async with self._rebuild_lock:
            if broken is not None and self._executor is not broken:
                return
            loop = asyncio.get_running_loop()
            if broken is None and (
                not self._recovery_pending
                or loop.time() - self._last_attempt < self._retry_cooldown
            ):
                return  # queued callers skip a respawn if one just ended (cooldown)
            log.error("ner_engine_pool_rebuilding")
            self._recovery_pending = True  # stays set if the rebuild fails or is cancelled
            try:
                self._recovery_pending = not await self._spawn()
            finally:
                self._last_attempt = loop.time()  # stamped at attempt END

    async def aclose(self) -> None:
        """Shut the pool down and mark the engine not-ready."""
        self._ready = False
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        executor, self._executor = self._executor, None
        if executor is not None:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: executor.shutdown(wait=True, cancel_futures=True)
            )


__all__ = [
    "EngineAnalysisError",
    "EngineError",
    "EngineOverloaded",
    "EngineTimeout",
    "EngineUnavailable",
    "EngineWorkerFailure",
    "NerEngine",
    "RawEntity",
    "WorkerInfo",
]
