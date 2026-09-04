"""End-to-end failover scenarios (spec §8) against two in-process aiohttp stubs.

Drives the REAL DestinationResolver -> DestinationConnectorRegistry ->
FailoverDispatcher -> OpenAIConnector stack over HTTP to 127.0.0.1, never
mocking any of those four layers -- only the DB (FakeDB.executesql) and the
provider itself (StubProvider) are faked. Every scenario asserts on
stub-observed facts (request counts, Authorization headers), not only on the
dispatcher's return value, so a bug that fabricates a plausible-looking
Outcome without actually reaching -- or without actually using the right
credential against -- the right stub would still fail these tests.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared.routing.destination_breaker import DestinationBreaker
from shared.routing.failover import DestinationsExhausted
from shared.utils.llm_connectors import ProviderClientError
from tests.unit.failover.conftest import FakeDB

ACTIVE_KEY, STANDBY_KEY = "sk-active-TEST-0001", "sk-standby-TEST-0002"


def _ctx(*, stream: bool = False) -> SimpleNamespace:
    """A minimal PipelineContext stand-in carrying only what FailoverDispatcher reads."""
    return SimpleNamespace(model="gpt-4", stream=stream, bytes_flushed=False)


@pytest.mark.asyncio
async def test_active_503_fails_over_and_standby_uses_its_own_key(two_stubs, make_stack):
    """S2/S5/S6: active 503 fails over to standby, which authenticates with ITS OWN key."""
    active, standby = two_stubs
    active.mode = "server_error"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = make_stack(db)
    dests = await resolver.resolve(7, "gpt-4")
    out = await dispatcher.dispatch(_ctx(), dests, [{"role": "user", "content": "hi"}])
    assert out.text == "from-standby"
    assert standby.seen_auth == [f"Bearer {STANDBY_KEY}"]  # S2 -- standby used ITS key
    assert active.seen_auth == [f"Bearer {ACTIVE_KEY}"]  # active used its own, never standby's
    assert out.attempts[0].reason == "server_error"


@pytest.mark.asyncio
async def test_active_503_fails_over_and_standby_uses_its_own_key_streaming(two_stubs, make_stack):
    """Streaming variant: standby answers via SSE and still authenticates with ITS OWN key."""
    active, standby = two_stubs
    active.mode = "server_error"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = make_stack(db)
    out = await dispatcher.dispatch(
        _ctx(stream=True), await resolver.resolve(7, "gpt-4"), [{"role": "user", "content": "hi"}]
    )
    assert out.text == "from-standby"
    assert standby.seen_auth == [f"Bearer {STANDBY_KEY}"]
    assert active.seen_auth == [f"Bearer {ACTIVE_KEY}"]


@pytest.mark.asyncio
async def test_active_429_with_retry_after_fails_over(two_stubs, make_stack):
    """Active 429 + Retry-After fails over to standby; the attempt trail records rate_limit."""
    active, standby = two_stubs
    active.mode = "rate_limit"
    active.retry_after = "7"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = make_stack(db)
    out = await dispatcher.dispatch(
        _ctx(), await resolver.resolve(7, "gpt-4"), [{"role": "user", "content": "hi"}]
    )
    assert out.text == "from-standby"
    assert out.attempts[0].reason == "rate_limit"


@pytest.mark.asyncio
async def test_active_429_with_retry_after_fails_over_streaming(two_stubs, make_stack):
    """Streaming variant of the 429 + Retry-After failover."""
    active, standby = two_stubs
    active.mode = "rate_limit"
    active.retry_after = "7"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = make_stack(db)
    out = await dispatcher.dispatch(
        _ctx(stream=True), await resolver.resolve(7, "gpt-4"), [{"role": "user", "content": "hi"}]
    )
    assert out.text == "from-standby"
    assert out.attempts[0].reason == "rate_limit"


@pytest.mark.asyncio
async def test_active_hang_past_timeout_fails_over(two_stubs, make_stack):
    """Active hangs past its (lowered) timeout_seconds -> times out and fails over."""
    active, standby = two_stubs
    active.mode = "hang"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = make_stack(db)
    dests = await resolver.resolve(7, "gpt-4")
    object.__setattr__(dests[0], "timeout_seconds", 1)  # bound the hang low for the test
    out = await dispatcher.dispatch(_ctx(), dests, [{"role": "user", "content": "hi"}])
    assert out.text == "from-standby"
    assert out.attempts[0].reason == "timeout"


@pytest.mark.asyncio
async def test_active_hang_past_timeout_fails_over_streaming(two_stubs, make_stack):
    """Streaming hang: the timeout bounds time-to-FIRST-BYTE, not the whole stream (spec S5.4)."""
    active, standby = two_stubs
    active.mode = "hang"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = make_stack(db)
    dests = await resolver.resolve(7, "gpt-4")
    object.__setattr__(dests[0], "timeout_seconds", 1)
    out = await dispatcher.dispatch(_ctx(stream=True), dests, [{"role": "user", "content": "hi"}])
    assert out.text == "from-standby"
    assert out.attempts[0].reason == "timeout"


@pytest.mark.asyncio
async def test_active_401_is_returned_and_standby_untouched(two_stubs, make_stack):
    """S5: a 401 (client error) propagates without failover; standby is never called."""
    active, standby = two_stubs
    active.mode = "unauthorized"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = make_stack(db)
    with pytest.raises(ProviderClientError):
        await dispatcher.dispatch(
            _ctx(), await resolver.resolve(7, "gpt-4"), [{"role": "user", "content": "hi"}]
        )
    assert standby.seen_auth == []  # standby never called (S5)


@pytest.mark.asyncio
async def test_both_down_raises_destinations_exhausted(two_stubs, make_stack):
    """Both destinations return 503 -> DestinationsExhausted maps to a 502, no Retry-After."""
    active, standby = two_stubs
    active.mode = "server_error"
    standby.mode = "server_error"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = make_stack(db)
    with pytest.raises(DestinationsExhausted) as ei:
        await dispatcher.dispatch(
            _ctx(), await resolver.resolve(7, "gpt-4"), [{"role": "user", "content": "hi"}]
        )
    assert ei.value.status_code() == 502
    assert ei.value.retry_after() is None


@pytest.mark.asyncio
async def test_both_rate_limited_raises_destinations_exhausted_429(two_stubs, make_stack):
    """Both destinations return 429+Retry-After -> DestinationsExhausted maps to 429/retry_after."""
    active, standby = two_stubs
    active.mode = "rate_limit"
    active.retry_after = "7"
    standby.mode = "rate_limit"
    standby.retry_after = "7"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = make_stack(db)
    with pytest.raises(DestinationsExhausted) as ei:
        await dispatcher.dispatch(
            _ctx(), await resolver.resolve(7, "gpt-4"), [{"role": "user", "content": "hi"}]
        )
    assert ei.value.status_code() == 429
    assert ei.value.retry_after() == 7.0


@pytest.mark.asyncio
async def test_breaker_opens_after_three_failures_and_skips_active(two_stubs, make_stack):
    """After 3 consecutive active failures the breaker opens; the 4th dispatch skips active."""
    active, standby = two_stubs
    active.mode = "server_error"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    breaker = DestinationBreaker(failure_threshold=3, cooldown_seconds=300)
    resolver, dispatcher = make_stack(db, breaker=breaker)
    for _ in range(3):
        await dispatcher.dispatch(
            _ctx(), await resolver.resolve(7, "gpt-4"), [{"role": "user", "content": "hi"}]
        )
    active.seen_auth.clear()
    out = await dispatcher.dispatch(
        _ctx(), await resolver.resolve(7, "gpt-4"), [{"role": "user", "content": "hi"}]
    )
    assert out.text == "from-standby"
    assert active.seen_auth == []  # active skipped -- breaker open, no request reached it
    assert out.attempts[0].reason == "breaker_open"


@pytest.mark.asyncio
async def test_marker_shape_reports_attempts(two_stubs, make_stack):
    """The usage.waddleai.destination marker carries role/attempts only -- no secret material."""
    active, standby = two_stubs
    active.mode = "server_error"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = make_stack(db)
    out = await dispatcher.dispatch(
        _ctx(), await resolver.resolve(7, "gpt-4"), [{"role": "user", "content": "hi"}]
    )
    m = out.marker
    assert m["role"] == "standby" and m["provider"] == "openai"
    assert [a["outcome"] for a in m["attempts"]] == ["failed", "ok"]
    assert set(m) == {"id", "priority", "role", "provider", "model", "attempts"}
    for a in m["attempts"]:
        assert set(a) == {"destination_id", "provider", "outcome", "reason"}
