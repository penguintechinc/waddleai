"""Context injection for documentation RAG.

Queries the documentation index and formats results for
injection into agent prompts. Only injects relevant docs
for libraries actually used in the project.
"""


from .indexer import DocumentationIndexer
from .models import DocSearchResult, ProjectContext


class ContextInjector:
    """Injects relevant documentation context into prompts."""

    def __init__(
        self,
        indexer: DocumentationIndexer,
        max_context_tokens: int = 2000,
        max_chunks: int = 5,
    ):
        self.indexer = indexer
        self.max_tokens = max_context_tokens
        self.max_chunks = max_chunks

        # Approximate tokens per character
        self.chars_per_token = 4

    async def get_relevant_context(
        self,
        query: str,
        project_context: ProjectContext,
    ) -> str:
        """
        Get relevant documentation context for a query.

        Only searches docs for libraries detected in the project,
        preventing injection of irrelevant documentation.

        Args:
            query: User query to find relevant docs for
            project_context: Detected project languages and libraries

        Returns:
            Formatted context string for prompt injection
        """
        if not project_context.libraries and not project_context.languages:
            return ""

        # Filter to only project libraries
        library_names = project_context.library_names
        language_names = project_context.language_names

        # Search with filters
        results = await self.indexer.search(
            query=query,
            libraries=library_names if library_names else None,
            languages=language_names if language_names else None,
            limit=self.max_chunks,
        )

        if not results:
            return ""

        return self.format_context(results)

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
            "hello", "hi ", "hey ", "thanks", "bye",
            "help", "what can you",
            "/",  # Commands
        ]
        query_lower = query.lower()
        if any(p in query_lower for p in skip_patterns):
            return False

        # Inject for code-related queries
        code_patterns = [
            "how to", "how do i", "what is", "explain",
            "error", "bug", "fix", "implement",
            "example", "usage", "api", "function",
            "class", "method", "import", "install",
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
