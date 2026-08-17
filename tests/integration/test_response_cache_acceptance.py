"""§6.5 acceptance suite for the response cache.

Covers eligibility E2E, replay, TTL, org-isolation security, threshold
regression, cache_control injection, flag-off zero-behavior-change proof,
and poisoning defense.

Exercises the real ProxyPipeline (AuthStage -> SecurityInStage(stub) ->
CacheStage -> DispatchStage -> SecurityOutStage(controllable) -> MeterStage)
wired to a real ResponseCache backed by in-memory fakes (FakeValkey +
a combined fake penguin-dal db exposing cache_configs and
response_cache_entries) -- no live Postgres/Valkey required, matching the
rest of this branch's test conventions (see tests/unit/cache/conftest.py).
"""

import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from proxy.apps.proxy_server.pipeline import (
    CacheStage,
    DispatchStage,
    MeterStage,
    PipelineContext,
    ProxyPipeline,
    SecurityOutStage,
    Stage,
)
from shared.cache.config import CacheConfigResolver
from shared.cache.exact import ExactCache
from shared.cache.response_cache import RESPONSE_CACHE_FLAG, ResponseCache
from shared.cache.semantic import SemanticCache
from shared.cache.upstream import AnthropicPromptCacheOrchestrator
from tests.unit.cache.conftest import FakeValkey, StubEmbedder

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Minimal query DSL + combined fake db (cache_configs + response_cache_entries)
# ---------------------------------------------------------------------------


class _Field:
    """A queryable column on a fake table: supports `==`/`>` producing a `_Cond`."""

    def __init__(self, name):
        """Store the column name this field represents."""
        self.name = name

    def __eq__(self, other):
        return _Cond(lambda row: row.get(self.name) == other)

    def __gt__(self, other):
        return _Cond(lambda row: row.get(self.name) is not None and row.get(self.name) > other)


class _Cond:
    """A composable predicate over a raw dict row, combinable with `&`/`|`."""

    def __init__(self, fn):
        """Wrap a `row -> bool` predicate function."""
        self.fn = fn

    def __and__(self, other):
        return _Cond(lambda row: self.fn(row) and other.fn(row))

    def __or__(self, other):
        return _Cond(lambda row: self.fn(row) or other.fn(row))

    def __call__(self, row):
        return bool(self.fn(row))


class _Table:
    """Field accessors + insert() for one table on a `CombinedFakeDB`."""

    def __init__(self, db, name):
        """Bind this table accessor to its owning db and table name."""
        self._db = db
        self._name = name
        self.scope_type = _Field("scope_type")
        self.scope_ref = _Field("scope_ref")
        self.org_id = _Field("org_id")
        self.model_class = _Field("model_class")
        self.context_hash = _Field("context_hash")
        self.expires_at = _Field("expires_at")
        self.id = _Field("id")

    def insert(self, **kwargs) -> int:
        """Append a new row to this table and return its assigned id."""
        new_id = self._db._next_id
        self._db._next_id += 1
        self._db.rows[self._name].append({"id": new_id, **kwargs})
        return new_id


class _SelectResult(list):
    """A list of matched rows supporting `.select()`/`.first()` chaining."""

    def select(self, *a, **k):
        """Return self -- `db(query).select()` is a no-op filter step in this fake."""
        return self

    def first(self):
        """Return the first matched row, or None if there were no matches."""
        return self[0] if self else None


class _UpdatableProxy(_SelectResult):
    """_SelectResult that also supports .update(**kwargs).

    Mutates the raw dict rows it was built from -- takes ``raw_rows``
    explicitly via __init__ rather than closing over a loop variable (see
    CombinedFakeDB.__call__), so there's exactly one binding per instance
    and no B023-style late-binding ambiguity.
    """

    def __init__(self, namespaced_rows, raw_rows):
        """Wrap the namespaced (read) rows and keep a reference to the raw (mutable) ones."""
        super().__init__(namespaced_rows)
        self._raw_rows = raw_rows

    def update(self, **kwargs):
        """Apply `kwargs` to every underlying raw row; returns the count updated."""
        for row in self._raw_rows:
            row.update(kwargs)
        return len(self._raw_rows)


