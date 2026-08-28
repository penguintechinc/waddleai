"""§6A proxy memory pipeline stages: ScratchpadStage, SummarizationStage, DedupStage.

Insertion point (documented in proxy/apps/proxy_server/pipeline/stages.py's
module docstring insertion-point convention): after SecurityInStage, before
DispatchStage -- context assembly happens on post-security-filter content
(poisoning defense §3.6). Relative order vs the §6 CacheStage is settled at
merge time: memory assembly runs first, so cache keys hash what would
actually be dispatched (see the coordination note at the pipeline-build
insertion point in main.py, wired in Task 13).

``KnowledgeInjectStage`` (proxy.apps.proxy_server.pipeline.knowledge_stage,
§9.5/§9.6) lands between SummarizationStage and DedupStage for the same
cache-key reason plus two more specific to it: it must run after
ScratchpadStage so its retrieval query is resolved text, not a literal
``waddleai://scratchpad/...`` marker, and after SummarizationStage so
ephemeral retrieved context is never folded into the persisted session
summary. Landing before DedupStage lets intra-request dedup/§6.3
stable-block observation also cover the injected block.

All three stages are flag-gated on ``waddleai.proxy_memory`` at the
ProxyPipeline level (coarse, whole-feature on/off -- see Stage.flag);
ProxyPipeline itself records "skipped:{name}" in ``ctx.stage_log`` when
that whole-feature flag is off, before ever calling the stage. Each stage
additionally re-resolves its own per-key ``ProxyMemoryConfig`` via an
injected ``config_resolver`` and no-ops (returns ``ctx`` unchanged) when
the specific per-key knob is off or a precondition (e.g. no session id)
isn't met. That per-key no-op is deliberately NOT also logged to
``ctx.stage_log`` -- ProxyPipeline already marks the stage "ran" once
``__call__`` returns without raising, and appending a second, conflicting
"skipped:{name}" entry from inside the stage would double up against that
(no other stage in stages.py touches stage_log itself; this follows the
same convention). Callers detect a per-key no-op via ``ctx.usage_meta``
(absent/zero fields) or by the message content being unchanged.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from shared.memory.dedup_store import DedupStore
from shared.memory.provenance import ProvenanceTag, recall
from shared.memory.scratchpad import ScratchpadStore
from shared.memory.summarizer import ConversationSummarizer
from shared.memory.token_len_cache import TokenLenCache

from .stages import PipelineContext, Stage

logger = logging.getLogger(__name__)

_SCRATCHPAD_MARKER_RE = re.compile(r"waddleai://scratchpad/([A-Za-z0-9_.\-]+)")

ConfigResolver = Callable[[Any], Awaitable[Any]]  # user_context -> ProxyMemoryConfig


def _org_and_user(user: Any) -> tuple[int | None, int | None]:
    """Extract (org_id, user_id) from ctx.user, matching the existing stages' convention."""
    org_id = getattr(user, "organization_id", None) or getattr(user, "tenant_id", None)
    user_id = getattr(user, "user_id", None) or getattr(user, "id", None)
    return org_id, user_id


async def _async_regex_sub(
    pattern: re.Pattern, resolve: Callable[[re.Match], Awaitable[str]], text: str
) -> str:
    """Like re.sub, but the replacement callable is async and runs sequentially per match."""
    matches = list(pattern.finditer(text))
    if not matches:
        return text
    parts: list[str] = []
    last_end = 0
    for match in matches:
        replacement = await resolve(match)
        parts.append(text[last_end : match.start()])
        parts.append(replacement)
        last_end = match.end()
    parts.append(text[last_end:])
    return "".join(parts)


