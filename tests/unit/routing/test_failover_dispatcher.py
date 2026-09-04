"""Tests for FailoverDispatcher (spec S5.3/S5.4).

Covers retryable-only failover, breaker gating, the first-byte rule, bounded
attempts, and the security properties of ``Outcome.marker`` (no endpoint,
credential, or other material ever leaves this module).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from shared.routing.destination_breaker import DestinationBreaker
from shared.routing.destination_connectors import OwnershipError
from shared.routing.destinations import Destination
from shared.routing.failover import DestinationsExhausted, FailoverDispatcher
from shared.utils.llm_connectors import (
    ProviderClientError,
    ProviderRateLimitError,
    ProviderServerError,
)


def _dest(i, provider="openai", model_id=None):
    """Build a Destination with id/priority both set to `i` (priority 0 == active)."""
    return Destination(
        id=i,
        organization_id=7,
        model="m",
        priority=i,
        provider_id=i,
        provider_type=provider,
        endpoint_url=None,
        region=None,
        provider_model_id=model_id,
        timeout_seconds=30,
        credential_id=i,
        owner_org_id=7,
        credential_version="v1",
    )


class _Connector:
    """Fake LLMConnector: returns fixed text, raises a fixed error, or hangs forever."""

    def __init__(self, *, text=None, exc=None, hang=False):
        """Configure this fake to succeed with `text`, raise `exc`, or hang if `hang`."""
        self._text, self._exc, self._hang = text, exc, hang

    async def chat_completion(self, messages, model, **kw):
        """Non-streaming fake call: hang, raise, or return the configured text + usage."""
        if self._hang:
            await asyncio.sleep(10)
        if self._exc:
            raise self._exc
        return self._text, {"input_tokens": 1, "output_tokens": 2, "finish_reason": "stop"}

    async def stream_chat_completion(self, messages, model, **kw):
        """Streaming fake call: hang, raise, or yield one done=True chunk."""
        if self._hang:
            await asyncio.sleep(10)
        if self._exc:
            raise self._exc
        yield SimpleNamespace(delta=self._text or "", usage={"finish_reason": "stop"}, done=True)


class _Registry:
    """Fake DestinationConnectorRegistry backed by a fixed id -> connector mapping."""

    def __init__(self, mapping):
        """Bind the fixed destination-id -> connector mapping this fake serves."""
        self._m = mapping

    async def get(self, dest):
        """Return the connector registered for `dest.id`."""
        return self._m[dest.id]


def _ctx(stream=False, bytes_flushed=False):
    """Build a minimal dispatch context (model/stream/bytes_flushed only)."""
    return SimpleNamespace(model="m", stream=stream, bytes_flushed=bytes_flushed)


class _RecordingStream:
    """Async iterator over fixed chunks; records whether the caller closed it."""

    def __init__(self, chunks, owner):
        """Bind the fixed chunk sequence and the owning connector to flag on close."""
        self._iter = iter(chunks)
        self._owner = owner

    def __aiter__(self):
        """Return self -- this object is its own async iterator."""
        return self

    async def __anext__(self):
        """Yield the next fixed chunk, or stop the iteration once exhausted."""
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self):
        """Flag on the owning connector that this stream was explicitly closed."""
        self._owner.closed = True


class _MultiChunkStreamConnector:
    """Fake connector whose stream yields several fixed chunks, tracking close()."""

    def __init__(self, chunks):
        """Bind the fixed chunk sequence this connector's stream will yield."""
        self._chunks = chunks
        self.closed = False

    def stream_chat_completion(self, messages, model, **kw):
        """Return a fresh _RecordingStream over the fixed chunks."""
        return _RecordingStream(self._chunks, self)


class _SpyBreaker(DestinationBreaker):
    """DestinationBreaker subclass that records every record_success/record_failure call."""

    def __init__(self, *args, **kwargs):
        """Initialise the wrapped breaker plus empty success/failure call logs."""
        super().__init__(*args, **kwargs)
        self.success_calls: list[int] = []
        self.failure_calls: list[int] = []

    def record_success(self, dest_id):
        """Log the call, then delegate to the real breaker."""
        self.success_calls.append(dest_id)
        super().record_success(dest_id)

    def record_failure(self, dest_id):
        """Log the call, then delegate to the real breaker."""
        self.failure_calls.append(dest_id)
        super().record_failure(dest_id)


