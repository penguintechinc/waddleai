"""DedupStore tests: intra-request elision, savings accounting, §6.3 prefix feed, isolation."""

import pytest

from shared.memory.dedup_store import DedupStore
from shared.memory.token_len_cache import TokenLenCache
from tests.unit.memory.test_scratchpad import FakeValkey

BIG_BLOCK = " ".join(f"tok{i}" for i in range(50))  # 50-word block
OTHER_BLOCK = " ".join(f"other{i}" for i in range(50))
SMALL_BLOCK = "just a few words"


async def _word_counter(text: str) -> int:
    return len(text.split())


@pytest.fixture
def valkey() -> FakeValkey:
    """Fresh in-memory Valkey double per test."""
    return FakeValkey()


@pytest.fixture
def token_len_cache(valkey) -> TokenLenCache:
    """TokenLenCache backed by the fixture Valkey double."""
    return TokenLenCache(valkey)


@pytest.fixture
def store(valkey) -> DedupStore:
    """DedupStore backed by the fixture Valkey double."""
    return DedupStore(valkey)


class TestElisionBasics:
    """elide_intra_request: duplicate-block reduction, size floor, distinct blocks kept."""

    @pytest.mark.asyncio
    async def test_doubly_pasted_block_reduced_to_one_canonical_plus_stub(
        self, store, token_len_cache
    ):
        """A block pasted twice keeps the first copy and stubs the second."""
        messages = [
            {"role": "user", "content": BIG_BLOCK},
            {"role": "user", "content": "some other short reply"},
            {"role": "user", "content": BIG_BLOCK},
        ]
        new_messages, _tools, _system, tokens_saved = await store.elide_intra_request(
            messages,
            None,
            None,
            model="gpt-4",
            token_len_cache=token_len_cache,
            floor_tokens=10,
            counter=_word_counter,
        )

        assert new_messages[0]["content"] == BIG_BLOCK  # canonical, untouched
        assert new_messages[2]["content"] == "[deduplicated: see block #1 above]"
        assert tokens_saved > 0

    @pytest.mark.asyncio
    async def test_triple_occurrence_one_copy_two_stubs(self, store, token_len_cache):
        """A block pasted three times keeps one copy and stubs both repeats."""
        messages = [
            {"role": "user", "content": BIG_BLOCK},
            {"role": "user", "content": BIG_BLOCK},
            {"role": "user", "content": BIG_BLOCK},
        ]
        new_messages, _tools, _system, tokens_saved = await store.elide_intra_request(
            messages,
            None,
            None,
            model="gpt-4",
            token_len_cache=token_len_cache,
            floor_tokens=10,
            counter=_word_counter,
        )
        assert new_messages[0]["content"] == BIG_BLOCK
        assert new_messages[1]["content"] == "[deduplicated: see block #1 above]"
        assert new_messages[2]["content"] == "[deduplicated: see block #1 above]"
        assert tokens_saved > 0

    @pytest.mark.asyncio
    async def test_block_below_size_floor_untouched(self, store, token_len_cache):
        """A duplicated block under floor_tokens is left untouched, zero savings."""
        messages = [
            {"role": "user", "content": SMALL_BLOCK},
            {"role": "user", "content": SMALL_BLOCK},
        ]
        new_messages, _tools, _system, tokens_saved = await store.elide_intra_request(
            messages,
            None,
            None,
            model="gpt-4",
            token_len_cache=token_len_cache,
            floor_tokens=1000,
            counter=_word_counter,
        )
        assert new_messages[0]["content"] == SMALL_BLOCK
        assert new_messages[1]["content"] == SMALL_BLOCK
        assert tokens_saved == 0

    @pytest.mark.asyncio
    async def test_two_different_blocks_both_kept(self, store, token_len_cache):
        """Two distinct large blocks are both kept verbatim."""
        messages = [
            {"role": "user", "content": BIG_BLOCK},
            {"role": "user", "content": OTHER_BLOCK},
        ]
        new_messages, _tools, _system, tokens_saved = await store.elide_intra_request(
            messages,
            None,
            None,
            model="gpt-4",
            token_len_cache=token_len_cache,
            floor_tokens=10,
            counter=_word_counter,
        )
        assert new_messages[0]["content"] == BIG_BLOCK
        assert new_messages[1]["content"] == OTHER_BLOCK
        assert tokens_saved == 0


