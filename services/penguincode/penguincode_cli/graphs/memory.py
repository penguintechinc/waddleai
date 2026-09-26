"""Memory-graph extractor: LLM entity/relation triples over memory writes (T13).

`extract_memory_graph()` is the hook `ScopedMemoryManager.add()`
(`tools/memory.py`, T8) calls right after a memory write returns: it prompts
the configured Ollama **orchestration model** (`settings.models.orchestration`)
for `(subject, relation, object)` triples describing the memory's content,
turns them into `GraphNode`/`GraphEdge` pairs, and writes them via
`GraphStore` (T10) under `graph_kind="memory"`, scoped by the caller's
`ScopeContext`. This mirrors `graphs.knowledge`'s (T12) extraction pattern
exactly -- prompt/parse/build/write -- with three memory-specific changes
described below.

**Flag-gated:** `flags.client.is_enabled(MEMORY_GRAPH_FLAG, ctx)` is checked
first, before any other work -- off means no LLM call and no store write,
same contract as T12.

**LLM output is untrusted input**, same as T12: the model is asked for a
single JSON object (`{"triples": [...]}`) via Ollama's `format="json"` mode,
but a local model can still wrap it in prose or markdown fences, truncate
it, or emit a malformed/incomplete entry. `_parse_triples()` never raises: a
failed `json.loads` falls back to recovering a JSON object/array from
surrounding text, then every candidate entry is validated independently --
one malformed triple is skipped, not fatal to the batch. A total parse
failure yields zero triples (a no-op extraction), not an exception.

**No PII in logs/spans -- more so than T12's indexed docs.** Memory content
is inherently personal (that is the whole point of a memory), so only
counts (chars in, triples extracted/skipped, node/edge counts) are ever
logged or passed as span/metric attributes -- never the memory text, the raw
LLM response, or any extracted entity/relation value.

**Default `visibility="team"`** (unlike T12's `"tenant"` default for shared
docs): the memory layer is a shared team brain by product intent -- a
teammate's memory should be visible to the rest of their team (client
engagement) by default -- `ScopedMemoryManager.add()` itself defaults to
`visibility="team"` (see `tools/memory.py`), so the graph fragment
describing it inherits that same team-shared-by-default scope unless the
memory's own recorded scope stamp (`source_metadata`, see next) says
otherwise. As in `tools/memory.py`, defaulting to `"team"` with no explicit
`team_id` resolves the caller's own team from `ctx.team_ids`
(`_resolve_default_team_scope`): exactly one team resolves unambiguously,
more than one raises rather than guessing across client engagements, and
zero teams degrades to private `"user"` visibility.

**`source_metadata` carries T8's exact scope stamp and takes precedence
over the `visibility`/`team_id` keyword defaults.** The intended caller
(`ScopedMemoryManager.add()`) passes its own `scope_meta` -- T8's
`_scope_metadata()` output (`tenant_id`/`org_id`/`team_id`/`owner_user_id`/
`visibility`), computed once at write time -- directly as `source_metadata`,
so the extracted triples land in the *identical* tenant/org/team/user/
visibility bucket as the memory they came from, never a value independently
re-derived from `ctx` that could drift from what the memory itself was
actually stamped with (e.g. a caller passing an explicit `visibility="team"`
for the memory write but forgetting to also pass it here). This is deliberately
NOT read back out of mem0's own `add()` return envelope: mem0ai==2.2.0's real
`add(infer=False)` doesn't echo `metadata` in `result["results"][0]`, so doing
that would have silently fallen through to this function's own keyword
defaults for any non-default visibility, extracting into the wrong scope
bucket. `tenant_id`/`org_id`/`owner_user_id` are deliberately never read out
of `source_metadata`: `GraphStore.upsert_nodes`/`upsert_edges` always derive
those three from `ctx` directly (see `stores/graph.py`'s `_scope_columns`),
so untrusted caller-supplied metadata can never be used to widen `ctx`'s own
identity -- only the write-time `visibility`/`team_id` selection is
influenced by it.

**Ollama call failures degrade gracefully** (`httpx.HTTPError` -- connection
refused, timeout, non-2xx) into a skipped, empty extraction (logged, not
raised) -- memory-graph extraction is a best-effort enrichment layered on
top of a memory write, not a hard dependency that write should fail on.
`GraphStore` write failures are NOT caught here and propagate normally,
same as T12 and every other `GraphStore` consumer (T6/T10).
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
from penguincode_cli.flags.client import MEMORY_GRAPH_FLAG, is_enabled
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

#: Documented default per the module docstring -- a memory is team-shared by
#: default (the "we all learn" model) unless `source_metadata` (or an
#: explicit `visibility=`) says otherwise. Mirrors `tools.memory
#: .DEFAULT_VISIBILITY` -- kept as a separate constant (not imported) to
#: avoid a `tools.memory` <-> `graphs.memory` import cycle (`tools.memory`
#: already imports `extract_memory_graph` from this module).
DEFAULT_VISIBILITY = "team"

#: Fallback node type when the LLM omits/blanks `subject_type`/`object_type`.
_DEFAULT_NODE_TYPE = "entity"

#: Bound the prompt: memory notes are short personal statements by nature
#: (unlike T12's full indexed documents), but this is still a defensive cap
#: for a pathological caller passing an unusually large `content` -- keeps
#: the extraction call's cost and latency predictable.
_MAX_INPUT_CHARS = 4000

_EXTRACTION_SYSTEM_PROMPT = (
    "You are a memory-graph extraction engine. Read the user's personal memory note and "
    "extract factual (subject, relation, object) triples describing entities (people, "
    "places, organizations, preferences, projects, events) and the relationships between "
    "them. Respond with ONLY a JSON object of this exact shape, no other text, no markdown "
    'fences:\n{"triples": [{"subject": "...", "subject_type": "...", "relation": "...", '
    '"object": "...", "object_type": "..."}]}\n'
    '"subject_type"/"object_type" are short noun categories (e.g. "person", "place", '
    '"organization", "preference", "project", "event"). If the note contains no clear '
    'triples, respond with {"triples": []}.'
)


@dataclass(slots=True, frozen=True)
class _RawTriple:
    """One validated (subject, relation, object) triple parsed from the LLM response."""

    subject: str
    subject_type: str
    relation: str
    obj: str
    object_type: str


async def extract_memory_graph(
    ctx: ScopeContext,
    content: str,
    *,
    source_metadata: dict[str, Any] | None = None,
    visibility: str = DEFAULT_VISIBILITY,
    team_id: str | None = None,
    settings: Settings | None = None,
    ollama_client: OllamaClient | None = None,
    graph_store: GraphStore | None = None,
) -> Subgraph:
    """Extract a memory (sub)graph from `content` and write it via `GraphStore`.

    Called by `ScopedMemoryManager.add()` (T8) right after a memory write
    returns. Gated on `MEMORY_GRAPH_FLAG` -- when off, returns an empty
    `Subgraph` immediately with no LLM call and no write.
    `settings`/`ollama_client`/`graph_store` are optional seams for tests
    (and for callers that already hold a long-lived `OllamaClient`/
    `GraphStore`) -- production defaults are built from `Settings()` when
    omitted. See the module docstring for how `source_metadata` overrides
    the `visibility`/`team_id` write scope.
    """
    if not is_enabled(MEMORY_GRAPH_FLAG, ctx):
        logger.debug("memory-graph extraction skipped: %s is off", MEMORY_GRAPH_FLAG)
        return Subgraph(nodes=[], edges=[])

    if not content or not content.strip():
        return Subgraph(nodes=[], edges=[])

    write_visibility, write_team_id = _resolve_write_scope(
        ctx, source_metadata, visibility, team_id
    )

    cfg = settings or Settings()
    store = graph_store or create_graph_store(cfg.graph)
    truncated = content[:_MAX_INPUT_CHARS]

    try:
        with timed_store_operation(
            "extraction", "memory.extract", backend="ollama", graph_kind="memory"
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
                    "memory-graph extraction: skipped %d malformed triple(s) of %d parsed",
                    skipped,
                    skipped + len(triples),
                )
            logger.info(
                "memory-graph extraction: %d triple(s) from %d char(s) of input",
                len(triples),
                len(truncated),
            )

            subgraph = _build_subgraph(triples)
            if subgraph.nodes or subgraph.edges:
                await asyncio.to_thread(
                    store.upsert_nodes,
                    ctx,
                    "memory",
                    subgraph.nodes,
                    visibility=write_visibility,
                    team_id=write_team_id,
                )
                await asyncio.to_thread(
                    store.upsert_edges,
                    ctx,
                    "memory",
                    subgraph.edges,
                    visibility=write_visibility,
                    team_id=write_team_id,
                )
    except httpx.HTTPError as exc:
        logger.warning(
            "memory-graph extraction: Ollama call failed (%s); skipping this batch",
            type(exc).__name__,
        )
        return Subgraph(nodes=[], edges=[])

    return subgraph


def _resolve_default_team_scope(
    ctx: ScopeContext, visibility: str, team_id: str | None
) -> tuple[str, str | None]:
    """Derive an effective `team_id` for a `"team"`-visibility write with none given.

    Mirrors `tools.memory._resolve_default_team_scope` exactly (duplicated
    rather than imported -- see `DEFAULT_VISIBILITY`'s comment on the import
    cycle). Only engages when `visibility == "team"` and no `team_id` was
    already supplied. A caller on exactly one team resolves to it
    unambiguously; a caller on multiple teams is never silently guessed into
    one (would risk leaking a memory-graph fragment across client
    engagements) -- this raises `ValueError`, requiring an explicit
    `team_id`. A caller on no team at all can't share to a team that doesn't
    exist, so this degrades to private `"user"` visibility (logged at
    DEBUG).
    """
    if visibility != "team" or team_id is not None:
        return visibility, team_id

    if len(ctx.team_ids) == 1:
        return "team", ctx.team_ids[0]

    if len(ctx.team_ids) > 1:
        raise ValueError(
            "cannot default to 'team' visibility: caller belongs to multiple teams "
            f"{ctx.team_ids!r} -- pass an explicit team_id"
        )

    logger.debug(
        "graphs.memory: defaulting 'team'-visibility write to 'user' -- caller has no team_ids"
    )
    return "user", None


def _resolve_write_scope(
    ctx: ScopeContext,
    source_metadata: dict[str, Any] | None,
    visibility: str,
    team_id: str | None,
) -> tuple[str, str | None]:
    """Prefer T8's exact scope stamp over the `visibility`/`team_id` keyword defaults.

    See the module docstring's `source_metadata` section. `source_metadata`
    is T8's own `_scope_metadata()` output; when its `visibility` key is a
    non-empty string, it and the paired `team_id` (legitimately `None` for
    `tenant`/`user` visibility) replace the keyword defaults wholesale --
    partial overrides aren't meaningful here since the two travel together
    as one scope stamp. `tenant_id`/`org_id`/`owner_user_id` are
    deliberately never read from here -- see module docstring. When no
    usable `source_metadata` is present, the keyword `visibility`/`team_id`
    go through `_resolve_default_team_scope` so a defaulted `"team"` write
    (the common case: a direct `extract_memory_graph()` call with no
    `team_id`) resolves the caller's own team before `GraphStore` sees it.
    """
    if source_metadata:
        meta_visibility = source_metadata.get("visibility")
        if isinstance(meta_visibility, str) and meta_visibility:
            return meta_visibility, source_metadata.get("team_id")
    return _resolve_default_team_scope(ctx, visibility, team_id)


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

    Identical strategy to `graphs.knowledge._extract_json_block` (T12) --
    handles a leading/trailing sentence or a ```json ... ``` fence, tracking
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


def _build_subgraph(triples: list[_RawTriple]) -> Subgraph:
    """Turn validated triples into deduplicated `GraphNode`s + one `GraphEdge` each.

    Nodes are deduplicated on `(node_type, key)` -- the same tuple T1's
    schema uniquely constrains a node on, so two triples naming the same
    `(type, entity)` pair collapse to one node. Unlike T12, there is no
    `source_id`-equivalent to stamp into `props` here: `source_metadata`
    already carries the memory's scope stamp (consumed by
    `_resolve_write_scope`, not stored in node `props`), and a raw memory
    write has no natural document/chunk id to attribute back to.
    """
    seen: dict[tuple[str, str], GraphNode] = {}
    edges: list[GraphEdge] = []
    for t in triples:
        seen.setdefault((t.subject_type, t.subject), GraphNode(t.subject_type, t.subject, {}))
        seen.setdefault((t.object_type, t.obj), GraphNode(t.object_type, t.obj, {}))
        edges.append(
            GraphEdge(
                src_type=t.subject_type,
                src_key=t.subject,
                dst_type=t.object_type,
                dst_key=t.obj,
                rel_type=t.relation,
                props={},
            )
        )
    return Subgraph(nodes=list(seen.values()), edges=edges)


__all__ = ["DEFAULT_VISIBILITY", "extract_memory_graph"]