class CombinedFakeDB:
    """Fake penguin-dal db exposing cache_configs + response_cache_entries."""

    def __init__(self):
        """Initialize empty cache_configs/response_cache_entries row stores."""
        self.rows = {"cache_configs": [], "response_cache_entries": []}
        self._next_id = 1
        self.cache_configs = _Table(self, "cache_configs")
        self.response_cache_entries = _Table(self, "response_cache_entries")

    def seed_global_config(self, **overrides):
        """Insert a global-scope cache_configs row with spec §6 defaults, minus overrides."""
        row = {
            "scope_type": "global",
            "scope_ref": None,
            "exact_enabled": True,
            "semantic_enabled": False,
            "semantic_threshold": 0.95,
            "ttl_seconds": 86400,
            "max_entry_kb": 256,
            "anthropic_cache_control": True,
        }
        row.update(overrides)
        self.rows["cache_configs"].append(row)

    def commit(self):
        """Record a commit call (no-op storage-wise; this fake is always durable)."""

    def __call__(self, query):
        """Evaluate `query` against both tables' rows and return an updatable proxy."""
        # Search whichever table's rows satisfy the predicate structurally;
        # both tables are tiny and field names don't collide meaningfully
        # across them for the queries this module issues.
        for table_rows in self.rows.values():
            matched_raw = [r for r in table_rows if _safe_match(query, r)]
            if matched_raw:
                matched = [SimpleNamespace(**r) for r in matched_raw]
                return _UpdatableProxy(matched, matched_raw)
        # No matches in any table -- still need a table-shaped empty result
        # that supports .update() harmlessly.
        return _UpdatableProxy([], [])


def _safe_match(query, row) -> bool:
    """Evaluate `query(row)`, treating any exception (e.g. missing field) as no-match."""
    try:
        return query(row)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Pipeline scaffolding: real stages, fake security/dispatch backends
# ---------------------------------------------------------------------------


class _PassThroughSecurityInStage(Stage):
    """A no-op SecurityInStage stand-in: forwards ctx unchanged (never blocks, never filters)."""

    def __init__(self):
        """Initialize as a "security_in"-named, unconditionally-run stage."""
        super().__init__("security_in", flag=None)

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """Return ctx unchanged."""
        return ctx


class _ControllableConnector:
    """Deterministic stub LLMConnector: echoes back a fixed reply per call count."""

    def __init__(self):
        """Initialize the call counter and last-seen-messages capture."""
        self.call_count = 0
        self.last_messages = None

    async def chat_completion(self, messages, model=None, **kwargs):
        """Return a deterministic numbered reply, recording the call and its messages."""
        self.call_count += 1
        self.last_messages = messages
        provider = "anthropic" if "claude" in (model or "") else "stub"
        usage = {
            "input_tokens": 20,
            "output_tokens": 10,
            "finish_reason": "stop",
            "provider": provider,
        }
        return f"stub reply #{self.call_count}", usage

    async def stream_chat_completion(self, messages, model=None, **kwargs):
        """Yield a deterministic two-chunk stream plus a final usage-bearing chunk."""
        from shared.utils.llm_connectors import StreamChunk

        self.call_count += 1
        final_usage = {"input_tokens": 20, "output_tokens": 10, "finish_reason": "stop"}
        yield StreamChunk(delta="stub ", done=False)
        yield StreamChunk(delta="stream", done=False)
        yield StreamChunk(delta="", usage=final_usage, done=True)


class _FakeRouter:
    """Single-provider stub router: always selects "stub" regardless of hints."""

    def __init__(self, connector):
        """Store the connector this router always routes to."""
        self.connector = connector

    def select_provider(self, model, strategy=None, preferred_backend=None):
        """Always return ("stub", model), ignoring strategy/preferred_backend."""
        return "stub", model