class TestObserve:
    """observe: §6.3 prefix-hash observation-counter key shape and increment."""

    @pytest.mark.asyncio
    async def test_observe_writes_prefix_hash_keys_in_expected_shape(self, store, valkey):
        """Observe writes a waddleai:prefix:{vkey_id}:{hash} counter key."""
        from shared.memory.dedup_store import _content_hash

        await store.observe(1, "sess-a", "vkey-1", [BIG_BLOCK])
        content_hash = _content_hash(BIG_BLOCK)
        expected_key = f"waddleai:prefix:vkey-1:{content_hash}"
        assert valkey.store[expected_key] == "1"

    @pytest.mark.asyncio
    async def test_repeat_observation_increments_counter(self, store, valkey):
        """Observing the same block twice increments its counter to 2."""
        from shared.memory.dedup_store import _content_hash

        await store.observe(1, "sess-a", "vkey-1", [BIG_BLOCK])
        await store.observe(1, "sess-a", "vkey-1", [BIG_BLOCK])
        content_hash = _content_hash(BIG_BLOCK)
        expected_key = f"waddleai:prefix:vkey-1:{content_hash}"
        assert valkey.store[expected_key] == "2"


class TestIsolationSecurity:
    """SECURITY: the canonical block store is scoped per (org, session)."""

    @pytest.mark.asyncio
    async def test_canonical_store_scoped_per_org_and_session(self, store):
        """The canonical block resolves only under its own (org, session)."""
        from shared.memory.dedup_store import _content_hash

        await store.observe(1, "sess-a", "vkey-1", [BIG_BLOCK])
        content_hash = _content_hash(BIG_BLOCK)

        assert await store.get_canonical(1, "sess-a", content_hash) == BIG_BLOCK
        assert await store.get_canonical(1, "sess-b", content_hash) is None  # different session
        assert await store.get_canonical(2, "sess-a", content_hash) is None  # different org


class TestIdempotency:
    """Eliding already-elided messages is a no-op."""

    @pytest.mark.asyncio
    async def test_eliding_already_elided_messages_is_noop(self, store, token_len_cache):
        """Running elision again on its own output changes nothing further."""
        messages = [
            {"role": "user", "content": BIG_BLOCK},
            {"role": "user", "content": BIG_BLOCK},
        ]
        once, _tools, _system, _saved = await store.elide_intra_request(
            messages,
            None,
            None,
            model="gpt-4",
            token_len_cache=token_len_cache,
            floor_tokens=10,
            counter=_word_counter,
        )
        twice, _tools2, _system2, saved2 = await store.elide_intra_request(
            once,
            None,
            None,
            model="gpt-4",
            token_len_cache=token_len_cache,
            floor_tokens=10,
            counter=_word_counter,
        )
        assert once == twice
        assert saved2 == 0


class TestToolsAndSystemDedup:
    """tools/system list entries: exact duplicates dropped, strings passed through."""

    @pytest.mark.asyncio
    async def test_duplicate_tool_schema_entries_dropped(self, store, token_len_cache):
        """An exact-duplicate tool schema entry is dropped, distinct ones kept."""
        tools = [
            {"name": "search", "parameters": {}},
            {"name": "search", "parameters": {}},
            {"name": "fetch", "parameters": {}},
        ]
        _messages, new_tools, _system, _saved = await store.elide_intra_request(
            [],
            tools,
            None,
            model="gpt-4",
            token_len_cache=token_len_cache,
            floor_tokens=10,
            counter=_word_counter,
        )
        assert new_tools == [
            {"name": "search", "parameters": {}},
            {"name": "fetch", "parameters": {}},
        ]

    @pytest.mark.asyncio
    async def test_string_system_prompt_passed_through(self, store, token_len_cache):
        """A string system prompt is returned unchanged."""
        _messages, _tools, new_system, _saved = await store.elide_intra_request(
            [],
            None,
            "you are a helpful assistant",
            model="gpt-4",
            token_len_cache=token_len_cache,
            floor_tokens=10,
            counter=_word_counter,
        )
        assert new_system == "you are a helpful assistant"


class TestTokensSavedAccuracy:
    """tokens_saved matches a fresh token-count delta."""

    @pytest.mark.asyncio
    async def test_tokens_saved_matches_fresh_count_delta(self, store, token_len_cache):
        """tokens_saved equals the original block's token count minus the stub's."""
        messages = [
            {"role": "user", "content": BIG_BLOCK},
            {"role": "user", "content": BIG_BLOCK},
        ]
        _new_messages, _tools, _system, tokens_saved = await store.elide_intra_request(
            messages,
            None,
            None,
            model="gpt-4",
            token_len_cache=token_len_cache,
            floor_tokens=10,
            counter=_word_counter,
        )
        original_tokens = await _word_counter(BIG_BLOCK)
        stub_tokens = await _word_counter("[deduplicated: see block #1 above]")
        assert tokens_saved == original_tokens - stub_tokens