class ScratchpadStage(Stage):
    """Opt-in plain-client ``waddleai://scratchpad/<key>`` marker substitution (§6A.1).

    Only active when the request carries a session id (X-WaddleAI-Session,
    plumbed into ctx.session_id) AND per-key config `scratchpad_substitution`
    is on AND the whole-feature flag is on. Substituted content is always
    re-filtered and provenance-wrapped via `recall` -- never raw. Unknown
    keys, cross-scope keys, and content that fails re-filter all fail open
    to the literal marker text (logged), never a hard error mid-conversation.
    """

    def __init__(
        self,
        name: str,
        store: ScratchpadStore,
        config_resolver: ConfigResolver,
        scanner: Any,
        content_filter: Any,
        flag: str | None = None,
    ) -> None:
        """Wire the store, per-key config resolver, and the security tiers `recall` uses."""
        super().__init__(name, flag)
        self.store = store
        self.config_resolver = config_resolver
        self.scanner = scanner
        self.content_filter = content_filter

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """Substitute resolvable scratchpad markers in ctx.messages; no-op without a session id."""
        if not ctx.session_id:
            return ctx

        config = await self.config_resolver(ctx.user)
        if not config.scratchpad_substitution:
            return ctx

        org_id, user_id = _org_and_user(ctx.user)
        session_id = ctx.session_id
        substitutions = 0

        async def _resolve_marker(match: re.Match) -> str:
            nonlocal substitutions
            key = match.group(1)
            value = await self.store.get(org_id, session_id, user_id, key)
            if value is None:
                logger.info(
                    "ScratchpadStage: marker %r unresolved (unknown or out-of-scope), leaving "
                    "literal",
                    key,
                )
                return match.group(0)

            tag = ProvenanceTag(
                scope_type="session",
                scope_ref=session_id,
                author_user_id=user_id,
                trust_tier="unverified",
                created_at=datetime.now(UTC),
            )
            wrapped = await recall(
                value,
                tag,
                scanner=self.scanner,
                content_filter=self.content_filter,
                user_id=user_id,
                org_id=org_id,
            )
            if wrapped is None:
                logger.warning(
                    "ScratchpadStage: marker %r failed re-filter at recall, leaving literal", key
                )
                return match.group(0)

            substitutions += 1
            return wrapped

        new_messages = []
        for msg in ctx.messages:
            content = msg.get("content", "")
            if not isinstance(content, str) or "waddleai://scratchpad/" not in content:
                new_messages.append(msg)
                continue
            new_content = await _async_regex_sub(_SCRATCHPAD_MARKER_RE, _resolve_marker, content)
            new_messages.append({**msg, "content": new_content})

        ctx.messages = new_messages
        ctx.usage_meta["scratchpad_substitutions"] = (
            ctx.usage_meta.get("scratchpad_substitutions", 0) + substitutions
        )
        return ctx


class SummarizationStage(Stage):
    """Inject summary + last keep_recent turns in place of full history (§6A.2).

    System-role messages are never touched or counted toward
    threshold/coverage -- they stay at the front of the dispatch view
    unchanged. The summary block is always inserted as user-role quoted
    data (via `recall`), never as a system/developer message. Conversation
    identity is `ctx.session_id`; no session id means no safe summary key,
    so the stage no-ops rather than guessing an identity.
    """

    def __init__(
        self,
        name: str,
        summarizer: ConversationSummarizer,
        config_resolver: ConfigResolver,
        scanner: Any,
        content_filter: Any,
        flag: str | None = None,
    ) -> None:
        """Wire the summarizer, per-key config resolver, and the security tiers `recall` uses."""
        super().__init__(name, flag)
        self.summarizer = summarizer
        self.config_resolver = config_resolver
        self.scanner = scanner
        self.content_filter = content_filter

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """Inject summary + recent-N turns into ctx.messages; no-op below threshold or off."""
        if not ctx.session_id:
            return ctx

        config = await self.config_resolver(ctx.user)
        if not config.summarization_enabled:
            return ctx

        org_id, user_id = _org_and_user(ctx.user)
        model = ctx.model or "gpt-4"

        system_msgs = [m for m in ctx.messages if m.get("role") == "system"]
        convo_msgs = [m for m in ctx.messages if m.get("role") != "system"]

        result = await self.summarizer.maybe_summarize(
            org_id, user_id, ctx.session_id, convo_msgs, config, model
        )
        if not result.applied or not result.summary:
            return ctx

        tag = ProvenanceTag(
            scope_type="session",
            scope_ref=ctx.session_id,
            author_user_id=user_id,
            trust_tier="unverified",
            created_at=datetime.now(UTC),
        )
        wrapped = await recall(
            result.summary,
            tag,
            scanner=self.scanner,
            content_filter=self.content_filter,
            user_id=user_id,
            org_id=org_id,
        )
        if wrapped is None:
            logger.warning(
                "SummarizationStage: stored summary failed re-filter at recall, using originals"
            )
            return ctx

        keep_recent = max(0, min(config.keep_recent, len(convo_msgs)))
        recent_msgs = convo_msgs[len(convo_msgs) - keep_recent :] if keep_recent else []
        summary_msg = {"role": "user", "content": wrapped}

        ctx.messages = [*system_msgs, summary_msg, *recent_msgs]
        ctx.usage_meta["summarized"] = True
        ctx.usage_meta["tokens_elided"] = (
            ctx.usage_meta.get("tokens_elided", 0) + result.tokens_elided
        )
        return ctx


