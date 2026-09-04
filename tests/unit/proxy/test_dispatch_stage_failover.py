"""DispatchStage's destination-failover branch (spec §5.1; task-14 brief).

Covers: gate-off falls through to the existing path uncounted-as-SQL but
counted as a metric (S10); a resolved destination list dispatches via
FailoverDispatcher and populates ctx exactly like the legacy path so
MeterStage/SecurityOutStage behave identically; DestinationsExhausted maps to
status/block_reason/retry_after; and the branch is fully inert (byte-for-byte
legacy) when the failover collaborators are absent.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from proxy.apps.proxy_server.pipeline.stages import DispatchStage, PipelineContext
from shared.routing.destinations import Destination
from shared.routing.failover import DestinationAttempt, DestinationsExhausted, Outcome
from shared.utils.llm_connectors import ProviderClientError, ProviderRateLimitError


def _dest():
    return Destination(
        id=1,
        organization_id=7,
        model="claude-sonnet-4",
        priority=0,
        provider_id=3,
        provider_type="bedrock",
        endpoint_url=None,
        region="us-west-2",
        provider_model_id="anthropic.claude-sonnet-4-v1:0",
        timeout_seconds=30,
        credential_id=5,
        owner_org_id=7,
        credential_version="v1",
    )


class _Gate:
    """Stub FailoverGate returning a fixed (enabled, reason) pair."""

    def __init__(self, enabled, reason="ok"):
        self._e, self._r = enabled, reason

    async def evaluate(self, org_id):
        return (self._e, self._r)


class _Resolver:
    """Stub DestinationResolver recording every resolve() call it receives."""

    def __init__(self, dests):
        self.dests = dests
        self.calls = []

    async def resolve(self, org_id, model, *, pin=None, local_only=False):
        self.calls.append((org_id, model, pin, local_only))
        return self.dests


class _Dispatcher:
    """Stub FailoverDispatcher returning a fixed outcome or raising a fixed exception."""

    def __init__(self, outcome=None, exc=None):
        self._o, self._exc = outcome, exc
        self.messages = None

    async def dispatch(self, ctx, dests, messages):
        self.messages = messages
        if self._exc:
            raise self._exc
        return self._o


class _Metrics:
    """Stub metrics sink recording gate-denied reasons."""

    def __init__(self):
        self.denied = []

    def record_destination_gate_denied(self, reason):
        self.denied.append(reason)


class _UpstreamFilter:
    """Stub §8.7 UpstreamFilter recording pseudonymize/depseudonymize/cleanup calls."""

    def __init__(self):
        self.apply_calls = []
        self.depseudo_calls = []
        self.cleanup_calls = []

    async def apply(self, content, resolved, destination_kind, user):
        self.apply_calls.append((content, destination_kind))
        return SimpleNamespace(text=f"[REDACTED]{content}", mapping_id="map-1")

    async def depseudonymize(self, text, mapping_id):
        self.depseudo_calls.append((text, mapping_id))
        return text.replace("[REDACTED]", "")

    async def cleanup(self, mapping_id):
        self.cleanup_calls.append(mapping_id)


class _PolicyResolver:
    """Stub §8.1 PolicyResolver returning a fixed resolved policy."""

    async def resolve(self, org_id, target_model, tool_name=None, direction="input"):
        return SimpleNamespace(applies_to="all")


class _Features:
    """Stub feature-flag helper returning a fixed is_feature_enabled() result."""

    def __init__(self, enabled=True):
        self._enabled = enabled

    def is_feature_enabled(self, flag_key, distinct_id=None):
        return self._enabled


def _stage(gate, resolver, dispatcher, metrics=None):
    return DispatchStage(
        name="dispatch",
        router=SimpleNamespace(select_provider=lambda *a, **k: None),
        connectors={},
        failover_gate=gate,
        destination_resolver=resolver,
        failover_dispatcher=dispatcher,
        metrics=metrics,
    )


def _stage_v2(gate, resolver, dispatcher, *, upstream_filter, policy_resolver, features):
    return DispatchStage(
        name="dispatch",
        router=SimpleNamespace(select_provider=lambda *a, **k: None),
        connectors={},
        upstream_filter=upstream_filter,
        policy_resolver=policy_resolver,
        features=features,
        failover_gate=gate,
        destination_resolver=resolver,
        failover_dispatcher=dispatcher,
    )


def _ctx():
    ctx = PipelineContext(
        user=SimpleNamespace(tenant_id=7),
        body={},
        model="claude-sonnet-4",
        messages=[{"role": "user", "content": "hi"}],
    )
    ctx.requested_model = "claude-sonnet-4"
    return ctx


@pytest.mark.asyncio
async def test_failover_branch_populates_ctx_and_marker():
    """A resolved destination + successful dispatch populates ctx exactly like the legacy path."""
    outcome = Outcome(
        destination=_dest(),
        text="answer",
        usage={"input_tokens": 3, "output_tokens": 4, "finish_reason": "stop"},
        finish_reason="stop",
        attempts=(DestinationAttempt(1, "bedrock", "ok", None),),
    )
    stage = _stage(_Gate(True), _Resolver([_dest()]), _Dispatcher(outcome=outcome))
    out = await stage(_ctx())
    assert out.blocked is False
    assert out.provider == "bedrock"
    assert out.requested_model == "claude-sonnet-4"
    assert out.model == "anthropic.claude-sonnet-4-v1:0"  # provider_model_id
    assert out.response_text == "answer"
    assert out.usage["output_tokens"] == 4
    assert out.destination["role"] == "active"


@pytest.mark.asyncio
async def test_gate_off_skips_resolve_and_counts_denied():
    """Gate denial never resolves destinations and counts the denial reason (S10)."""
    resolver = _Resolver([_dest()])
    metrics = _Metrics()
    dispatcher = _Dispatcher()
    stage = _stage(_Gate(False, "flag_off"), resolver, dispatcher, metrics=metrics)
    # Existing path has no connectors -> it will block with no_available_providers,
    # but the point is: no resolve()/dispatch() call, and the denial is counted (S10).
    out = await stage(_ctx())
    assert resolver.calls == []  # no new SQL on the hot path
    assert dispatcher.messages is None  # legacy connector path taken, not the dispatcher
    assert metrics.denied == ["flag_off"]
    assert out.blocked is True and out.block_reason == "no_available_providers"


@pytest.mark.asyncio
async def test_resolver_receives_pin_and_local_only():
    """ctx.provider_pin/local_only thread through to the resolver; empty result never dispatches."""
    resolver = _Resolver([])  # empty -> falls through to existing path
    dispatcher = _Dispatcher()
    ctx = _ctx()
    ctx.provider_pin = "bedrock"
    ctx.local_only = True
    stage = _stage(_Gate(True), resolver, dispatcher)
    await stage(ctx)
    assert resolver.calls[0] == (7, "claude-sonnet-4", "bedrock", True)
    assert dispatcher.messages is None  # no destinations -> dispatch() never called


@pytest.mark.asyncio
async def test_security_v2_pseudonymize_and_depseudonymize_around_dispatch():
    """security_v2 pseudonymizes ctx.messages before dispatch and de-pseudonymizes after.

    Winning text is de-pseudonymized and the Valkey map cleaned up (S11) --
    exactly like the existing (non-failover) DispatchStage path.
    """
    upstream_filter = _UpstreamFilter()
    outcome = Outcome(
        destination=_dest(),
        text="[REDACTED]answer",
        usage={"input_tokens": 3, "output_tokens": 4},
        finish_reason="stop",
        attempts=(),
    )
    dispatcher = _Dispatcher(outcome=outcome)
    stage = _stage_v2(
        _Gate(True),
        _Resolver([_dest()]),
        dispatcher,
        upstream_filter=upstream_filter,
        policy_resolver=_PolicyResolver(),
        features=_Features(True),
    )
    out = await stage(_ctx())
    assert out.blocked is False
    # messages pseudonymized before the dispatcher ever sees them
    assert dispatcher.messages[0]["content"] == "[REDACTED]hi"
    # winning text de-pseudonymized, map cleaned up, id cleared afterward
    assert out.response_text == "answer"
    assert upstream_filter.depseudo_calls == [("[REDACTED]answer", "map-1")]
    assert upstream_filter.cleanup_calls == ["map-1"]
    assert out.upstream_mapping_id is None


@pytest.mark.asyncio
async def test_security_v2_flag_off_skips_pseudonymize():
    """features.is_feature_enabled() False -> messages reach the dispatcher unfiltered."""
    upstream_filter = _UpstreamFilter()
    outcome = Outcome(
        destination=_dest(), text="answer", usage={}, finish_reason="stop", attempts=()
    )
    dispatcher = _Dispatcher(outcome=outcome)
    stage = _stage_v2(
        _Gate(True),
        _Resolver([_dest()]),
        dispatcher,
        upstream_filter=upstream_filter,
        policy_resolver=_PolicyResolver(),
        features=_Features(False),
    )
    out = await stage(_ctx())
    assert out.blocked is False
    assert upstream_filter.apply_calls == []
    assert dispatcher.messages[0]["content"] == "hi"


@pytest.mark.asyncio
async def test_destinations_exhausted_maps_to_status():
    """No retryable error ever seen -> 502, block_reason destinations_exhausted."""
    exc = DestinationsExhausted((), None)
    stage = _stage(_Gate(True), _Resolver([_dest()]), _Dispatcher(exc=exc))
    out = await stage(_ctx())
    assert out.blocked is True and out.status_code == 502
    assert out.block_reason == "destinations_exhausted"


@pytest.mark.asyncio
async def test_destinations_exhausted_429_sets_usage_meta_retry_after():
    """A 429 last error sets ctx.usage_meta['retry_after'] for the response header."""
    exc = DestinationsExhausted(
        (), ProviderRateLimitError("openai", "m", "429", status_code=429, retry_after=5.0)
    )
    stage = _stage(_Gate(True), _Resolver([_dest()]), _Dispatcher(exc=exc))
    out = await stage(_ctx())
    assert out.blocked is True and out.status_code == 429
    assert out.usage_meta["retry_after"] == 5.0


@pytest.mark.asyncio
async def test_provider_client_error_maps_to_its_status_code():
    """A 4xx ProviderClientError from the dispatcher is not retried -- it maps straight through."""
    exc = ProviderClientError("openai", "gpt-4", "forbidden", status_code=403)
    stage = _stage(_Gate(True), _Resolver([_dest()]), _Dispatcher(exc=exc))
    out = await stage(_ctx())
    assert out.blocked is True
    assert out.status_code == 403
    assert out.block_reason == "provider_error_403"


@pytest.mark.asyncio
async def test_no_failover_collaborators_is_existing_path():
    """gate/resolver/dispatcher all None -> failover branch inert (byte-for-byte, S10)."""
    stage = DispatchStage(
        name="dispatch",
        router=SimpleNamespace(select_provider=lambda *a, **k: None),
        connectors={},
    )
    out = await stage(_ctx())
    assert out.blocked is True and out.block_reason == "no_available_providers"
