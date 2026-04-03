"""Live integration tests for PenguinCode ChatAgent against Ollama.

These tests send real prompts through the ChatAgent → Ollama pipeline
and validate the actual LLM responses against scenario criteria.

Requires: Ollama running locally with llama3.2:latest available.
"""

import httpx
import pytest
import pytest_asyncio

from penguincode_cli.agents.chat import ChatAgent
from penguincode_cli.agents.intent import detect_user_intent, estimate_complexity
from penguincode_cli.config.settings import Settings, load_settings
from penguincode_cli.ollama import Message, OllamaClient

# ---------------------------------------------------------------------------
# Skip entire module if Ollama isn't reachable
# ---------------------------------------------------------------------------
try:
    _r = httpx.get("http://localhost:11434/api/tags", timeout=5)
    _r.raise_for_status()
    _models = [m["name"] for m in _r.json().get("models", [])]
    if not any("llama3" in m for m in _models):
        pytest.skip("No llama3 model available in Ollama", allow_module_level=True)
except Exception as exc:
    pytest.skip(f"Ollama not reachable: {exc}", allow_module_level=True)

# ---------------------------------------------------------------------------
# Shared timeout — local models can be slow on first load
# ---------------------------------------------------------------------------
OLLAMA_TIMEOUT = 180  # seconds


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def settings():
    """Load real project settings."""
    try:
        return load_settings("config.yaml")
    except FileNotFoundError:
        s = Settings()
        return s


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def ollama():
    """Module-scoped Ollama client — keeps the connection warm."""
    client = OllamaClient(base_url="http://localhost:11434", timeout=OLLAMA_TIMEOUT)
    await client.__aenter__()
    yield client
    await client.__aexit__(None, None, None)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def chat_agent(ollama, settings):
    """Module-scoped ChatAgent wired to the live Ollama client."""
    return ChatAgent(
        ollama_client=ollama,
        settings=settings,
        project_dir="/tmp/test-scenarios",
    )


# ---------------------------------------------------------------------------
# Helper: raw chat call (bypasses agent routing, tests Ollama directly)
# ---------------------------------------------------------------------------
async def raw_chat(ollama: OllamaClient, prompt: str, system: str = "") -> str:
    """Send a single prompt to Ollama and collect the full response."""
    messages = []
    if system:
        messages.append(Message(role="system", content=system))
    messages.append(Message(role="user", content=prompt))

    response_text = ""
    async for chunk in ollama.chat(
        model="llama3.2:latest",
        messages=messages,
        stream=True,
    ):
        if chunk.message and chunk.message.content:
            response_text += chunk.message.content
    return response_text


# ===========================================================================
# SCENARIO 1: Flask Penguin Website
# ===========================================================================
class TestFlaskScenarioLive:
    """Live test: 'build a python flask website which shows off penguins'."""

    PROMPT = "build a python flask website which shows off penguins"

    async def test_intent_detection(self):
        """Verify intent detection routes this to the right agent."""
        intent = detect_user_intent(self.PROMPT)
        # "build a" triggers planner
        assert intent in ("spawn_planner", "spawn_executor"), (
            f"Expected planner or executor intent, got {intent}"
        )

    async def test_complexity_estimation(self):
        """Multi-file website should be moderate or complex."""
        complexity = estimate_complexity(self.PROMPT)
        assert complexity in ("moderate", "complex"), (
            f"Expected moderate/complex, got {complexity}"
        )

    async def test_ollama_generates_flask_code(self, ollama):
        """Ollama should generate valid Flask code for this prompt."""
        response = await raw_chat(
            ollama,
            self.PROMPT,
            system="You are a Python developer. Respond with working Flask code only.",
        )
        response_lower = response.lower()

        # Must contain Flask fundamentals
        assert "flask" in response_lower, "Response missing Flask import"
        assert "route" in response_lower, "Response missing route decorator"
        assert "penguin" in response_lower, "Response missing penguin content"

    async def test_ollama_includes_template_or_html(self, ollama):
        """Response should include template rendering or inline HTML."""
        response = await raw_chat(
            ollama,
            self.PROMPT,
            system="You are a Python developer. Generate a complete Flask app with routes and templates.",
        )
        response_lower = response.lower()

        has_templates = "render_template" in response_lower
        has_html = "<html" in response_lower or "html" in response_lower
        assert has_templates or has_html, (
            "Response has neither render_template nor HTML content"
        )