def _build_pipeline(
    response_cache: ResponseCache, connector: _ControllableConnector, block_output_fn=None
):
    """Assemble a real ProxyPipeline (security_in -> cache -> dispatch -> security_out -> meter)."""
    security_out = SecurityOutStage(
        name="security_out", content_filter=_NoOpContentFilter(block_output_fn)
    )
    metering_buffer = MagicMock()
    token_limiter = AsyncMock()
    stages = [
        _PassThroughSecurityInStage(),
        CacheStage(name="cache", response_cache=response_cache),
        DispatchStage(
            name="dispatch", router=_FakeRouter(connector), connectors={"stub": connector}
        ),
        security_out,
        MeterStage(name="meter", metering_buffer=metering_buffer, token_limiter=token_limiter),
    ]

    class _AlwaysOnFeatures:
        """Feature helper that reports only RESPONSE_CACHE_FLAG as enabled."""

        def is_feature_enabled(self, flag_key, distinct_id=None):
            """Return True only for RESPONSE_CACHE_FLAG."""
            return flag_key == RESPONSE_CACHE_FLAG

    pipeline = ProxyPipeline(stages=stages, features=_AlwaysOnFeatures())
    pipeline.metering_buffer = metering_buffer
    return pipeline


class _NoOpContentFilter:
    """content_filter.filter_output stub: allows unless block_fn(text) says otherwise."""

    def __init__(self, block_fn=None):
        """Store the predicate used to decide whether output should be blocked."""
        self.block_fn = block_fn or (lambda text: False)

    async def filter_output(self, text, user_id=None, org_id=None, ip=None):
        """Return an allowed/blocked verdict for `text` per the configured block_fn."""
        allowed = not self.block_fn(text)
        return SimpleNamespace(allowed=allowed, filtered_text=text, violations=[])


def _make_user(org_id: int = 1, vkey_id: int = 10):
    """Return a minimal user context (org/tenant/vkey ids) for pipeline tests."""
    return SimpleNamespace(
        id=1, user_id=1, organization_id=org_id, tenant_id=org_id, vkey_id=vkey_id, api_key_id=1
    )


def _make_response_cache(db: CombinedFakeDB, valkey: FakeValkey, embedder=None) -> ResponseCache:
    """Wire a real ResponseCache from the fake db/valkey (and optional embedder)."""
    exact = ExactCache(valkey)
    semantic = SemanticCache(db=db, embedder=embedder) if embedder is not None else None
    upstream = AnthropicPromptCacheOrchestrator(valkey)
    resolver = CacheConfigResolver(db=db, valkey=valkey)
    features = MagicMock()
    return ResponseCache(
        exact=exact,
        semantic=semantic,
        upstream=upstream,
        affinity=None,
        resolver=resolver,
        features=features,
    )


def _openai_response_json(response_text: str, finish_reason: str | None) -> dict:
    """Build a minimal OpenAI chat.completion body for a write-back call in tests."""
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response_text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    }


async def _run_openai_request(
    pipeline, user, messages, model="gpt-4o", temperature=0, stream=False, extra_body=None
):
    """Build an OpenAI-format PipelineContext and run it through `pipeline`."""
    body = {"model": model, "messages": messages, "temperature": temperature}
    if extra_body:
        body.update(extra_body)
    ctx = PipelineContext(
        user=user,
        body=body,
        model=model,
        messages=messages,
        stream=stream,
        response_format="openai",
    )
    return await pipeline.run(ctx)


# ---------------------------------------------------------------------------
# 1. Determinism-eligibility matrix E2E
# ---------------------------------------------------------------------------


