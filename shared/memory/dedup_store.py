"""Tool-schema / system-prompt dedup store (§6A.4).

Canonical copies of large stable blocks (tool schemas, system prompts,
pasted files) keyed by content-hash per (org, session) in Valkey. Two
functions:

- ``observe``: records stable blocks and their content hashes as §6.3
  prefix-hash observation counters (``waddleai:prefix:{vkey_id}:{prefix_hash}``)
  -- these >1024-token blocks are exactly what gets ``cache_control``
  breakpoints / Ollama KV affinity on the response-cache branch.
- ``elide_intra_request``: a pure, content-mechanical function (no model
  call) -- a message content block appearing >=2x within one request, at
  or above a size floor, is reduced to a single canonical occurrence plus
  short reference stubs. tools/system list entries that exact-duplicate an
  earlier entry are dropped outright (they must remain valid schema
  objects, not free text, so there is no stub form for them).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from shared.memory.token_len_cache import TokenLenCache

logger = logging.getLogger(__name__)

DEFAULT_FLOOR_TOKENS = 512
DEDUP_STUB_PREFIX = "[deduplicated: see block #"
DEDUP_STUB_TEMPLATE = DEDUP_STUB_PREFIX + "{index} above]"


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _default_counter(text: str) -> int:
    """Rough token estimate, matching OpenAIConnector's own tiktoken-failure fallback."""
    return len(text) // 4


def _dedup_entry_list(entries: list | None) -> list | None:
    """Drop exact-duplicate entries from a tools/system list, preserving first occurrence order."""
    if entries is None:
        return entries
    seen: set[str] = set()
    result: list = []
    for entry in entries:
        key = json.dumps(entry, sort_keys=True) if isinstance(entry, dict) else str(entry)
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


class DedupStore:
    """Canonical block registry (per org+session) + intra-request elision."""

    def __init__(self, valkey: Any) -> None:
        """Wire the Valkey client the canonical registry and prefix-hash counters live in."""
        self.valkey = valkey

    @staticmethod
    def _canonical_key(org_id: int, session_id: str, content_hash: str) -> str:
        return f"waddleai:dedup:{org_id}:{session_id}:{content_hash}"

    @staticmethod
    def _prefix_key(vkey_id: str, prefix_hash: str) -> str:
        return f"waddleai:prefix:{vkey_id}:{prefix_hash}"

    async def observe(self, org_id: int, session_id: str, vkey_id: str, blocks: list[str]) -> None:
        """Record stable blocks: §6.3 prefix-hash observation counters + canonical copies."""
        for block in blocks:
            content_hash = _content_hash(block)
            await self.valkey.incr(self._prefix_key(vkey_id, content_hash))
            await self.valkey.set(self._canonical_key(org_id, session_id, content_hash), block)

    async def get_canonical(self, org_id: int, session_id: str, content_hash: str) -> str | None:
        """Session-scoped canonical lookup -- never resolves another session's block hash."""
        return await self.valkey.get(self._canonical_key(org_id, session_id, content_hash))

    async def elide_intra_request(
        self,
        messages: list[dict],
        tools: list | None,
        system: Any | None,
        *,
        model: str,
        token_len_cache: TokenLenCache,
        floor_tokens: int = DEFAULT_FLOOR_TOKENS,
        counter: Callable[[str], Awaitable[int]] | None = None,
    ) -> tuple[list[dict], list | None, Any | None, int]:
        """Reduce repeated large blocks to one canonical copy + reference stubs.

        Idempotent: content that already looks like a dedup stub is passed
        through untouched rather than being folded into the duplicate pool
        (so re-running this on its own output is a no-op).
        """
        count_fn = counter or _default_counter
        tokens_saved = 0

        seen_blocks: dict[str, int] = {}  # content hash -> canonical block number
        new_messages: list[dict] = []
        block_counter = 0

        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, str) or content.startswith(DEDUP_STUB_PREFIX):
                new_messages.append(msg)
                continue

            tokens = await token_len_cache.count(model, content, count_fn)
            if tokens < floor_tokens:
                new_messages.append(msg)
                continue

            block_hash = _content_hash(content)
            if block_hash not in seen_blocks:
                block_counter += 1
                seen_blocks[block_hash] = block_counter
                new_messages.append(msg)
                continue

            stub_text = DEDUP_STUB_TEMPLATE.format(index=seen_blocks[block_hash])
            stub_tokens = await token_len_cache.count(model, stub_text, count_fn)
            tokens_saved += max(0, tokens - stub_tokens)
            new_messages.append({**msg, "content": stub_text})

        new_tools = _dedup_entry_list(tools)
        new_system = _dedup_entry_list(system) if isinstance(system, list) else system

        return new_messages, new_tools, new_system, tokens_saved
