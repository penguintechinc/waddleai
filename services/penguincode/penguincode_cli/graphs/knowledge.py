"""Knowledge-graph extractor: LLM entity/relation triples over indexed docs (T12).

`extract_knowledge()` is the hook the docs-indexing flow (`docs_rag/indexer.py`,
T7) calls once per indexed chunk/document: it prompts the configured Ollama
**orchestration model** (`settings.models.orchestration`) for `(subject,
relation, object)` triples describing the text, turns them into
`GraphNode`/`GraphEdge` pairs, and writes them via `GraphStore` (T10) under
`graph_kind="knowledge"`, scoped by the caller's `ScopeContext`.

**Flag-gated:** `flags.client.is_enabled(KNOWLEDGE_GRAPH_FLAG, ctx)` is
checked first, before any other work -- off means no LLM call and no store
write, not just a skipped write.

**LLM output is untrusted input.** The model is asked for a single JSON
object (`{"triples": [...]}`) via Ollama's `format="json"` mode, but a local
model can still wrap it in prose or markdown fences, truncate it, or emit a
malformed/incomplete entry. `_parse_triples()` never raises: it recovers a
JSON object/array from surrounding text when a bare `json.loads` fails, then
validates every entry independently -- one malformed triple is skipped, not
fatal to the batch. A total parse failure yields zero triples (a no-op
extraction), not an exception.

**No PII in logs/spans.** Only counts (chars in, triples extracted/skipped,
node/edge counts) are logged or passed as span/metric attributes -- never the
input text, the raw LLM response, or any extracted entity/relation value,
since those are derived directly from arbitrary indexed document content and
may coincidentally contain PII (e.g. a person's name as an extracted entity).

**Default `visibility="tenant"`:** indexed documentation is treated as a
shared reference corpus for the whole tenant unless the caller narrows it
explicitly (e.g. a private team runbook) by passing `visibility="team"` +
`team_id`. This mirrors docs-RAG's own default scope for indexed content.

**Ollama call failures degrade gracefully** (`httpx.HTTPError` -- connection
refused, timeout, non-2xx) into a skipped, empty extraction (logged, not
raised) -- knowledge-graph extraction is a best-effort enrichment layered on
top of docs indexing, not a hard dependency the indexing flow should fail on.
`GraphStore` write failures are NOT caught here and propagate normally, same
as every other `GraphStore` consumer (T6/T10) -- a failed write is a real
data-integrity problem the caller should see, unlike a flaky local LLM call.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import Settings
from penguincode_cli.flags.client import KNOWLEDGE_GRAPH_FLAG, is_enabled
from penguincode_cli.observability.otel import timed_store_operation
from penguincode_cli.ollama.client import OllamaClient
from penguincode_cli.ollama.types import Message
from penguincode_cli.stores.graph import (
    GraphEdge,
    GraphNode,
    GraphStore,
    Subgraph,
    create_graph_store,
)

logger = logging.getLogger(__name__)

#: Documented default per the module docstring -- indexed docs are a shared
#: tenant-wide corpus unless the caller opts into a narrower scope.
DEFAULT_VISIBILITY = "tenant"

#: Fallback node type when the LLM omits/blanks `subject_type`/`object_type`.
_DEFAULT_NODE_TYPE = "entity"

#: Bound the prompt: a defensive cap for callers passing large documents
#: directly (docs-RAG's own chunking already keeps typical inputs well under
#: this) -- keeps the extraction call's cost and latency predictable.
_MAX_INPUT_CHARS = 8000

_EXTRACTION_SYSTEM_PROMPT = (
    "You are a knowledge-graph extraction engine. Read the user's text and extract "
    "factual (subject, relation, object) triples describing entities and the "
    "relationships between them. Respond with ONLY a JSON object of this exact shape, "
    "no other text, no markdown fences:\n"
    '{"triples": [{"subject": "...", "subject_type": "...", "relation": "...", '
    '"object": "...", "object_type": "..."}]}\n'
    '"subject_type"/"object_type" are short noun categories (e.g. "concept", '
    '"person", "organization", "technology", "component"). If the text contains no '
    'clear triples, respond with {"triples": []}.'
)


@dataclass(slots=True, frozen=True)
class _RawTriple:
    """One validated (subject, relation, object) triple parsed from the LLM response."""

    subject: str
    subject_type: str
    relation: str
    obj: str
    object_type: str


async def extract_knowledge(
    ctx: ScopeContext,
    text: str,
    *,
    source_id: str | None = None,
    visibility: str = DEFAULT_VISIBILITY,
    team_id: str | None = None,
    settings: Settings | None = None,
    ollama_client: OllamaClient | None = None,
    graph_store: GraphStore | None = None,
) -> Subgraph:
    """Extract a knowledge (sub)graph from `text` and write it via `GraphStore`.

    Called by the docs-indexing flow once per indexed chunk/document. Gated
    on `KNOWLEDGE_GRAPH_FLAG` -- when off, returns an empty `Subgraph`
    immediately with no LLM call and no write. `settings`/`ollama_client`/
    `graph_store` are optional seams for tests (and for callers that already
    hold a long-lived `OllamaClient`/`GraphStore` and want to reuse it rather
    than pay per-call construction cost) -- production defaults are built
    from `Settings()` when omitted. `source_id` (e.g. a doc chunk id), when
    given, is stamped into each extracted node's `props` so a later cleanup
    pass can trace a node back to the document that produced it.
    """
    if not is_enabled(KNOWLEDGE_GRAPH_FLAG, ctx):
        logger.debug("knowledge-graph extraction skipped: %s is off", KNOWLEDGE_GRAPH_FLAG)
        return Subgraph(nodes=[], edges=[])

    if not text or not text.strip():
        return Subgraph(nodes=[], edges=[])

    cfg = settings or Settings()
    store = graph_store or create_graph_store(cfg.graph)
    truncated = text[:_MAX_INPUT_CHARS]

    try:
        with timed_store_operation(
            "extraction", "knowledge.extract", backend="ollama", graph_kind="knowledge"
        ):
            if ollama_client is not None:
                raw_response = await _call_llm(ollama_client, cfg.models.orchestration, truncated)
            else:
                async with OllamaClient(
                    base_url=cfg.ollama.api_url, timeout=cfg.ollama.timeout
                ) as client:
                    raw_response = await _call_llm(client, cfg.models.orchestration, truncated)

            triples, skipped = _parse_triples(raw_response)
            if skipped:
                logger.warning(
                    "knowledge-graph extraction: skipped %d malformed triple(s) of %d parsed",
                    skipped,
                    skipped + len(triples),
                )
            logger.info(
                "knowledge-graph extraction: %d triple(s) from %d char(s) of input",
                len(triples),
                len(truncated),
            )

            subgraph = _build_subgraph(triples, source_id=source_id)
            if subgraph.nodes or subgraph.edges:
                await asyncio.to_thread(
                    store.upsert_nodes,
                    ctx,
                    "knowledge",
                    subgraph.nodes,
                    visibility=visibility,
                    team_id=team_id,
                )
                await asyncio.to_thread(
                    store.upsert_edges,
                    ctx,
                    "knowledge",
                    subgraph.edges,
                    visibility=visibility,
                    team_id=team_id,
                )
    except httpx.HTTPError as exc:
        logger.warning(
            "knowledge-graph extraction: Ollama call failed (%s); skipping this batch",
            type(exc).__name__,
        )
        return Subgraph(nodes=[], edges=[])

    return subgraph


async def _call_llm(client: OllamaClient, model: str, text: str) -> str:
    """Prompt `model` for JSON triples over `text`, accumulating the streamed response."""
    messages = [
        Message(role="system", content=_EXTRACTION_SYSTEM_PROMPT),
        Message(role="user", content=text),
    ]
    response_text = ""
    async for chunk in client.chat(model=model, messages=messages, stream=True, format="json"):
        if chunk.message and chunk.message.content:
            response_text += chunk.message.content
    return response_text


def _parse_triples(raw: str) -> tuple[list[_RawTriple], int]:
    """Parse the LLM's JSON triples response into validated `_RawTriple`s.

    Never raises -- see module docstring's "LLM output is untrusted input"
    section. Returns `(valid_triples, skipped_count)`.
    """
    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        block = _extract_json_block(raw)
        if block is not None:
            try:
                parsed = json.loads(block)
            except (json.JSONDecodeError, ValueError):
                parsed = None

    if isinstance(parsed, dict):
        candidates = parsed.get("triples", [])
    elif isinstance(parsed, list):
        candidates = parsed
    else:
        candidates = []

    if not isinstance(candidates, list):
        candidates = []

    triples: list[_RawTriple] = []
    skipped = 0
    for item in candidates:
        triple = _coerce_triple(item)
        if triple is None:
            skipped += 1
            continue
        triples.append(triple)
    return triples, skipped


def _extract_json_block(raw: str) -> str | None:
    """Best-effort recovery of one JSON object/array from non-pure-JSON text.

    Handles the common local-model failure modes: a leading/trailing
    sentence, or the whole thing wrapped in a ```json ... ``` fence. Tracks
    whether it is inside a JSON string (respecting `\\"` escapes) so braces
    inside string values don't throw off the bracket count.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text[:4].lower() == "json":
            text = text[4:].lstrip()

    start_idx = next((i for i, c in enumerate(text) if c in "{["), None)
    if start_idx is None:
        return None
    open_char = text[start_idx]
    close_char = "}" if open_char == "{" else "]"

    depth = 0
    in_string = False
    escape = False
    for i in range(start_idx, len(text)):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == open_char:
            depth += 1
        elif c == close_char:
            depth -= 1
            if depth == 0:
                return text[start_idx : i + 1]
    return None