class TestDeterminismEligibilityE2E:
    """Tests for determinism eligibility E2E."""

    async def test_eligible_request_twice_second_is_exact_hit(self):
        """Eligible request twice second is exact hit."""
        db = CombinedFakeDB()
        db.seed_global_config()
        valkey = FakeValkey()
        response_cache = _make_response_cache(db, valkey)
        connector = _ControllableConnector()
        pipeline = _build_pipeline(response_cache, connector)
        user = _make_user()
        messages = [{"role": "user", "content": "What is 2+2?"}]

        first = await _run_openai_request(pipeline, user, messages)
        assert first.cache_status == "miss"
        assert connector.call_count == 1

        response_json = _openai_response_json(first.response_text, first.finish_reason)
        await first.cache_write_back(response_json, first.usage)

        second = await _run_openai_request(pipeline, user, messages)
        assert second.cache_status == "exact"
        assert second.cache_hit is True
        assert connector.call_count == 1  # dispatch never called again

    async def test_ineligible_variant_twice_both_miss(self):
        """Ineligible variant twice both miss."""
        db = CombinedFakeDB()
        db.seed_global_config()
        valkey = FakeValkey()
        response_cache = _make_response_cache(db, valkey)
        connector = _ControllableConnector()
        pipeline = _build_pipeline(response_cache, connector)
        user = _make_user()
        messages = [{"role": "user", "content": "Tell me something random"}]

        first = await _run_openai_request(pipeline, user, messages, temperature=0.9)
        second = await _run_openai_request(pipeline, user, messages, temperature=0.9)

        assert first.cache_status == "miss"
        assert second.cache_status == "miss"
        assert connector.call_count == 2


# ---------------------------------------------------------------------------
# 2. Streaming replay byte-equivalence E2E
# ---------------------------------------------------------------------------


class TestStreamingReplayE2E:
    """Tests for streaming replay E2E."""

    async def test_streamed_miss_then_hit_have_identical_content_and_usage(self):
        """Streamed miss then hit have identical content and usage."""
        db = CombinedFakeDB()
        db.seed_global_config()
        valkey = FakeValkey()
        response_cache = _make_response_cache(db, valkey)
        connector = _ControllableConnector()
        pipeline = _build_pipeline(response_cache, connector)
        user = _make_user()
        messages = [{"role": "user", "content": "Stream this please"}]

        miss_ctx = await _run_openai_request(pipeline, user, messages, stream=True)
        assert miss_ctx.cache_status == "miss"
        assert miss_ctx.response_text == "stub stream"

        # First response gets written back (simulating what main.py does
        # after SecurityOutStage passes).
        response_json = _openai_response_json(miss_ctx.response_text, miss_ctx.finish_reason)
        await miss_ctx.cache_write_back(response_json, miss_ctx.usage)

        hit_ctx = await _run_openai_request(pipeline, user, messages, stream=True)
        assert hit_ctx.cache_status == "exact"
        assert hit_ctx.stream_iter is not None

        chunks = [c async for c in hit_ctx.stream_iter]
        assembled = "".join(
            __import__("orjson")
            .loads(c[len(b"data: ") :])["choices"][0]["delta"]
            .get("content", "")
            for c in chunks[:-1]
        )
        assert assembled == "stub stream" == miss_ctx.response_text
        assert hit_ctx.response_text == miss_ctx.response_text


# ---------------------------------------------------------------------------
# 3. TTL expiry
# ---------------------------------------------------------------------------


class TestTTLExpiry:
    """Tests for t t l expiry."""

    async def test_entry_no_longer_hits_after_ttl_expiry(self):
        """Entry no longer hits after ttl expiry."""
        db = CombinedFakeDB()
        db.seed_global_config(ttl_seconds=1)
        valkey = FakeValkey()
        valkey.now = lambda: 0
        response_cache = _make_response_cache(db, valkey)
        connector = _ControllableConnector()
        pipeline = _build_pipeline(response_cache, connector)
        user = _make_user()
        messages = [{"role": "user", "content": "Expire me"}]

        first = await _run_openai_request(pipeline, user, messages)
        response_json = _openai_response_json(first.response_text, first.finish_reason)
        await first.cache_write_back(response_json, first.usage)

        valkey.now = lambda: 2  # past the 1s TTL
        second = await _run_openai_request(pipeline, user, messages)
        assert second.cache_status == "miss"
        assert connector.call_count == 2


