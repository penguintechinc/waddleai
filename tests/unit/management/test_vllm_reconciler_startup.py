"""C3: startup wiring of the vLLM reconciler."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import create_engine

from services.management.app import build_vllm_reconciler
from services.management.app.services.model_store import PullPoller
from services.management.app.services.vllm_reconciler import PgAdvisoryLeaderGuard
from shared.routing.prefer_local import create_prefer_local_rule
from tests.unit.management.test_vllm_reconciler import _reconciler


def _app() -> SimpleNamespace:
    """A stand-in with the logger the builder uses."""
    return SimpleNamespace(logger=logging.getLogger("startup-test"))


def test_disabled_by_env(monkeypatch) -> None:
    """WADDLEAI_VLLM_RECONCILER=0 builds nothing."""
    monkeypatch.setenv("WADDLEAI_VLLM_RECONCILER", "0")
    assert build_vllm_reconciler(_app()) is None


def test_no_engine_builds_nothing_and_warns(monkeypatch, caplog) -> None:
    """Without a DB engine (no leader lock possible) nothing starts, loudly."""
    monkeypatch.delenv("WADDLEAI_VLLM_RECONCILER", raising=False)
    monkeypatch.setattr("services.management.app.extensions.db", SimpleNamespace(engine=None))
    with caplog.at_level(logging.WARNING, logger="startup-test"):
        assert build_vllm_reconciler(_app()) is None
    assert "not started" in caplog.text


def test_built_with_leader_guard_and_pull_poller(monkeypatch, tmp_path) -> None:
    """With an engine: a PG-advisory guard and the pull poller are wired."""
    monkeypatch.delenv("WADDLEAI_VLLM_RECONCILER", raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    monkeypatch.setattr(
        "services.management.app.extensions.db", SimpleNamespace(engine=engine, marker=MagicMock())
    )
    reconciler = build_vllm_reconciler(_app())
    assert reconciler is not None
    assert isinstance(reconciler.guard, PgAdvisoryLeaderGuard)
    assert isinstance(reconciler.pull_poller, PullPoller)
    assert reconciler.prefer_local_fn is create_prefer_local_rule


def test_run_forever_is_cancellable_as_a_task(vllm_db, both_flags) -> None:
    """The loop started as a task cancels cleanly (the after_serving path)."""
    rec, _, _ = _reconciler(vllm_db)

    async def go() -> None:
        task = asyncio.ensure_future(rec.run_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.done()

    asyncio.run(go())