def _coerce_triple(item: Any) -> _RawTriple | None:
    """Validate one candidate triple dict; `None` means "skip, don't crash"."""
    if not isinstance(item, dict):
        return None
    subject = _clean_str(item.get("subject"))
    relation = _clean_str(item.get("relation"))
    obj = _clean_str(item.get("object"))
    if not subject or not relation or not obj:
        return None
    subject_type = _clean_str(item.get("subject_type")) or _DEFAULT_NODE_TYPE
    object_type = _clean_str(item.get("object_type")) or _DEFAULT_NODE_TYPE
    return _RawTriple(
        subject=subject,
        subject_type=subject_type,
        relation=_normalize_rel_type(relation),
        obj=obj,
        object_type=object_type,
    )


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalize_rel_type(relation: str) -> str:
    """Collapse whitespace to underscores and lowercase.

    Keeps `rel_type` one consistent form regardless of how the LLM
    capitalized/spaced a relation phrase, so a later consumer filtering on
    `rel_types` (e.g. T14 GraphRAG expansion) sees a stable vocabulary.
    """
    return "_".join(relation.split()).lower()


def _build_subgraph(triples: list[_RawTriple], *, source_id: str | None) -> Subgraph:
    """Turn validated triples into deduplicated `GraphNode`s + one `GraphEdge` each.

    Nodes are deduplicated on `(node_type, key)` -- the same tuple T1's
    schema uniquely constrains a node on, so two triples naming the same
    `(type, entity)` pair collapse to one node.
    """
    props: dict[str, Any] = {"source_id": source_id} if source_id else {}
    seen: dict[tuple[str, str], GraphNode] = {}
    edges: list[GraphEdge] = []
    for t in triples:
        seen.setdefault(
            (t.subject_type, t.subject), GraphNode(t.subject_type, t.subject, dict(props))
        )
        seen.setdefault((t.object_type, t.obj), GraphNode(t.object_type, t.obj, dict(props)))
        edges.append(
            GraphEdge(
                src_type=t.subject_type,
                src_key=t.subject,
                dst_type=t.object_type,
                dst_key=t.obj,
                rel_type=t.relation,
                props=dict(props),
            )
        )
    return Subgraph(nodes=list(seen.values()), edges=edges)


__all__ = ["DEFAULT_VISIBILITY", "extract_knowledge"]