# ---------------------------------------------------------------------------
# 4. Org isolation (SECURITY)
# ---------------------------------------------------------------------------


@pytest.mark.security
class TestOrgIsolationE2E:
    """Tests for org isolation E2E."""

    async def test_org_b_request_after_org_a_warmup_is_a_miss(self):
        """Org b request after org a warmup is a miss."""
        db = CombinedFakeDB()
        db.seed_global_config()
        valkey = FakeValkey()
        response_cache = _make_response_cache(db, valkey)
        connector = _ControllableConnector()
        pipeline = _build_pipeline(response_cache, connector)
        messages = [{"role": "user", "content": "Confidential org A question"}]

        org_a_user = _make_user(org_id=100)
        org_b_user = _make_user(org_id=200)

        first = await _run_openai_request(pipeline, org_a_user, messages)
        response_json = _openai_response_json(first.response_text, first.finish_reason)
        await first.cache_write_back(response_json, first.usage)

        org_a_second = await _run_openai_request(pipeline, org_a_user, messages)
        assert org_a_second.cache_status == "exact"

        org_b_result = await _run_openai_request(pipeline, org_b_user, messages)
        assert org_b_result.cache_status == "miss"
        assert connector.call_count == 2  # org A's hit didn't dispatch; org B's miss did


# ---------------------------------------------------------------------------
# 5. Semantic corpus + threshold regression
# ---------------------------------------------------------------------------


class TestSemanticCorpusRegressionE2E:
    """Tests for semantic corpus regression E2E."""

    async def test_should_hit_corpus_hits_through_full_pipeline(self):
        """Should hit corpus hits through full pipeline."""
        db = CombinedFakeDB()
        db.seed_global_config(semantic_enabled=True, exact_enabled=False)
        valkey = FakeValkey()

        vec_cached = [1.0, 0.0]
        vec_query = [0.97, math.sqrt(1 - 0.97**2)]
        cached_question = "What is the capital of France?"
        query_question = "What's the capital city of France?"
        vectors = {cached_question: vec_cached, query_question: vec_query}
        embedder = StubEmbedder(vectors, dimensions=2)
        response_cache = _make_response_cache(db, valkey, embedder=embedder)
        connector = _ControllableConnector()
        pipeline = _build_pipeline(response_cache, connector)
        user = _make_user()

        cached_messages = [{"role": "user", "content": cached_question}]
        query_messages = [{"role": "user", "content": query_question}]

        first = await _run_openai_request(pipeline, user, cached_messages)
        response_json = _openai_response_json("Paris", "stop")
        await first.cache_write_back(response_json, first.usage)

        second = await _run_openai_request(pipeline, user, query_messages)
        assert second.cache_status == "semantic"
        assert connector.call_count == 1


# ---------------------------------------------------------------------------
# 6. cache_control injection vs recorded Anthropic responses
# ---------------------------------------------------------------------------


class TestAnthropicCacheControlE2E:
    """Tests for anthropic cache control E2E."""

    async def test_repeated_large_prefix_injects_cache_control_on_third_call(self):
        """Repeated large prefix injects cache control on third call."""
        db = CombinedFakeDB()
        db.seed_global_config()
        valkey = FakeValkey()
        response_cache = _make_response_cache(db, valkey)
        connector = _ControllableConnector()
        pipeline = _build_pipeline(response_cache, connector)
        user = _make_user()

        long_system_text = " ".join(["stable context sentence"] * 400)

        # Non-cache-eligible (temp>0 doesn't matter for upstream annotation,
        # which runs regardless of exact/semantic eligibility) -- use a
        # unique final question each call so exact caching doesn't short
        # circuit dispatch before we can inspect the annotated body.
        model = "claude-3-5-sonnet-latest"

        def _anthropic_body(final_question: str) -> dict:
            messages = [
                {"role": "user", "content": "background turn"},
                {"role": "assistant", "content": "ack"},
                {"role": "user", "content": final_question},
            ]
            return {
                "model": model,
                "system": long_system_text,
                "messages": messages,
                "temperature": 0.7,
            }

        for i in range(2):
            body = _anthropic_body(f"final question {i}")
            ctx = PipelineContext(
                user=user,
                body=body,
                model=model,
                messages=body["messages"],
                response_format="anthropic",
            )
            await pipeline.run(ctx)

        # Third call (same stable prefix) should carry the injected breakpoint.
        body = _anthropic_body("final question 2")
        ctx = PipelineContext(
            user=user,
            body=body,
            model=model,
            messages=list(body["messages"]),
            response_format="anthropic",
        )
        await pipeline.run(ctx)

        dispatched_messages = connector.last_messages
        assistant_turn = dispatched_messages[1]
        assert isinstance(assistant_turn["content"], list)
        assert assistant_turn["content"][-1]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# 7. Flag-off zero-behavior-change proof
