"""Context injection for documentation RAG.

Queries the documentation index and formats results for
injection into agent prompts. Only injects relevant docs
for libraries actually used in the project.

**Hybrid retrieval (T-wire).** ``get_relevant_context`` is layered on top of
``retrieval.graphrag.retrieve`` (T14) rather than ``DocumentationIndexer.search``
directly: scoped vector top-k plus scoped, flag-gated graph expansion in one
call. ``retrieve`` already self-gates on ``penguincode.rag`` (and each graph
kind on its own flag) and never raises, so this module adds only its own
project-library/language client-side filter (``retrieve`` has no ``where=``
filter, unlike ``PgVectorStore.query`` directly) plus a broad
``try/except`` around the call for defense in depth -- a retrieval outage
degrades to no injected context, never a crash on the chat turn it would
otherwise augment.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.retrieval.graphrag import RetrievalResult
from penguincode_cli.retrieval.graphrag import retrieve as graphrag_retrieve
from penguincode_cli.stores.graph import Subgraph
from penguincode_cli.stores.vector import TableName

from .indexer import DocumentationIndexer
from .models import DocSearchResult, ProjectContext

logger = logging.getLogger(__name__)

#: Test/production seam matching ``retrieval.graphrag.retrieve``'s signature.
RetrieveFn = Callable[..., Awaitable[RetrievalResult]]

#: Docs-RAG only ever wants documentation content, not memory content --
#: unlike ``retrieve``'s own default of both ``docs_vectors`` and
#: ``memory_vectors``, scoped here to preserve this class's documented
#: "only injects relevant docs" contract.
_DOC_VECTOR_TABLES: tuple[TableName, ...] = ("docs_vectors",)


class ContextInjector:
    """Injects relevant documentation context (vector + scoped graph expansion) into prompts."""

    def __init__(
        self,
        indexer: DocumentationIndexer,
        max_context_tokens: int = 2000,
        max_chunks: int = 5,
        *,
        retrieve_fn: RetrieveFn | None = None,
        vector_tables: tuple[TableName, ...] = _DOC_VECTOR_TABLES,
    ):
        self.indexer = indexer
        self.max_tokens = max_context_tokens
        self.max_chunks = max_chunks
        #: Defaults to the real ``graphrag.retrieve``; overridable in tests
        #: (and by a caller wanting a different vector/graph store wiring)
        #: without needing a live Ollama/Postgres.
        self._retrieve: RetrieveFn = retrieve_fn or graphrag_retrieve
        self._vector_tables = vector_tables

        # Approximate tokens per character
        self.chars_per_token = 4

    async def get_relevant_context(
        self,
        ctx: ScopeContext | None,
        query: str,
        project_context: ProjectContext,
    ) -> str:
        """
        Get relevant documentation context for a query via hybrid GraphRAG retrieval.

        Only injects content for libraries/languages detected in the
        project, preventing injection of irrelevant documentation --
        filtered client-side against ``retrieve``'s vector hits, since
        ``retrieve`` (unlike ``DocumentationIndexer.search``) has no
        ``where=`` filter of its own.

        Args:
            ctx: Caller's scope. ``retrieval.graphrag.retrieve`` requires a
                real ``ScopeContext`` (unlike ``DocumentationIndexer.search``,
                which tolerates ``None``) -- ``ctx=None`` (the interactive
                CLI REPL has no auth flow yet; see ``core/repl.py``'s
                ``REPLSession.scope_ctx`` injection point) degrades to no
                context here, never a crash or a fabricated scope.
            query: User query to find relevant docs for
            project_context: Detected project languages and libraries

        Returns:
            Formatted context string (vector hits, then a graph-expansion
            section for each on-flag graph kind) for prompt injection.
        """
        if not project_context.libraries and not project_context.languages:
            return ""

        if ctx is None:
            logger.info("docs_rag: no ScopeContext supplied -- hybrid context injection skipped")
            return ""

        library_names = {name.lower() for name in project_context.library_names}
        language_names = {name.lower() for name in project_context.language_names}

        try:
            result = await self._retrieve(
                ctx, query, n_vector=self.max_chunks, vector_tables=self._vector_tables
            )
        except Exception as exc:  # noqa: BLE001 -- retrieval outage degrades to no context, never crash
            logger.warning("docs_rag: hybrid context retrieval failed: %s", exc)
            return ""

        hits = [
            hit
            for hit in result.vector_hits
            if not (library_names or language_names)
            or str(hit.metadata.get("library", "")).lower() in library_names
            or str(hit.metadata.get("language", "")).lower() in language_names
        ]

        if not hits and not result.subgraphs:
            return ""

        doc_results = [
            DocSearchResult(
                content=hit.document,
                library=hit.metadata.get("library", ""),
                section=hit.metadata.get("section", ""),
                relevance_score=hit.score,
                url=hit.metadata.get("url", ""),
                language=hit.metadata.get("language", ""),
            )
            for hit in hits
        ]

        context = self.format_context(doc_results) if doc_results else ""
        graph_section = self._format_graph_section(result.subgraphs)
        if graph_section:
            context = f"{context}\n{graph_section}" if context else graph_section
        return context

    def _format_graph_section(self, subgraphs: dict[str, Subgraph]) -> str:
        """Render each on-flag graph kind's expansion as short factual lines.

        Mirrors ``retrieval.graphrag._assemble_context``'s edge-line format.
        A graph kind absent from ``subgraphs`` means its flag was off for
        this call (see ``RetrievalResult``'s docstring) and simply
        contributes nothing here -- never an empty/placeholder section.
        """
        lines: list[str] = []
        for kind, subgraph in subgraphs.items():
            for edge in subgraph.edges:
                lines.append(
                    f"- ({kind}) {edge.src_type}:{edge.src_key} "
                    f"--{edge.rel_type}--> {edge.dst_type}:{edge.dst_key}"
                )
        if not lines:
            return ""
        return "## Related Knowledge Graph\n\n" + "\n".join(lines) + "\n"

    def format_context(self, results: list[DocSearchResult]) -> str:
        """
        Format search results for prompt injection.

        Respects token limits and formats for readability.

        Args:
            results: Search results to format

        Returns:
            Formatted context string
        """
        if not results:
            return ""

        lines = ["## Relevant Documentation\n"]
        current_chars = len(lines[0])
        max_chars = self.max_tokens * self.chars_per_token

        for result in results:
            # Build result block
            header = f"### {result.library}"
            if result.section:
                header += f" - {result.section}"
            header += f" (relevance: {result.relevance_score:.2f})\n"

            content = result.content.strip()

            # Truncate content if needed
            available = max_chars - current_chars - len(header) - 50
            if len(content) > available:
                content = content[:available] + "..."

            block = f"{header}\n{content}\n\n"

            # Check if we'd exceed limit
            if current_chars + len(block) > max_chars:
                break

            lines.append(block)
            current_chars += len(block)

        return "".join(lines)

    async def should_inject_context(
        self,
        query: str,
        project_context: ProjectContext,
    ) -> bool:
        """
        Determine if documentation context should be injected.

        Returns False for simple queries that don't need docs.

        Args:
            query: User query
            project_context: Project context

        Returns:
            True if context should be injected
        """
        # Skip if no project context
        if not project_context.libraries:
            return False

        # Skip for very short queries
        if len(query.split()) < 3:
            return False

        # Skip for greetings and meta queries
        skip_patterns = [
            "hello",
            "hi ",
            "hey ",
            "thanks",
            "bye",
            "help",
            "what can you",
            "/",  # Commands
        ]
        query_lower = query.lower()
        if any(p in query_lower for p in skip_patterns):
            return False

        # Inject for code-related queries
        code_patterns = [
            "how to",
            "how do i",
            "what is",
            "explain",
            "error",
            "bug",
            "fix",
            "implement",
            "example",
            "usage",
            "api",
            "function",
            "class",
            "method",
            "import",
            "install",
        ]
        if any(p in query_lower for p in code_patterns):
            return True

        # Inject if query mentions a project library
        return any(lib.lower() in query_lower for lib in project_context.library_names)

    def build_augmented_prompt(
        self,
        original_prompt: str,
        context: str,
    ) -> str:
        """
        Build an augmented prompt with documentation context.

        Args:
            original_prompt: Original system prompt
            context: Documentation context to inject

        Returns:
            Augmented prompt
        """
        if not context:
            return original_prompt

        return f"""{original_prompt}

{context}

Use the above documentation context to help answer questions accurately.
Cite specific documentation when relevant.
"""
