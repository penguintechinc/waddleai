"""ProxyPipeline releases a held reservation when a LATER stage blocks or raises."""

from unittest.mock import AsyncMock

import pytest

from proxy.apps.proxy_server.pipeline.stages import PipelineContext, ProxyPipeline, Stage


class Holds(Stage):
    """Pretends to be the QuotaStage: installs a release closure."""

    def __init__(self, release):
        """Hold the release callable to install on the context."""
        super().__init__("quota", None)
        self.release = release

    async def __call__(self, ctx):
        """Install the release closure."""
        ctx.quota_release = self.release
        return ctx


class Blocks(Stage):
    """A later stage that blocks the request (e.g. security_out)."""

    async def __call__(self, ctx):
        """Block the request."""
        ctx.blocked, ctx.status_code, ctx.block_reason = True, 400, "blocked"
        return ctx


class Raises(Stage):
    """A later stage that raises."""

    async def __call__(self, ctx):
        """Raise."""
        raise RuntimeError("provider exploded")


class Passes(Stage):
    """A later stage that succeeds."""

    async def __call__(self, ctx):
        """Pass through."""
        return ctx


def _ctx():
    """A bare context."""
    return PipelineContext(user=None, body={})


async def test_block_after_reserve_releases_exactly_once_and_clears_the_hook():
    """security_out-style block: the reservation is refunded once and the hook cleared."""
    release = AsyncMock()
    ctx = await ProxyPipeline([Holds(release), Blocks("b", None), Passes("p", None)], None).run(
        _ctx()
    )
    release.assert_awaited_once()
    assert ctx.quota_release is None and ctx.quota_reservation is None


async def test_exception_after_reserve_releases_and_still_raises():
    """A crashing stage refunds and the error propagates unchanged."""
    release = AsyncMock()
    with pytest.raises(RuntimeError, match="provider exploded"):
        await ProxyPipeline([Holds(release), Raises("r", None)], None).run(_ctx())
    release.assert_awaited_once()


async def test_successful_pipeline_does_not_release():
    """Normal completion leaves the reservation for _finalize to reconcile."""
    release = AsyncMock()
    ctx = await ProxyPipeline([Holds(release), Passes("p", None)], None).run(_ctx())
    release.assert_not_awaited()
    assert ctx.quota_release is release


async def test_failing_release_is_logged_not_raised(caplog):
    """A refund that itself fails must not mask the block."""
    release = AsyncMock(side_effect=ConnectionError("down"))
    with caplog.at_level("ERROR"):
        ctx = await ProxyPipeline([Holds(release), Blocks("b", None)], None).run(_ctx())
    assert ctx.blocked is True and "quota release failed" in caplog.text