class _FakeMetrics:
    """Records every call so tests can assert on outcome/failover/gauge sequences."""

    def __init__(self):
        """Start with empty attempt, failover, and gauge logs."""
        self.attempts: list[tuple[str, str]] = []
        self.failovers: list[tuple[str, str, str]] = []
        self.gauge: list[tuple[str, bool]] = []

    def record_destination_attempt(self, provider_type, outcome):
        """Log one (provider_type, outcome) attempt."""
        self.attempts.append((provider_type, outcome))

    def record_destination_failover(self, from_provider, to_provider, reason):
        """Log one (from_provider, to_provider, reason) failover hop."""
        self.failovers.append((from_provider, to_provider, reason))

    def set_destination_breaker_open(self, destination_id, is_open):
        """Log one (destination_id, is_open) breaker gauge update."""
        self.gauge.append((destination_id, is_open))


# --- brief Step 1 tests (verbatim behaviour, docstrings added for lint) ----


@pytest.mark.asyncio
async def test_active_success_returns_first():
    """A single healthy destination serves the request on the first attempt."""
    reg = _Registry({1: _Connector(text="hi")})
    out = await FailoverDispatcher(reg, DestinationBreaker()).dispatch(_ctx(), [_dest(1)], ["m"])
    assert out.text == "hi" and out.destination.id == 1
    assert out.attempts[-1].outcome == "ok"


@pytest.mark.asyncio
async def test_retryable_fails_over_to_standby():
    """A 5xx on the active destination fails over to the standby, which serves it."""
    reg = _Registry(
        {
            1: _Connector(exc=ProviderServerError("openai", "m", "503", status_code=503)),
            2: _Connector(text="from-standby"),
        }
    )
    out = await FailoverDispatcher(reg, DestinationBreaker()).dispatch(
        _ctx(), [_dest(1), _dest(2)], ["m"]
    )
    assert out.text == "from-standby" and out.destination.id == 2
    assert out.attempts[0].outcome == "failed" and out.attempts[0].reason == "server_error"


@pytest.mark.asyncio
async def test_client_error_never_fails_over_and_is_raised():
    """A 4xx propagates immediately; the standby's connector is never reached."""
    reg = _Registry(
        {
            1: _Connector(exc=ProviderClientError("openai", "m", "401", status_code=401)),
            2: _Connector(text="should-not-run"),
        }
    )
    with pytest.raises(ProviderClientError):
        await FailoverDispatcher(reg, DestinationBreaker()).dispatch(
            _ctx(), [_dest(1), _dest(2)], ["m"]
        )


