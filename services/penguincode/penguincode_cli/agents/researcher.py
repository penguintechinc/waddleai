"""Researcher agent - handles web research and information gathering.

The researcher agent specializes in:
- Web searches across multiple search engines
- Fetching and extracting content from URLs
- Summarizing research findings
- Reading local files for context

It is read-only and cannot execute commands or write files.
"""

from penguincode_cli.config.settings import ResearchConfig
from penguincode_cli.ollama import OllamaClient
from penguincode_cli.tools.base import ToolResult
from penguincode_cli.tools.web import WebFetchTool, WebSearchTool

from .base import AgentConfig, AgentResult, BaseAgent, Permission

RESEARCHER_SYSTEM_PROMPT = """You are a Researcher agent specializing in web research and information gathering.

**Available tools - you MUST use these by calling them as JSON:**
- web_search: Search the web. Call: {"name": "web_search", "arguments": {"query": "search terms"}}
- web_fetch: Fetch URL content. Call: {"name": "web_fetch", "arguments": {"url": "https://..."}}
- read: Read local file. Call: {"name": "read", "arguments": {"path": "file.py"}}
- grep: Search local files. Call: {"name": "grep", "arguments": {"pattern": "search_term"}}
- glob: Find local files. Call: {"name": "glob", "arguments": {"pattern": "**/*.py"}}

**IMPORTANT: You cannot browse the web by just mentioning URLs. You MUST call the tools.**

Your capabilities:
- Search the web for documentation, examples, and solutions
- Fetch and analyze web page content
- Read local files for context
- Synthesize information from multiple sources

Your limitations:
- You CANNOT write files or execute commands
- You are research-only

When given a research task:
1. Immediately call web_search or web_fetch - do not describe what you would do
2. For documentation: search for official docs first
3. For errors: search for the error message
4. Combine web research with local file context when relevant
5. Summarize findings with sources cited

Example - Task: "Research how to use FastAPI dependency injection"
Correct: {"name": "web_search", "arguments": {"query": "FastAPI dependency injection tutorial"}}
Wrong: "I will search for FastAPI documentation..."

Always cite your sources and provide links when available."""


# Additional tool definitions for web research
WEB_TOOL_DEFINITIONS = {
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for information. Returns a list of search results with titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default: 5)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    "web_fetch": {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and extract text content from a web URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "The URL to fetch"}},
                "required": ["url"],
            },
        },
    },
}


