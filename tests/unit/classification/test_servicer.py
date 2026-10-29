"""Tests for the ClassificationServicer orchestration (T9), over a real grpc.aio channel."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import grpc
import pytest
import pytest_asyncio

from services.classification.app import engine as eng
from services.classification.app.engine import RawEntity
from services.classification.app.failmode import PolicyOutcome
from services.classification.app.grpc_gen import classification_pb2 as pb
from services.classification.app.grpc_gen import classification_pb2_grpc as pb_grpc
from services.classification.app.servicer import ClassificationServicer
from shared.security.ner_filter import ENTITY_CONFIG

MAX_BYTES = 64
ORG = "11111111-1111-1111-1111-111111111111"
TEXT = "Alice lives here"


class FakeEngine:
    """Engine double: returns scripted entities or raises a scripted error."""

    backend = "presidio"
    fingerprint = "presidio|m|1|presidio=2"
    ready = True

    def __init__(self) -> None:
        """Start with one PERSON entity and no error."""
        self.raw = [RawEntity("PERSON", 0, 5, 0.9), RawEntity("LOCATION", 12, 16, 0.9)]
        self.error: Exception | None = None
        self.calls = 0
        self.deadlines: list[float] = []

    async def analyze(self, text: str, language: str, deadline_s: float) -> list[RawEntity]:
        """Record the call, then raise or return."""
        self.calls += 1
        self.deadlines.append(deadline_s)
        if self.error is not None:
            raise self.error
        return list(self.raw)


class FakeCache:
    """Cache double with in-memory dict and call counters."""

    def __init__(self) -> None:
        """Start empty."""
        self.store: dict[str, list[RawEntity]] = {}
        self.gets = 0
        self.puts = 0
        self.keys: list[tuple[str, str]] = []

    def key(self, fingerprint: str, org_public_id: str, text: str) -> str:
        """Deterministic per-org key."""
        self.keys.append((fingerprint, org_public_id))
        return f"{fingerprint}:{org_public_id}:{text}"

    async def get(self, key: str) -> list[RawEntity] | None:
        """Count and look up."""
        self.gets += 1
        return self.store.get(key)

    async def put(self, key: str, raw: list[RawEntity]) -> None:
        """Count and store."""
        self.puts += 1
        self.store[key] = raw


@dataclass(slots=True)
class FakeGate:
    """Gate double."""

    on: bool = True
    seen: list[str] = field(default_factory=list)

    async def enabled(self, flag_distinct_id: str) -> bool:
        """Record the id and answer."""
        self.seen.append(flag_distinct_id)
        return self.on


class FakePolicy:
    """Policy double mirroring the real closed/open semantics."""

    def __init__(self) -> None:
        """Record calls."""
        self.calls: list[dict[str, Any]] = []

    async def apply(self, **kw: Any) -> PolicyOutcome:
        """CLOSED blocks; anything else is fail-open."""
        self.calls.append(kw)
        closed = kw["requested_mode"] == pb.FAIL_MODE_CLOSED
        mode = pb.FAIL_MODE_CLOSED if closed else pb.FAIL_MODE_OPEN
        return PolicyOutcome(degraded=True, block_recommended=closed, effective_mode=mode)


@dataclass(slots=True)
class Env:
    """Wired servicer + doubles + client stub."""

    stub: Any
    engine: FakeEngine
    cache: FakeCache
    gate: FakeGate
    policy: FakePolicy


@pytest_asyncio.fixture
async def env() -> AsyncIterator[Env]:
    """Serve the servicer on an ephemeral localhost port and yield a client."""
    engine, cache, gate, policy = FakeEngine(), FakeCache(), FakeGate(), FakePolicy()
    server = grpc.aio.server()
    pb_grpc.add_ClassificationServiceServicer_to_server(
        ClassificationServicer(engine, cache, gate, policy, max_text_bytes=MAX_BYTES), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        yield Env(pb_grpc.ClassificationServiceStub(channel), engine, cache, gate, policy)
    await server.stop(None)


def req(**kw: Any) -> Any:
    """Build a valid AnalyzeRequest, overridable per test."""
    base: dict[str, Any] = {
        "api_version": "v1",
        "text": TEXT,
        "language": "en",
        "phase": pb.FILTER_PHASE_INPUT,
        "org_public_id": ORG,
        "flag_distinct_id": "flag-id",
    }
    base.update(kw)
    return pb.AnalyzeRequest(**base)


async def code_of(env: Env, request: Any) -> grpc.aio.AioRpcError:
    """Run Analyze expecting an RPC error and return it."""
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await env.stub.Analyze(request)
    return ei.value


@pytest.mark.parametrize("version", ["", "v2"])
async def test_unknown_api_version_unimplemented(env: Env, version: str) -> None:
    """Unknown/missing api_version is UNIMPLEMENTED and does no work."""
    err = await code_of(env, req(api_version=version))
    assert err.code() == grpc.StatusCode.UNIMPLEMENTED
    assert f"api_version {version} not supported" in err.details()
    assert env.engine.calls == 0


async def test_non_en_language_invalid_argument(env: Env) -> None:
    """Non-English is INVALID_ARGUMENT, never degraded, even under CLOSED."""
    err = await code_of(env, req(language="de", fail_mode=pb.FAIL_MODE_CLOSED))
    assert err.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert env.policy.calls == [] and env.engine.calls == 0


async def test_oversize_text_invalid_argument(env: Env) -> None:
    """Oversize (by UTF-8 bytes) is INVALID_ARGUMENT, never degraded, even under CLOSED."""
    err = await code_of(env, req(text="é" * (MAX_BYTES // 2 + 1), fail_mode=pb.FAIL_MODE_CLOSED))
    assert err.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert env.policy.calls == [] and env.engine.calls == 0
    ok = await env.stub.Analyze(req(text="é" * (MAX_BYTES // 2)))  # exactly at the limit
    assert ok.ner_status == pb.NER_STATUS_OK


async def test_empty_text_ok_no_spans(env: Env) -> None:
    """Empty text is OK with no spans and no engine call."""
    r = await env.stub.Analyze(req(text=""))
    assert list(r.spans) == [] and not r.degraded
    assert r.ner_status == pb.NER_STATUS_OK
    assert env.engine.calls == 0


async def test_gate_disabled_returns_ok_gate_disabled_not_degraded(env: Env) -> None:
    """A closed gate skips the tier: GATE_DISABLED, not degraded, not blocked, even CLOSED."""
    env.gate.on = False
    r = await env.stub.Analyze(req(fail_mode=pb.FAIL_MODE_CLOSED))
    assert r.ner_status == pb.NER_STATUS_GATE_DISABLED
    assert not r.degraded and not r.block_recommended and list(r.spans) == []
    assert env.engine.calls == 0 and env.policy.calls == []
    assert env.gate.seen == ["flag-id"]


async def test_cache_hit_skips_engine_and_sets_cache_hit(env: Env) -> None:
    """A cached entry is served without the engine and flagged cache_hit."""
    await env.stub.Analyze(req())
    r = await env.stub.Analyze(req())
    assert r.cache_hit and env.engine.calls == 1 and len(r.spans) == 2
    assert r.ner_status == pb.NER_STATUS_OK and r.backend == "presidio"


async def test_cache_miss_populates_cache(env: Env) -> None:
    """A miss runs the engine, stores raw entities and reports cache_hit False."""
    r = await env.stub.Analyze(req())
    assert not r.cache_hit and env.engine.calls == 1
    assert env.cache.puts == 1 and env.cache.gets == 1
    assert r.model_fingerprint == env.engine.fingerprint
    assert r.analyzed_chars == len(TEXT) and not r.truncated


@pytest.mark.parametrize(
    ("error", "reason", "status"),
    [
        (eng.EngineUnavailable("x"), pb.DEGRADED_REASON_BACKEND_UNAVAILABLE,
         pb.NER_STATUS_BACKEND_UNAVAILABLE),
        (eng.EngineOverloaded("x"), pb.DEGRADED_REASON_OVERLOADED, pb.NER_STATUS_DEGRADED),
        (eng.EngineTimeout("x"), pb.DEGRADED_REASON_TIMEOUT, pb.NER_STATUS_DEGRADED),
        (eng.EngineWorkerFailure("x"), pb.DEGRADED_REASON_WORKER_FAILURE, pb.NER_STATUS_DEGRADED),
        (eng.EngineAnalysisError("x"), pb.DEGRADED_REASON_ANALYSIS_ERROR, pb.NER_STATUS_DEGRADED),
    ],
)  # fmt: skip
async def test_engine_errors_map_to_reason_and_status(
    env: Env, error: Exception, reason: int, status: int
) -> None:
    """Each Engine* exception maps to its DegradedReason/NerStatus and nothing is cached."""
    env.engine.error = error
    r = await env.stub.Analyze(req())
    assert r.degraded and r.degraded_reason == reason and r.ner_status == status
    assert not r.block_recommended and list(r.spans) == []
    assert env.cache.puts == 0
    assert env.policy.calls[0]["reason"] == reason


async def test_engine_unavailable_returns_backend_unavailable_degraded(env: Env) -> None:
    """Unavailable backend is BACKEND_UNAVAILABLE + degraded."""
    env.engine.error = eng.EngineUnavailable("x")
    r = await env.stub.Analyze(req())
    assert r.ner_status == pb.NER_STATUS_BACKEND_UNAVAILABLE and r.degraded


async def test_engine_overloaded_maps_overloaded_degraded(env: Env) -> None:
    """Overload is DEGRADED/OVERLOADED."""
    env.engine.error = eng.EngineOverloaded("x")
    r = await env.stub.Analyze(req())
    assert r.degraded_reason == pb.DEGRADED_REASON_OVERLOADED and r.degraded


async def test_closed_mode_degraded_sets_block_recommended(env: Env) -> None:
    """Degraded under CLOSED recommends blocking; the requested mode reaches the policy."""
    env.engine.error = eng.EngineTimeout("x")
    r = await env.stub.Analyze(req(fail_mode=pb.FAIL_MODE_CLOSED))
    assert r.degraded and r.block_recommended
    call = env.policy.calls[0]
    assert call["requested_mode"] == pb.FAIL_MODE_CLOSED
    assert call["org_public_id"] == ORG and call["flag_distinct_id"] == "flag-id"
    assert call["phase"] == pb.FILTER_PHASE_INPUT


async def test_disabled_labels_applied_after_cache(env: Env) -> None:
    """SAME org + text, different disabled sets: one engine call, different spans."""
    a = await env.stub.Analyze(req(disabled_entity_labels=["PERSON"]))
    b = await env.stub.Analyze(req(disabled_entity_labels=["LOCATION"]))
    assert env.engine.calls == 1 and b.cache_hit
    assert {s.entity_label for s in a.spans} == {"LOCATION"}
    assert {s.entity_label for s in b.spans} == {"PERSON"}


async def test_empty_org_bypasses_cache(env: Env) -> None:
    """No org id: no cache get/put/key at all; the engine runs every time."""
    r1 = await env.stub.Analyze(req(org_public_id=""))
    r2 = await env.stub.Analyze(req(org_public_id=""))
    assert env.engine.calls == 2 and not r1.cache_hit and not r2.cache_hit
    assert env.cache.gets == 0 and env.cache.puts == 0 and env.cache.keys == []
    assert len(r1.spans) == 2


async def test_response_spans_have_no_text(env: Env) -> None:
    """Spans carry offsets only; the serialized response never contains the input text."""
    r = await env.stub.Analyze(req())
    assert {f.name for f in pb.EntitySpan.DESCRIPTOR.fields}.isdisjoint({"text", "matched_text"})
    assert "Alice" not in r.SerializeToString().decode("latin-1")


async def test_get_entity_config_matches_entity_config(env: Env) -> None:
    """The entity table mirrors ENTITY_CONFIG exactly."""
    r = await env.stub.GetEntityConfig(pb.GetEntityConfigRequest(api_version="v1"))
    got = {i.entity_label: (i.default_action, i.confidence_weight) for i in r.items}
    acts = {"log": pb.ENTITY_ACTION_LOG, "redact": pb.ENTITY_ACTION_REDACT,
            "block": pb.ENTITY_ACTION_BLOCK}  # fmt: skip
    assert set(got) == set(ENTITY_CONFIG)
    for label, (action, weight) in ENTITY_CONFIG.items():
        assert got[label][0] == acts[action]
        assert got[label][1] == pytest.approx(weight)
    assert all(i.entity_type != pb.ENTITY_TYPE_UNSPECIFIED for i in r.items)


async def test_get_entity_config_unknown_api_version(env: Env) -> None:
    """GetEntityConfig also enforces api_version."""
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await env.stub.GetEntityConfig(pb.GetEntityConfigRequest(api_version="nope"))
    assert ei.value.code() == grpc.StatusCode.UNIMPLEMENTED


async def test_deadline_already_expired_returns_timeout_degraded() -> None:
    """A spent deadline is a TIMEOUT degraded result without invoking the engine."""
    engine, cache, gate, policy = FakeEngine(), FakeCache(), FakeGate(), FakePolicy()
    svc = ClassificationServicer(engine, cache, gate, policy, max_text_bytes=MAX_BYTES)

    class Ctx:
        def time_remaining(self) -> float:
            return 0.0

    r = await svc.Analyze(req(), Ctx())
    assert r.degraded and r.degraded_reason == pb.DEGRADED_REASON_TIMEOUT
    assert r.ner_status == pb.NER_STATUS_DEGRADED and engine.calls == 0
    assert cache.puts == 0


async def test_no_client_deadline_uses_default_budget(env: Env) -> None:
    """Without a client deadline the engine still gets a finite positive deadline."""
    await env.stub.Analyze(req())
    assert 0 < env.engine.deadlines[0] < float("inf")


async def test_client_deadline_is_passed_to_engine(env: Env) -> None:
    """A client deadline bounds the engine deadline."""
    await env.stub.Analyze(req(), timeout=5.0)
    assert 0 < env.engine.deadlines[0] <= 5.0