@pytest.mark.asyncio
async def test_client_error_does_not_trip_breaker():
    """A 4xx must never count as a breaker failure for its destination."""
    breaker = DestinationBreaker()
    reg = _Registry({1: _Connector(exc=ProviderClientError("openai", "m", "400", status_code=400))})
    with pytest.raises(ProviderClientError):
        await FailoverDispatcher(reg, breaker).dispatch(_ctx(), [_dest(1)], ["m"])
    assert breaker.is_open(1) is False
    snap = breaker.snapshot()
    assert "dest:1" not in snap or snap["dest:1"]["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_all_retryable_raises_destinations_exhausted_with_last_status():
    """Every destination failing retryably raises DestinationsExhausted mapped from the last."""
    reg = _Registry(
        {
            1: _Connector(exc=ProviderServerError("openai", "m", "503", status_code=503)),
            2: _Connector(exc=ProviderRateLimitError("openai", "m", "429", status_code=429)),
        }
    )
    with pytest.raises(DestinationsExhausted) as ei:
        await FailoverDispatcher(reg, DestinationBreaker()).dispatch(
            _ctx(), [_dest(1), _dest(2)], ["m"]
        )
    assert ei.value.status_code() == 429  # last retryable was a 429


@pytest.mark.asyncio
async def test_destinations_exhausted_retry_after_from_rate_limit_error():
    """retry_after() surfaces the LAST error's Retry-After, unit-preserved."""
    reg = _Registry(
        {
            1: _Connector(exc=ProviderServerError("openai", "m", "503", status_code=503)),
            2: _Connector(
                exc=ProviderRateLimitError("openai", "m", "429", status_code=429, retry_after=12.5)
            ),
        }
    )
    with pytest.raises(DestinationsExhausted) as ei:
        await FailoverDispatcher(reg, DestinationBreaker()).dispatch(
            _ctx(), [_dest(1), _dest(2)], ["m"]
        )
    assert ei.value.retry_after() == 12.5


@pytest.mark.asyncio
async def test_destinations_exhausted_retry_after_none_for_non_rate_limit():
    """retry_after() is None when the last error wasn't a rate limit."""
    reg = _Registry({1: _Connector(exc=ProviderServerError("openai", "m", "503", status_code=503))})
    with pytest.raises(DestinationsExhausted) as ei:
        await FailoverDispatcher(reg, DestinationBreaker()).dispatch(_ctx(), [_dest(1)], ["m"])
    assert ei.value.retry_after() is None


@pytest.mark.asyncio
async def test_breaker_open_destination_is_skipped():
    """A tripped, still-cooling-down destination is skipped without an attempt."""
    breaker = DestinationBreaker(failure_threshold=1, cooldown_seconds=300)
    breaker.record_failure(1)  # trip active
    reg = _Registry({1: _Connector(text="nope"), 2: _Connector(text="served")})
    out = await FailoverDispatcher(reg, breaker).dispatch(_ctx(), [_dest(1), _dest(2)], ["m"])
    assert out.destination.id == 2
    assert out.attempts[0].outcome == "skipped" and out.attempts[0].reason == "breaker_open"


@pytest.mark.asyncio
async def test_timeout_is_classified_and_fails_over():
    """A non-streaming attempt that hangs past timeout_seconds is classified `timeout`."""
    reg = _Registry({1: _Connector(hang=True), 2: _Connector(text="served")})
    d1 = _dest(1)
    object.__setattr__(d1, "timeout_seconds", 1)  # bound the hang low for the test
    out = await FailoverDispatcher(reg, DestinationBreaker()).dispatch(
        _ctx(), [d1, _dest(2)], ["m"]
    )
    assert out.destination.id == 2
    assert out.attempts[0].reason == "timeout"


@pytest.mark.asyncio
async def test_first_byte_rule_reraises_without_failover():
    """Once ctx.bytes_flushed is True, a retryable failure re-raises instead of failing over."""
    reg = _Registry(
        {
            1: _Connector(exc=ProviderServerError("openai", "m", "503", status_code=503)),
            2: _Connector(text="standby"),
        }
    )
    with pytest.raises(ProviderServerError):
        await FailoverDispatcher(reg, DestinationBreaker()).dispatch(
            _ctx(bytes_flushed=True), [_dest(1), _dest(2)], ["m"]
        )


@pytest.mark.asyncio
async def test_marker_shape_is_secret_free():
    """Outcome.marker carries only ids/roles/attempts -- never an endpoint or credential id."""
    # priority=0 (id 0) so role=="active" per Destination.role -- _dest(i, ...) ties
    # priority to the id parameter, and role is only "active" at priority 0.
    reg = _Registry({0: _Connector(text="hi")})
    out = await FailoverDispatcher(reg, DestinationBreaker()).dispatch(
        _ctx(), [_dest(0, model_id="x")], ["m"]
    )
    marker = out.marker
    assert marker["role"] == "active" and marker["provider"] == "openai"
    assert "attempts" in marker and marker["attempts"][0]["outcome"] == "ok"
    assert "endpoint" not in marker and "credential_id" not in marker


# --- additional self-review coverage ----------------------------------------


@pytest.mark.asyncio
async def test_client_error_stops_before_next_destination():
    """4xx must never even reach the next destination's connector."""

    class _CountingConnector(_Connector):
        """Wraps _Connector to count chat_completion invocations."""

        def __init__(self, **kw):
            """Start the call counter at zero."""
            super().__init__(**kw)
            self.calls = 0

        async def chat_completion(self, messages, model, **kw):
            """Increment the call counter, then delegate to the fake behaviour."""
            self.calls += 1
            return await super().chat_completion(messages, model, **kw)

    c2 = _CountingConnector(text="should-not-run")
    reg = _Registry(
        {1: _Connector(exc=ProviderClientError("openai", "m", "401", status_code=401)), 2: c2}
    )
    with pytest.raises(ProviderClientError):
        await FailoverDispatcher(reg, DestinationBreaker()).dispatch(
            _ctx(), [_dest(1), _dest(2)], ["m"]
        )
    assert c2.calls == 0


@pytest.mark.asyncio
async def test_registry_value_error_skips_as_config_defect():
    """A decrypt-failure ValueError from the registry skips the destination, breaker untouched."""

    class _BadRegistry:
        """Raises ValueError for destination 1 (simulating a decrypt failure)."""

        async def get(self, dest):
            """Raise for destination 1; otherwise serve a working connector."""
            if dest.id == 1:
                raise ValueError("decrypt failed")
            return _Connector(text="served")

    breaker = DestinationBreaker()
    out = await FailoverDispatcher(_BadRegistry(), breaker).dispatch(
        _ctx(), [_dest(1), _dest(2)], ["m"]
    )
    assert out.destination.id == 2
    assert out.attempts[0].outcome == "skipped" and out.attempts[0].reason == "config_defect"
    assert breaker.is_open(1) is False


@pytest.mark.asyncio
async def test_registry_ownership_error_skips_as_config_defect():
    """OwnershipError from the registry (S2 re-assertion) skips the destination, never trips it."""

    class _OwnershipFailRegistry:
        """Raises OwnershipError for destination 1 (simulating an S2 mismatch)."""

        async def get(self, dest):
            """Raise for destination 1; otherwise serve a working connector."""
            if dest.id == 1:
                raise OwnershipError("credential 9 owner 5 != destination org 7")
            return _Connector(text="served")

    breaker = DestinationBreaker()
    out = await FailoverDispatcher(_OwnershipFailRegistry(), breaker).dispatch(
        _ctx(), [_dest(1), _dest(2)], ["m"]
    )
    assert out.destination.id == 2
    assert out.attempts[0].outcome == "skipped" and out.attempts[0].reason == "config_defect"
    assert breaker.is_open(1) is False


@pytest.mark.asyncio
async def test_messages_object_identity_preserved_across_attempts():
    """The exact same `messages` list object must reach every attempt, unmutated."""
    seen_ids: list[int] = []

    class _IdConnector:
        """Records id(messages) on every call so identity can be checked."""

        def __init__(self, *, exc=None, text=None):
            """Configure this fake to raise `exc` or return `text`."""
            self._exc, self._text = exc, text

        async def chat_completion(self, messages, model, **kw):
            """Record the messages object's id, then raise or return as configured."""
            seen_ids.append(id(messages))
            if self._exc:
                raise self._exc
            return self._text, {"finish_reason": "stop"}

    reg = _Registry(
        {
            1: _IdConnector(exc=ProviderServerError("openai", "m", "503", status_code=503)),
            2: _IdConnector(text="ok"),
        }
    )
    messages = ["m"]
    out = await FailoverDispatcher(reg, DestinationBreaker()).dispatch(
        _ctx(), [_dest(1), _dest(2)], messages
    )
    assert seen_ids == [id(messages), id(messages)]
    assert messages == ["m"]  # never mutated
    assert out.text == "ok"


@pytest.mark.asyncio
async def test_stream_timeout_to_first_chunk_fails_over():
    """timeout_seconds also bounds time-to-first-chunk when ctx.stream is True."""
    reg = _Registry({1: _Connector(hang=True), 2: _Connector(text="served")})
    d1 = _dest(1)
    object.__setattr__(d1, "timeout_seconds", 1)
    out = await FailoverDispatcher(reg, DestinationBreaker()).dispatch(
        _ctx(stream=True), [d1, _dest(2)], ["m"]
    )
    assert out.destination.id == 2
    assert out.attempts[0].reason == "timeout"


@pytest.mark.asyncio
async def test_failover_metric_and_attempts_recorded_on_retry_success():
    """A retryable failure followed by a success records the attempt trail + failover hop."""
    metrics = _FakeMetrics()
    reg = _Registry(
        {
            1: _Connector(exc=ProviderServerError("openai", "m", "503", status_code=503)),
            2: _Connector(text="from-standby"),
        }
    )
    out = await FailoverDispatcher(reg, DestinationBreaker(), metrics=metrics).dispatch(
        _ctx(), [_dest(1, provider="openai"), _dest(2, provider="anthropic")], ["m"]
    )
    assert out.destination.id == 2
    assert [a.destination_id for a in out.attempts] == [1, 2]
    assert metrics.attempts == [("openai", "failed"), ("anthropic", "ok")]
    assert metrics.failovers == [("openai", "anthropic", "server_error")]
    assert metrics.gauge == [("1", False), ("2", False)]


@pytest.mark.asyncio
async def test_breaker_half_open_second_probe_is_skipped():
    """Once cooldown elapses, only ONE half-open probe may be reserved at a time."""
    breaker = DestinationBreaker(failure_threshold=1, cooldown_seconds=0)
    breaker.record_failure(1)
    assert breaker.reserve_probe(1) is True  # a concurrent request already took the probe
    reg = _Registry({1: _Connector(text="nope"), 2: _Connector(text="served")})
    out = await FailoverDispatcher(reg, breaker).dispatch(_ctx(), [_dest(1), _dest(2)], ["m"])
    assert out.destination.id == 2
    assert out.attempts[0].outcome == "skipped" and out.attempts[0].reason == "breaker_open"


@pytest.mark.asyncio
async def test_all_destinations_skipped_raises_exhausted_with_default_status():
    """No destination ever attempted (all breaker-open) still raises a well-formed exhaustion."""
    breaker = DestinationBreaker(failure_threshold=1, cooldown_seconds=300)
    breaker.record_failure(1)
    reg = _Registry({1: _Connector(text="nope")})
    with pytest.raises(DestinationsExhausted) as ei:
        await FailoverDispatcher(reg, breaker).dispatch(_ctx(), [_dest(1)], ["m"])
    assert ei.value.status_code() == 502
    assert ei.value.retry_after() is None


# --- fix round 1: streaming success drain + narrowed transport-error taxonomy ---


@pytest.mark.asyncio
async def test_stream_success_drains_multiple_chunks_and_closes_generator():
    """A successful multi-chunk stream concatenates text, captures usage, and records success."""
    chunks = [
        SimpleNamespace(delta="Hel", usage=None, done=False),
        SimpleNamespace(delta="lo", usage=None, done=False),
        SimpleNamespace(delta="!", usage={"finish_reason": "stop", "output_tokens": 5}, done=True),
    ]
    connector = _MultiChunkStreamConnector(chunks)
    reg = _Registry({1: connector})
    breaker = _SpyBreaker()
    out = await FailoverDispatcher(reg, breaker).dispatch(_ctx(stream=True), [_dest(1)], ["m"])
    assert out.text == "Hello!"
    assert out.usage == {"finish_reason": "stop", "output_tokens": 5}
    assert out.finish_reason == "stop"
    assert out.attempts[-1].outcome == "ok"
    assert breaker.success_calls == [1]
    assert breaker.failure_calls == []
    assert connector.closed is True


@pytest.mark.asyncio
async def test_transport_error_is_retryable_and_fails_over():
    """A raw transport exception (not already a ProviderError) is retryable and fails over."""
    breaker = _SpyBreaker()
    reg = _Registry(
        {
            1: _Connector(exc=ConnectionRefusedError("connection refused")),
            2: _Connector(text="from-standby"),
        }
    )
    out = await FailoverDispatcher(reg, breaker).dispatch(_ctx(), [_dest(1), _dest(2)], ["m"])
    assert out.destination.id == 2
    assert out.attempts[0].outcome == "failed" and out.attempts[0].reason == "server_error"
    assert breaker.failure_calls == [1]


@pytest.mark.asyncio
async def test_unknown_exception_propagates_unchanged_no_failover():
    """A bug (KeyError) from the connector propagates untouched -- no failover, no breaker trip."""

    class _CountingConnector(_Connector):
        """Wraps _Connector to count chat_completion invocations."""

        def __init__(self, **kw):
            """Start the call counter at zero."""
            super().__init__(**kw)
            self.calls = 0

        async def chat_completion(self, messages, model, **kw):
            """Increment the call counter, then delegate to the fake behaviour."""
            self.calls += 1
            return await super().chat_completion(messages, model, **kw)

    c2 = _CountingConnector(text="should-not-run")
    breaker = _SpyBreaker()
    reg = _Registry({1: _Connector(exc=KeyError("boom")), 2: c2})
    with pytest.raises(KeyError):
        await FailoverDispatcher(reg, breaker).dispatch(_ctx(), [_dest(1), _dest(2)], ["m"])
    assert breaker.failure_calls == []
    assert breaker.success_calls == []
    assert c2.calls == 0