class ResearcherAgent(BaseAgent):
    """Agent for web research and information gathering."""

    def __init__(
        self,
        ollama_client: OllamaClient,
        research_config: ResearchConfig,
        working_dir: str | None = None,
        model: str = "llama3.2:3b",
        config: AgentConfig | None = None,
        mcp_tools: tuple[dict, list] | None = None,
    ):
        """
        Initialize researcher agent with web and read permissions.

        Args:
            ollama_client: Ollama client instance
            research_config: Research/search configuration
            working_dir: Working directory for local file operations
            model: Model to use (default: llama3.2:3b)
            config: Optional custom config
            mcp_tools: Optional (tools_dict, tool_defs) from MCPToolManager
        """
        if config is None:
            config = AgentConfig(
                name="researcher",
                model=model,
                description="Web research, documentation lookup, information gathering",
                permissions=[Permission.READ, Permission.SEARCH, Permission.WEB],
                system_prompt=RESEARCHER_SYSTEM_PROMPT,
                max_iterations=30,
            )

        super().__init__(
            config=config,
            ollama_client=ollama_client,
            working_dir=working_dir,
            mcp_tools=mcp_tools,
        )

        # Initialize web tools
        self.research_config = research_config
        self.web_search_tool = WebSearchTool(research_config)
        self.web_fetch_tool = WebFetchTool(timeout=30)

        # Add web tools to available tools
        self.tools["web_search"] = self.web_search_tool
        self.tools["web_fetch"] = self.web_fetch_tool

        # Add web tool definitions
        self.tool_definitions.append(WEB_TOOL_DEFINITIONS["web_search"])
        self.tool_definitions.append(WEB_TOOL_DEFINITIONS["web_fetch"])

    async def execute_tool(self, tool_name: str, **kwargs) -> ToolResult:
        """
        Execute a tool, with special handling for web tools.

        Args:
            tool_name: Name of tool to execute
            **kwargs: Tool arguments

        Returns:
            ToolResult from tool execution
        """
        if tool_name == "web_search":
            return await self._execute_web_search(**kwargs)
        elif tool_name == "web_fetch":
            return await self._execute_web_fetch(**kwargs)
        else:
            # Use base class for other tools
            return await super().execute_tool(tool_name, **kwargs)

    async def _execute_web_search(
        self,
        query: str,
        max_results: int = 5,
    ) -> ToolResult:
        """Execute web search."""
        try:
            results = await self.web_search_tool.search(query, max_results=max_results)

            if not results:
                return ToolResult(
                    success=True,
                    data="No results found for query: " + query,
                    metadata={"query": query, "count": 0},
                )

            # Format results
            output_lines = [f"Search results for: {query}\n"]
            for i, result in enumerate(results, 1):
                output_lines.append(f"{i}. {result.title}")
                output_lines.append(f"   URL: {result.url}")
                if result.snippet:
                    output_lines.append(f"   {result.snippet[:200]}...")
                output_lines.append("")

            return ToolResult(
                success=True,
                data="\n".join(output_lines),
                metadata={
                    "query": query,
                    "count": len(results),
                    "engine": self.web_search_tool.get_engine_name(),
                },
            )

        except Exception as e:
            return ToolResult(
                success=False,
                data=None,
                error=f"Search failed: {str(e)}",
            )

    async def _execute_web_fetch(self, url: str) -> ToolResult:
        """Fetch and extract content from URL."""
        try:
            result = await self.web_fetch_tool.fetch(url, extract_text=True)

            if "error" in result:
                return ToolResult(
                    success=False,
                    data=None,
                    error=result["error"],
                )

            if result["status"] != 200:
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"HTTP {result['status']} for {url}",
                )

            # Truncate very long content
            text = result.get("text", result.get("content", ""))
            if len(text) > 5000:
                text = text[:5000] + f"\n\n... (truncated, {len(text)} chars total)"

            return ToolResult(
                success=True,
                data=f"Content from {url}:\n\n{text}",
                metadata={
                    "url": result["url"],
                    "status": result["status"],
                    "length": len(text),
                },
            )

        except Exception as e:
            return ToolResult(
                success=False,
                data=None,
                error=f"Fetch failed: {str(e)}",
            )

    def _detect_tool_intent(self, response_text: str, task: str) -> list[dict]:
        """
        Detect research tool intent from natural language.

        Extends base class detection with web-specific patterns.
        """
        response_lower = response_text.lower()
        task.lower()
        tool_calls = []

        # Web search detection
        search_patterns = [
            "search the web",
            "searching the web",
            "web search",
            "let me search",
            "i'll search",
            "i will search",
            "google for",
            "look up",
            "find information",
            "search for documentation",
            "look for docs",
        ]
        if any(p in response_lower for p in search_patterns):
            # Extract query from task or response
            query = self._extract_search_query(response_text, task)
            if query:
                tool_calls.append({"name": "web_search", "arguments": {"query": query}})
                return tool_calls

        # Web fetch detection
        fetch_patterns = [
            "fetch the url",
            "fetching url",
            "get the page",
            "visit the url",
            "open the link",
            "read the page",
            "fetch from",
            "get content from",
        ]
        if any(p in response_lower for p in fetch_patterns):
            url = self._extract_url(response_text, task)
            if url:
                tool_calls.append({"name": "web_fetch", "arguments": {"url": url}})
                return tool_calls

        # Fall back to base class detection for read/grep/glob
        return super()._detect_tool_intent(response_text, task)

    def _extract_search_query(self, response: str, task: str) -> str | None:
        """Extract search query from context."""
        import re

        # Try to find quoted queries
        quoted = re.search(r'["\']([^"\']+)["\']', task)
        if quoted:
            return quoted.group(1)

        # Use task keywords
        # Remove common prefixes
        query = task
        for prefix in ["search for", "find", "look up", "research", "how to", "what is"]:
            if query.lower().startswith(prefix):
                query = query[len(prefix) :].strip()
                break

        return query if query else None

    def _extract_url(self, response: str, task: str) -> str | None:
        """Extract URL from context."""
        import re

        # Look for URLs in task or response
        url_pattern = r'https?://[^\s<>"\'}\])]+'

        match = re.search(url_pattern, task)
        if match:
            return match.group(0)

        match = re.search(url_pattern, response)
        if match:
            return match.group(0)

        return None

    async def run(self, task: str, **kwargs) -> AgentResult:
        """
        Research a topic using web search and local files.

        Args:
            task: Research task (e.g., "Find documentation for FastAPI routers")
            **kwargs: Additional arguments

        Returns:
            AgentResult with research findings
        """
        # Use the agentic loop from base class
        return await self.agentic_loop(task)