# ===========================================================================
# SCENARIO 2: SQLAlchemy vs PyDAL Comparison
# ===========================================================================
class TestDatabaseComparisonLive:
    """Live test: 'tell me about the difference between SQLAlchemy and PyDAL'."""

    PROMPT = "tell me about the difference between SQLAlchemy and PyDAL in python"

    async def test_intent_detection(self):
        """Should route to researcher (explanation/comparison)."""
        intent = detect_user_intent(self.PROMPT)
        assert intent == "spawn_researcher", f"Expected researcher, got {intent}"

    async def test_complexity_estimation(self):
        """Explanation tasks should be simple or moderate."""
        complexity = estimate_complexity(self.PROMPT)
        assert complexity in ("simple", "moderate"), (
            f"Expected simple/moderate, got {complexity}"
        )

    async def test_ollama_covers_both_libraries(self, ollama):
        """Response must mention both SQLAlchemy and PyDAL."""
        response = await raw_chat(
            ollama,
            self.PROMPT,
            system="You are a Python database expert. Provide a thorough comparison.",
        )
        response_lower = response.lower()

        assert "sqlalchemy" in response_lower, "Response missing SQLAlchemy"
        assert "pydal" in response_lower, "Response missing PyDAL"

    async def test_ollama_mentions_key_concepts(self, ollama):
        """Response should cover ORM/DAL concepts and practical differences."""
        response = await raw_chat(
            ollama,
            self.PROMPT,
            system="You are a Python database expert. Cover ORM vs DAL, query syntax, and migrations.",
        )
        response_lower = response.lower()

        # Should mention at least 2 of these core concepts
        concepts = ["orm", "query", "migration", "model", "table", "session", "dal"]
        found = [c for c in concepts if c in response_lower]
        assert len(found) >= 2, (
            f"Response only covered {found} out of expected concepts {concepts}"
        )

    async def test_ollama_provides_examples(self, ollama):
        """Response should include code examples or syntax comparisons."""
        response = await raw_chat(
            ollama,
            self.PROMPT,
            system="You are a Python expert. Include code examples for both libraries.",
        )

        # Look for code indicators
        has_code_block = "```" in response
        has_import = "import" in response
        has_syntax = "db." in response or "session." in response or "query" in response.lower()
        assert has_code_block or has_import or has_syntax, (
            "Response lacks code examples or syntax demonstrations"
        )


# ===========================================================================
# SCENARIO 3: Go Cross-Platform GUI App
# ===========================================================================
class TestGoGUILive:
    """Live test: 'write a golang app which pops open a hello world window'."""

    PROMPT = "write a golang app which pops open a hello world window on run for mac and windows"

    async def test_intent_detection(self):
        """Should route to planner or executor (code generation)."""
        intent = detect_user_intent(self.PROMPT)
        assert intent in ("spawn_planner", "spawn_executor"), (
            f"Expected planner or executor, got {intent}"
        )

    async def test_complexity_estimation(self):
        """Cross-platform GUI app should be moderate or complex."""
        complexity = estimate_complexity(self.PROMPT)
        assert complexity in ("moderate", "complex"), (
            f"Expected moderate/complex, got {complexity}"
        )

    async def test_ollama_generates_go_code(self, ollama):
        """Ollama should produce valid Go code with main package."""
        response = await raw_chat(
            ollama,
            self.PROMPT,
            system="You are a Go developer. Write a complete Go GUI application.",
        )

        assert "package main" in response, "Response missing 'package main'"
        assert "func main()" in response, "Response missing 'func main()'"

    async def test_ollama_uses_gui_library(self, ollama):
        """Response should use a known Go GUI library."""
        response = await raw_chat(
            ollama,
            self.PROMPT,
            system="You are a Go developer. Use a cross-platform GUI library.",
        )
        response_lower = response.lower()

        gui_libs = ["fyne", "gio", "walk", "qt", "gtk", "webview", "wails", "lorca"]
        found = [lib for lib in gui_libs if lib in response_lower]
        assert found, (
            f"Response doesn't reference any known Go GUI library. "
            f"Checked: {gui_libs}"
        )

    async def test_ollama_displays_hello_world(self, ollama):
        """Response should display a 'hello world' message."""
        response = await raw_chat(
            ollama,
            self.PROMPT,
            system="You are a Go developer. Create a window that displays 'Hello World'.",
        )
        response_lower = response.lower()

        assert "hello" in response_lower, "Response missing 'hello' text"
        # "world" or "window" — either proves UI output
        assert "world" in response_lower or "window" in response_lower, (
            "Response missing 'world' or 'window' reference"
        )

    async def test_ollama_includes_go_mod(self, ollama):
        """Response should mention go.mod or module dependency setup."""
        response = await raw_chat(
            ollama,
            self.PROMPT,
            system="You are a Go developer. Include the go.mod file and dependencies.",
        )
        response_lower = response.lower()

        has_go_mod = "go.mod" in response_lower or "go mod" in response_lower
        has_module = "module " in response_lower
        has_require = "require " in response_lower
        assert has_go_mod or has_module or has_require, (
            "Response missing go.mod / module declaration / require directive"
        )
