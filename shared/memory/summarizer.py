"""Rolling conversation summarizer (§6A.2).

Threshold-triggered distillation of turns older than the last `keep_recent`
via the §7.1 `summarize` model-assignment seam (cheap local default).
Summaries persist versioned in `conversation_summaries`; a repeat turn
whose existing summary still covers the current coverage point reuses it
(no model call). Generated summaries longer than `ratio * original_tokens`
are rejected -- fall back to un-summarized injection, never inject a
bloated summary. Originals are never mutated or removed: only the
*injected* dispatch view is compacted (see SummarizationStage); a
conversation's full history stays retrievable from the memory store.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from dataclasses import dataclass
from typing import Any

from shared.memory.config import ProxyMemoryConfig
from shared.memory.provenance import filter_on_write
from shared.memory.token_len_cache import TokenLenCache

logger = logging.getLogger(__name__)

# §7.1 seam default: Gemma 4 (Apache-2.0). gemma4:2b is not a pullable tag --
# only e2b/e4b/12b/26b/31b exist; e2b is the cheapest.
DEFAULT_SUMMARIZE_MODEL = "gemma4:e2b"

_SUMMARIZE_SYSTEM_PROMPT = (
    "Summarize the following conversation turns concisely, preserving key "
    "facts, decisions, and open questions. Output only the summary text, "
    "no preamble."
)


@dataclass(slots=True)
class SummarizationResult:
    """Outcome of ConversationSummarizer.maybe_summarize."""

    applied: bool
    summary: str | None
    covers_through_turn: int
    tokens_elided: int


def resolve_summarize_model() -> str:
    """§7.1 model-assignment seam for the `summarize` role.

    # TODO(§7.1): replace with a model_assignments lookup when
    # feature/smart-routing lands (migration 010 introduces
    # model_assignments; until then this env-var-or-default is the entire
    # resolution path, and it is the single call site of this seam).
    """
    return os.getenv("SUMMARIZE_MODEL", DEFAULT_SUMMARIZE_MODEL)


def _empty_result() -> SummarizationResult:
    return SummarizationResult(applied=False, summary=None, covers_through_turn=0, tokens_elided=0)


def _build_summarize_messages(older: list[dict]) -> list[dict]:
    transcript = "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in older)
    return [
        {"role": "system", "content": _SUMMARIZE_SYSTEM_PROMPT},
        {"role": "user", "content": transcript},
    ]


class ConversationSummarizer:
    """Threshold-triggered, versioned, injection-safe conversation summarizer."""

    def __init__(
        self,
        db: Any,
        llm_manager: Any,
        token_len_cache: TokenLenCache,
        scanner: Any,
        content_filter: Any,
    ) -> None:
        """Wire the db, llm_manager (token counts + dispatch), token cache, and security tiers."""
        self.db = db
        self.llm_manager = llm_manager
        self.token_len_cache = token_len_cache
        self.scanner = scanner
        self.content_filter = content_filter

    async def maybe_summarize(
        self,
        org_id: int,
        user_id: int,
        conversation_id: str | None,
        messages: list[dict],
        cfg: ProxyMemoryConfig,
        model: str,
    ) -> SummarizationResult:
        """Summarize turns older than `cfg.keep_recent` once `cfg.threshold_tokens` is crossed."""
        if not cfg.summarization_enabled or not conversation_id:
            return _empty_result()

        total_tokens = await self._count_messages(model, messages)
        if total_tokens < cfg.threshold_tokens:
            return _empty_result()

        keep_recent = max(0, min(cfg.keep_recent, len(messages)))
        covers_through_turn = len(messages) - keep_recent
        older = messages[:covers_through_turn]
        if not older:
            return _empty_result()

        existing = await asyncio.to_thread(self._select_latest_summary, org_id, conversation_id)
        if (
            existing is not None
            and existing["status"] == "active"
            and existing["covers_through_turn"] >= covers_through_turn
        ):
            tokens_elided = await self._elision_savings(model, older, existing["summary"])
            return SummarizationResult(
                applied=True,
                summary=existing["summary"],
                covers_through_turn=existing["covers_through_turn"],
                tokens_elided=tokens_elided,
            )

        summarize_model = resolve_summarize_model()
        older_tokens = await self._count_messages(model, older)

        try:
            summary_text, _usage = await self._dispatch_summarize(summarize_model, older)
        except Exception as exc:
            logger.warning(
                "ConversationSummarizer: summarize dispatch failed, using originals: %s", exc
            )
            return _empty_result()

        summary_tokens = await self._count_text(model, summary_text)
        if older_tokens > 0 and summary_tokens > cfg.ratio * older_tokens:
            logger.warning(
                "ConversationSummarizer: summary (%d tok) exceeds ratio guardrail "
                "(%.2f * %d), using originals",
                summary_tokens,
                cfg.ratio,
                older_tokens,
            )
            return _empty_result()

        verdict = await filter_on_write(
            summary_text,
            scanner=self.scanner,
            content_filter=self.content_filter,
            user_id=user_id,
            org_id=org_id,
        )
        if verdict.quarantine:
            logger.warning("ConversationSummarizer: generated summary quarantined, using originals")
            return _empty_result()

        version = (existing["version"] + 1) if existing else 1
        new_id = await asyncio.to_thread(
            self._insert_summary,
            org_id,
            conversation_id,
            verdict.filtered_text,
            covers_through_turn,
            older_tokens,
            summarize_model,
            version,
            user_id,
        )
        if existing is not None:
            await asyncio.to_thread(self._mark_superseded, existing["id"], new_id)

        tokens_elided = max(0, older_tokens - summary_tokens)
        return SummarizationResult(
            applied=True,
            summary=verdict.filtered_text,
            covers_through_turn=covers_through_turn,
            tokens_elided=tokens_elided,
        )

    # ------------------------------------------------------------------
    # Token counting (delegates to TokenLenCache + the resolved connector)
    # ------------------------------------------------------------------

    async def _count_text(self, model: str, text: str) -> int:
        connector = self.llm_manager.get_connector_for_model(model) if self.llm_manager else None
        counter = functools.partial(self._invoke_counter, connector, model)
        return await self.token_len_cache.count(model, text, counter)

    @staticmethod
    async def _invoke_counter(connector: Any, model: str, text: str) -> int:
        if connector is None:
            return len(text) // 4  # matches OpenAIConnector's own tiktoken-failure fallback
        return await connector.count_tokens(text, model)

    async def _count_messages(self, model: str, messages: list[dict]) -> int:
        total = 0
        for msg in messages:
            total += await self._count_text(model, msg.get("content", "") or "")
        return total

    async def _elision_savings(self, model: str, older: list[dict], summary: str) -> int:
        older_tokens = await self._count_messages(model, older)
        summary_tokens = await self._count_text(model, summary)
        return max(0, older_tokens - summary_tokens)

    # ------------------------------------------------------------------
    # Dispatch to the summarize-role model
    # ------------------------------------------------------------------

    async def _dispatch_summarize(
        self, summarize_model: str, older: list[dict]
    ) -> tuple[str, dict]:
        connector = (
            self.llm_manager.get_connector_for_model(summarize_model) if self.llm_manager else None
        )
        if connector is None:
            raise RuntimeError(f"no connector available for summarize model {summarize_model!r}")
        messages = _build_summarize_messages(older)
        return await connector.chat_completion(messages, model=summarize_model)

    # ------------------------------------------------------------------
    # Postgres access (raw SQL via the injected db handle, matching
    # ScratchpadStore's convention). Wrapped in asyncio.to_thread (§3.5).
    # ------------------------------------------------------------------

    def _select_latest_summary(self, org_id: int, conversation_id: str) -> dict | None:
        rows = self.db.executesql(
            "SELECT id, summary, covers_through_turn, version, status FROM conversation_summaries "
            "WHERE org_id = %s AND conversation_id = %s ORDER BY version DESC LIMIT 1",
            (org_id, conversation_id),
        )
        if not rows:
            return None
        row_id, summary, covers_through_turn, version, status = rows[0]
        return {
            "id": row_id,
            "summary": summary,
            "covers_through_turn": covers_through_turn,
            "version": version,
            "status": status,
        }

    def _insert_summary(
        self,
        org_id: int,
        conversation_id: str,
        summary: str,
        covers_through_turn: int,
        tokens_summarized: int,
        model_used: str,
        version: int,
        author_user_id: int,
    ) -> int:
        rows = self.db.executesql(
            "INSERT INTO conversation_summaries "
            "(conversation_id, org_id, summary, covers_through_turn, tokens_summarized, "
            "model_used, scope_type, author_user_id, trust_tier, version, status, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'session', %s, 'unverified', %s, 'active', now()) "
            "RETURNING id",
            (
                org_id,
                conversation_id,
                summary,
                covers_through_turn,
                tokens_summarized,
                model_used,
                author_user_id,
                version,
            ),
        )
        return rows[0][0]

    def _mark_superseded(self, old_id: int, new_id: int) -> None:
        self.db.executesql(
            "UPDATE conversation_summaries SET status = 'superseded', "
            "superseded_by = %s WHERE id = %s",
            (new_id, old_id),
        )