# ---------------------------------------------------------------------------


class TestFlagOffZeroBehaviorChange:
    """Tests for flag off zero behavior change."""

    async def test_flag_off_always_dispatches_no_cache_state_created(self):
        """Flag off always dispatches no cache state created."""
        db = CombinedFakeDB()
        db.seed_global_config()
        valkey = FakeValkey()
        response_cache = _make_response_cache(db, valkey)
        connector = _ControllableConnector()

        security_out = SecurityOutStage(name="security_out", content_filter=_NoOpContentFilter())
        stages = [
            _PassThroughSecurityInStage(),
            CacheStage(name="cache", response_cache=response_cache),
            DispatchStage(
                name="dispatch", router=_FakeRouter(connector), connectors={"stub": connector}
            ),
            security_out,
            MeterStage(name="meter", metering_buffer=MagicMock(), token_limiter=AsyncMock()),
        ]

        class _AlwaysOffFeatures:
            """Always Off Features."""

            def is_feature_enabled(self, flag_key, distinct_id=None):
                """Is feature enabled."""
                return False

        pipeline = ProxyPipeline(stages=stages, features=_AlwaysOffFeatures())
        user = _make_user()
        messages = [{"role": "user", "content": "Same request every time"}]

        results = []
        for _ in range(3):
            results.append(await _run_openai_request(pipeline, user, messages))

        assert connector.call_count == 3  # every call dispatched -- no caching ever intervened
        assert all("skipped:cache" in r.stage_log for r in results)
        assert all(r.cache_status == "miss" for r in results)  # default, never touched
        assert valkey.keys_with_prefix("waddleai:cache:") == []
        assert db.rows["response_cache_entries"] == []


# ---------------------------------------------------------------------------
# 8. Blocked-response never cached (poisoning defense)
# ---------------------------------------------------------------------------


class TestPoisoningDefense:
    """Tests for poisoning defense."""

    async def test_output_filter_blocked_response_never_cached(self):
        """Output filter blocked response never cached."""
        db = CombinedFakeDB()
        db.seed_global_config()
        valkey = FakeValkey()
        response_cache = _make_response_cache(db, valkey)
        connector = _ControllableConnector()
        # Block every output for this test -- simulates SecurityOutStage
        # rejecting the response (e.g. it leaked a credential).
        pipeline = _build_pipeline(response_cache, connector, block_output_fn=lambda text: True)
        user = _make_user()
        messages = [{"role": "user", "content": "Leak something"}]

        ctx = await _run_openai_request(pipeline, user, messages)
        assert ctx.blocked is True

        # Route-handler-equivalent: only call the write-back if not blocked.
        # (main.py never calls it when ctx.blocked -- this assertion documents
        # that contract; the real guarantee is that main.py's blocked-check
        # short-circuits before reaching _maybe_write_back_cache at all.)
        if not ctx.blocked and ctx.cache_write_back is not None:
            await ctx.cache_write_back({"fake": "response"}, ctx.usage)

        # Second identical request must still dispatch -- nothing was cached.
        second = await _run_openai_request(pipeline, user, messages)
        assert second.blocked is True
        assert connector.call_count == 2