class DedupStage(Stage):
    """Intra-request elision pre-dispatch and pre-token-count (§6A.4).

    Runs before DispatchStage's provider call and before the dispatched
    token count is taken -- the phase-1 TokenBudgetStage reserve happens
    earlier on the raw pre-elision estimate, and MeterStage reconciles
    actuals after dispatch, so this stage's job is purely to shrink what
    actually gets sent (and metered) before either of those settle. Also
    feeds the §6.3 prefix-hash observation counters via `observe` so the
    response-cache branch's upstream prompt-cache orchestration can see
    which large blocks recur.
    """

    def __init__(
        self,
        name: str,
        dedup_store: DedupStore,
        token_len_cache: TokenLenCache,
        config_resolver: ConfigResolver,
        floor_tokens: int = 512,
        flag: str | None = None,
    ) -> None:
        """Wire the dedup store, tokenizer-length cache, and per-key config resolver."""
        super().__init__(name, flag)
        self.dedup_store = dedup_store
        self.token_len_cache = token_len_cache
        self.config_resolver = config_resolver
        self.floor_tokens = floor_tokens

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """Elide intra-request duplicate blocks in ctx.messages; observe stable blocks for §6.3."""
        config = await self.config_resolver(ctx.user)
        if not config.schema_dedup:
            return ctx

        org_id, user_id = _org_and_user(ctx.user)
        model = ctx.model or "gpt-4"

        if ctx.session_id:
            vkey_id = str(getattr(ctx.user, "api_key_id", None) or user_id or "unscoped")
            stable_blocks = []
            for msg in ctx.messages:
                content = msg.get("content")
                if not isinstance(content, str):
                    continue
                tokens = await self.token_len_cache.count(model, content, self._fallback_counter)
                if tokens >= self.floor_tokens:
                    stable_blocks.append(content)
            if stable_blocks:
                await self.dedup_store.observe(org_id, ctx.session_id, vkey_id, stable_blocks)

        tools = ctx.body.get("tools") if isinstance(ctx.body, dict) else None
        system = ctx.body.get("system") if isinstance(ctx.body, dict) else None

        (
            new_messages,
            _new_tools,
            _new_system,
            tokens_saved,
        ) = await self.dedup_store.elide_intra_request(
            ctx.messages,
            tools,
            system,
            model=model,
            token_len_cache=self.token_len_cache,
            floor_tokens=self.floor_tokens,
        )
        ctx.messages = new_messages
        ctx.usage_meta["tokens_saved"] = ctx.usage_meta.get("tokens_saved", 0) + tokens_saved
        return ctx

    @staticmethod
    async def _fallback_counter(text: str) -> int:
        return len(text) // 4
