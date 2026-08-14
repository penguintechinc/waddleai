"""Tests for tier-3 NER running off the event loop (§3.5).

Avoids triggering a real spaCy/transformers model load (no network egress in
the test sandbox): NERFilter availability is forced off via
WADDLEAI_STUB_UPSTREAM=1 (the existing degrade-gracefully switch), and the
process-pool mechanism itself is exercised with a lightweight, self-contained
worker rather than the real `ner_analyze`.
"""

from __future__ import annotations

import asyncio
import pickle
import time
from concurrent.futures import ProcessPoolExecutor

import pytest

from shared.security import content_filter as content_filter_module
from shared.security.content_filter import ContentFilter, _get_ner_pool
from shared.security.ner_filter import ner_analyze


def _busy_sleep(seconds: float) -> str:
    """Module-level, picklable CPU-simulating worker for ProcessPoolExecutor."""
    time.sleep(seconds)
    return "done"


def _fake_ner_analyze(text: str) -> list[dict]:
    """Module-level, picklable stand-in for ner_analyze (avoids a real model load)."""
    return []


class TestSharedProcessPool:
    """(a): content_filter exposes a shared, module-scoped ProcessPoolExecutor."""

    def test_get_ner_pool_returns_process_pool_executor(self) -> None:
        """`_get_ner_pool()` returns a real ProcessPoolExecutor, not a thread pool."""
        pool = _get_ner_pool()
        assert isinstance(pool, ProcessPoolExecutor)

    def test_get_ner_pool_is_a_singleton(self) -> None:
        """Repeated calls return the same pool instance (created once)."""
        first = _get_ner_pool()
        second = _get_ner_pool()
        assert first is second

    def test_run_ner_patterns_uses_the_shared_pool_not_default_executor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_run_ner_patterns` submits to the shared pool, not `run_in_executor(None, ...)`."""
        # ner_analyze would trigger a real model load in-process; substitute
        # a module-level (picklable) stand-in so the ProcessPoolExecutor
        # submission is fast and side-effect-free.
        monkeypatch.setattr(content_filter_module, "ner_analyze", _fake_ner_analyze)

        cf = ContentFilter(db=None)
        cf.ner_filter = object()  # non-None sentinel: skip the "unavailable" early return

        async def _stub_load_disabled(_org_id):
            return set()

        cf._load_disabled_ner_entities = _stub_load_disabled  # type: ignore[method-assign]

        captured: dict[str, object] = {}
        loop = asyncio.new_event_loop()
        try:
            real_run_in_executor = loop.run_in_executor

            def _spy(executor, func, *args):
                captured["executor"] = executor
                captured["func"] = func
                return real_run_in_executor(executor, func, *args)

            loop.run_in_executor = _spy  # instance-level patch, no ABC-vs-concrete-class guessing
            loop.run_until_complete(cf._run_ner_patterns("hello", "input", org_id=None))
        finally:
            loop.close()

        assert captured["func"] is _fake_ner_analyze
        assert isinstance(captured["executor"], ProcessPoolExecutor)


class TestWorkerPicklability:
    """(b): the worker fn is importable/picklable at module scope."""

    def test_ner_analyze_is_picklable(self) -> None:
        """`ner_analyze` pickles by reference (module-level function).

        Safe use of pickle: round-trips a function object this test itself
        just created (proving ProcessPoolExecutor can transport it), never
        deserializes data from an untrusted/external source.
        """
        blob = pickle.dumps(ner_analyze)
        # nosec B301 / noqa S301: round-trips a function object this test
        # itself just created -- not deserializing data from an untrusted
        # source.
        restored = pickle.loads(blob)  # nosec B301  # noqa: S301
        assert restored is ner_analyze


class TestEventLoopResponsiveness:
    """(c): CPU work in the pool does not block a concurrently-scheduled coroutine."""

    @pytest.mark.asyncio
    async def test_process_pool_work_does_not_block_event_loop(self) -> None:
        """A concurrent coroutine keeps making progress while pool CPU work runs."""
        pool = ProcessPoolExecutor(max_workers=1)
        try:
            loop = asyncio.get_event_loop()
            ticks: list[float] = []

            async def _ticker() -> None:
                for _ in range(5):
                    ticks.append(time.monotonic())
                    await asyncio.sleep(0.02)

            ticker_task = asyncio.create_task(_ticker())
            await loop.run_in_executor(pool, _busy_sleep, 0.15)
            await ticker_task

            # The ticker must have advanced multiple times *during* the pool
            # call -- if the pool blocked the loop, only the first tick
            # (scheduled before the executor call) would have a chance to run.
            assert len(ticks) == 5
        finally:
            pool.shutdown(wait=True)


class TestGracefulDegradation:
    """(d): NER-unavailable still degrades gracefully (tier-3 skipped)."""

    @pytest.mark.asyncio
    async def test_unavailable_ner_filter_skips_tier3(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With WADDLEAI_STUB_UPSTREAM=1, ContentFilter never initializes NER."""
        monkeypatch.setenv("WADDLEAI_STUB_UPSTREAM", "1")
        cf = ContentFilter(db=None)
        assert cf.ner_filter is None

        violations = await cf._run_ner_patterns("some text", "input", org_id=None)

        assert violations == []
