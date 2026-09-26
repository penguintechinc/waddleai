"""Interactive REPL loop for PenguinCode chat."""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.table import Table

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.client.knowledge_client import (
    KnowledgeClient,
    KnowledgeClientError,
    LibraryRef,
    RemoteMemoryManager,
)
from penguincode_cli.client.lessons_client import (
    LessonsClient,
    LessonsClientError,
    LessonsPermissionDeniedError,
)
from penguincode_cli.config.settings import (
    Settings,
    get_config_value,
    load_settings,
    save_settings,
    set_config_value,
    settings_to_dict,
)
from penguincode_cli.ollama import OllamaClient
from penguincode_cli.skills import SkillLoader
from penguincode_cli.ui import console, print_error, print_info, print_success

from .session import SessionManager

# Lazy imports to avoid circular dependency
if TYPE_CHECKING:
    from penguincode_cli.agents import ChatAgent


class REPLSession:
    """Interactive REPL session with agentic chat loop."""

    def __init__(self, project_dir: str = ".", config_path: str = "config.yaml"):
        """
        Initialize REPL session.

        Args:
            project_dir: Project directory
            config_path: Path to config.yaml
        """
        self.project_dir = Path(project_dir).resolve()
        self.config_path = config_path

        # Load settings
        try:
            self.settings = load_settings(config_path)
        except FileNotFoundError:
            print_error(f"Config file not found: {config_path}")
            print_info("Using default configuration")
            self.settings = Settings()

        # Initialize session manager
        self.session_manager = SessionManager(str(self.project_dir))
        self.session = self.session_manager.create_session()

        # Ollama client (will be initialized in async context)
        self.ollama_client: OllamaClient | None = None

        # Chat agent (main orchestrator) and specialized agents
        self.chat_agent: ChatAgent | None = None
        self.agents = {}

        # Thin gRPC client (F3): the CLI's only path to the server-side knowledge
        # platform -- docs-RAG indexing, hybrid GraphRAG retrieval, scoped memory, and the
        # code graph all live server-side now (see `client.knowledge_client`'s module
        # docstring). Constructed once per session in `__aenter__`, closed in `__aexit__`.
        self.knowledge_client: KnowledgeClient | None = None

        # Thin gRPC client (T-L2b): the CLI's only path to the server-side lessons-learned
        # promotion review workflow -- propose/list/approve/reject all live server-side (see
        # `client.lessons_client`'s module docstring). Constructed once per session in
        # `__aenter__`, closed in `__aexit__`, same lifecycle as `knowledge_client`.
        self.lessons_client: LessonsClient | None = None

        # Docs RAG components (initialized if enabled) -- `docs_fetcher` still fetches raw
        # doc text/HTML client-side; the embedding + vector-store write happens server-side
        # via `knowledge_client.index()`.
        self.project_context = None
        self.docs_fetcher = None
        self.context_injector = None

        # Vestigial (penguincode-knowledge-platform F3): docs-RAG/index-code/memory identity
        # now comes exclusively from the WaddleAI JWT `self.knowledge_client` attaches to
        # every call (F4's `WaddleAITokenProvider`), never a client-constructed
        # `ScopeContext` -- the interactive CLI REPL still has no local auth flow of its own.
        # Kept only as a placeholder for a possible future CLI-local scope use.
        self.scope_ctx: ScopeContext | None = None

        # Memory manager for cross-session persistence -- a `RemoteMemoryManager` facade
        # over `self.knowledge_client` (initialized in async context), never a local
        # `tools.memory.MemoryManager` (no local mem0/pgvector instance).
        self.memory_manager: RemoteMemoryManager | None = None

        # Skill system
        self.skill_loader = SkillLoader()
        self.skill_loader.discover()
        self.active_skill: str | None = None

    async def __aenter__(self):
        """Async context manager entry."""
        # Lazy import agents to avoid circular import
        from penguincode_cli.agents import ChatAgent, ExecutorAgent, ExplorerAgent

        # Initialize Ollama client
        self.ollama_client = OllamaClient(
            base_url=self.settings.ollama.api_url,
            timeout=self.settings.ollama.timeout,
        )
        await self.ollama_client.__aenter__()

        # Thin gRPC client (F3): shared for the whole session -- docs-RAG, memory, and
        # code-graph all route through this one `KnowledgeClient`. Construction never fails
        # (the channel is lazy; a WaddleAI token is only acquired on first real call), so
        # server-unreachable is discovered -- and reported -- at first use, not here.
        self.knowledge_client = KnowledgeClient(self.settings.server)
        self.lessons_client = LessonsClient(self.settings.server)

        # Memory manager for cross-session persistence -- a thin facade over
        # `self.knowledge_client`'s `MemoryAdd`/`MemorySearch` RPCs.
        if self.settings.memory.enabled:
            self.memory_manager = RemoteMemoryManager(self.knowledge_client)
            print_info("Memory layer initialized (server-side)")

        # Fetch organizational config from server (if configured)
        await self._fetch_org_config()

        # Initialize chat agent (main orchestrator) with memory support
        self.chat_agent = ChatAgent(
            ollama_client=self.ollama_client,
            settings=self.settings,
            project_dir=str(self.project_dir),
            memory_manager=self.memory_manager,
            session_id=self.session.session_id,
        )

        # Discover MCP tools so agents see them on first spawn
        if self.settings.mcp.enabled and self.settings.mcp.servers:
            try:
                mcp_tools = await self.chat_agent._get_mcp_tools()
                if mcp_tools:
                    print_info(
                        f"MCP: {len(mcp_tools[0])} tool(s) from {len(self.settings.mcp.servers)} server(s)"
                    )
            except Exception as e:
                print_info(f"MCP discovery skipped: {e}")

        # Keep direct agent references for manual commands (/explore, /execute)
        explorer_model = self.settings.models.orchestration
        executor_model = self.settings.models.execution

        self.agents["executor"] = ExecutorAgent(
            ollama_client=self.ollama_client,
            working_dir=str(self.project_dir),
            model=executor_model,
        )
        self.agents["explorer"] = ExplorerAgent(
            ollama_client=self.ollama_client,
            working_dir=str(self.project_dir),
            model=explorer_model,
        )

        # Initialize docs RAG if enabled
        if self.settings.docs_rag.enabled:
            await self._init_docs_rag()

        return self

    async def _fetch_org_config(self) -> None:
        """Fetch organizational config from management API server (if configured)."""
        if not self.settings.client.server_url:
            return

        try:
            from penguincode_cli.client.org_config import OrgConfigClient
            from penguincode_cli.config.settings import MCPServerConfig

            client = OrgConfigClient(
                server_url=self.settings.client.server_url,
                shared_key=self.settings.client.shared_key,
                token_path=self.settings.client.token_path,
            )

            # Authenticate first
            if not await client.authenticate():
                print_info("Org config: auth failed, skipping")
                return

            # Fetch all org config
            org = await client.fetch_all()

            # Merge org MCP servers (local takes priority on name collision)
            if org["mcp_servers"]:
                existing = {s.name for s in self.settings.mcp.servers}
                for srv in org["mcp_servers"]:
                    if isinstance(srv, dict) and srv.get("name") and srv["name"] not in existing:
                        self.settings.mcp.servers.append(
                            MCPServerConfig(
                                name=srv["name"],
                                enabled=srv.get("enabled", True),
                                transport=srv.get("transport", "stdio"),
                                command=srv.get("command", ""),
                                args=srv.get("args", []),
                                url=srv.get("url", ""),
                                env=srv.get("env", {}),
                                headers=srv.get("headers", {}),
                                timeout=srv.get("timeout", 30),
                            )
                        )
                        existing.add(srv["name"])
                print_info(f"Org config: merged {len(org['mcp_servers'])} MCP server(s)")

        except ImportError:
            pass  # httpx not available
        except Exception as e:
            print_info(f"Org config unavailable: {e}")

    async def _init_docs_rag(self) -> None:
        """Initialize documentation RAG system."""
        try:
            from penguincode_cli.docs_rag import (
                ContextInjector,
                DocumentationFetcher,
                Language,
                ProjectContext,
                ProjectDetector,
            )

            # Start with manual languages from config
            manual_languages = []
            for lang_name, enabled in self.settings.docs_rag.languages_manual.items():
                if enabled:
                    try:
                        manual_languages.append(Language(lang_name.lower()))
                    except ValueError:
                        print_error(f"Unknown language in config: {lang_name}")

            # Auto-detect project languages if enabled
            if self.settings.docs_rag.auto_detect_on_start:
                detector = ProjectDetector(str(self.project_dir))
                self.project_context = detector.detect()

                # Merge manual languages with detected ones
                for lang in manual_languages:
                    if lang not in self.project_context.languages:
                        self.project_context.languages.append(lang)

                if self.project_context.languages:
                    langs = ", ".join(self.project_context.language_names)
                    libs_count = len(self.project_context.libraries)
                    print_info(f"Detected: {langs} ({libs_count} libraries)")
            else:
                # Use only manual languages
                self.project_context = ProjectContext(languages=manual_languages)

            # Initialize fetcher and indexer
            self.docs_fetcher = DocumentationFetcher(
                cache_dir=self.settings.docs_rag.cache_dir,
                max_pages_per_library=self.settings.docs_rag.max_pages_per_library,
                cache_max_age_days=self.settings.docs_rag.cache_max_age_days,
            )

            # Embedding + vector-store indexing is server-side now (F3) -- the injector
            # queries `self.knowledge_client` directly, no local `DocumentationIndexer`.
            assert self.knowledge_client is not None  # set in __aenter__ before this call
            self.context_injector = ContextInjector(
                self.knowledge_client,
                max_context_tokens=self.settings.docs_rag.max_context_tokens,
                max_chunks=self.settings.docs_rag.max_chunks_per_query,
            )

            # Cleanup expired cache entries
            expired = self.docs_fetcher.expunge_expired()
            if expired > 0:
                print_info(f"Cleaned up {expired} expired doc cache entries")

            # Cleanup unused library docs
            if self.project_context:
                removed = self.docs_fetcher.cleanup_unused_libraries(self.project_context.libraries)
                if removed:
                    print_info(f"Removed docs for unused libraries: {', '.join(removed.keys())}")

            # Auto-index on detect if enabled
            if self.settings.docs_rag.auto_index_on_detect and self.project_context:
                await self._auto_index_languages()

        except ImportError as e:
            print_info(f"Docs RAG not available: {e}")
        except Exception as e:
            print_error(f"Docs RAG init failed: {e}")

    async def _auto_index_languages(self) -> None:
        """Auto-index documentation for detected/configured languages.

        Freshness/dedup (skip already-indexed languages) is the server's own
        responsibility now (`DocumentationIndexer.index_language`'s local cache moved
        server-side with it, F3) -- this loop simply calls `Index` for every detected
        language on every session start; the server decides whether there's real work to do.
        """
        if not self.project_context or not self.docs_fetcher or self.knowledge_client is None:
            return

        from penguincode_cli.docs_rag import get_language_doc_source

        indexed_count = 0
        for lang in self.project_context.languages:
            # Get doc source for language
            doc_source = get_language_doc_source(lang)
            if not doc_source:
                continue

            console.print(f"[dim]Indexing {lang.value} documentation...[/dim]")

            try:
                # Fetch language docs
                docs = await self.docs_fetcher.fetch_language_docs(lang)
                if docs:
                    chunks = await self.knowledge_client.index(
                        language=lang.value, doc_contents=docs
                    )
                    indexed_count += chunks
                    console.print(f"[dim]  Indexed {chunks} chunks for {lang.value}[/dim]")
            except KnowledgeClientError as e:
                console.print(f"[dim]  Failed to index {lang.value}: {e}[/dim]")

        if indexed_count > 0:
            print_info(f"Auto-indexed {indexed_count} documentation chunks")

    async def _ensure_language_indexed(self, language: str) -> bool:
        """Ensure a language's documentation is indexed (on-demand), via the `Index` RPC.

        Args:
            language: Language name (e.g., "python", "javascript")

        Returns:
            True if indexed successfully

        Freshness/dedup is the server's responsibility now (F3, see
        `_auto_index_languages`'s own note) -- no local "already indexed" check.
        """
        if not self.docs_fetcher or self.knowledge_client is None:
            return False

        from penguincode_cli.docs_rag import Language, get_language_doc_source

        # Get Language enum
        try:
            lang_enum = Language(language.lower())
        except ValueError:
            return False

        # Get doc source
        doc_source = get_language_doc_source(lang_enum)
        if not doc_source:
            return False

        console.print(f"[dim]Indexing {language} documentation on-demand...[/dim]")

        try:
            docs = await self.docs_fetcher.fetch_language_docs(lang_enum)
            if docs:
                chunks = await self.knowledge_client.index(
                    language=lang_enum.value, doc_contents=docs
                )
                console.print(f"[dim]  Indexed {chunks} chunks[/dim]")
                return True
        except KnowledgeClientError as e:
            console.print(f"[dim]  Failed: {e}[/dim]")

        return False

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        # Save session
        self.session_manager.save_session(self.session)

        # Shutdown MCP tool manager (stop stdio server processes)
        if self.chat_agent:
            await self.chat_agent.shutdown()

        # Close Ollama client
        if self.ollama_client:
            await self.ollama_client.__aexit__(exc_type, exc_val, exc_tb)

        # Close the gRPC channel to the penguincode server (F3)
        if self.knowledge_client:
            await self.knowledge_client.close()

        # Close the gRPC channel used by the lessons-promotion review workflow (T-L2b)
        if self.lessons_client:
            await self.lessons_client.close()

    async def handle_command(self, command: str) -> bool:
        """
        Handle REPL commands.

        Args:
            command: Command string

        Returns:
            True to continue REPL, False to exit
        """
        parts = command.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd == "/help":
            self.show_help()
        elif cmd == "/exit" or cmd == "/quit":
            return False
        elif cmd == "/clear":
            console.clear()
            if self.chat_agent:
                self.chat_agent.reset_conversation()
            print_info("Screen and conversation cleared")
        elif cmd == "/history":
            self.show_history()
        elif cmd == "/agents":
            self.show_agents()
        elif cmd == "/read":
            await self.handle_read(args)
        elif cmd == "/explore":
            await self.handle_explore(args)
        elif cmd == "/execute":
            await self.handle_execute(args)
        elif cmd == "/reset":
            if self.chat_agent:
                self.chat_agent.reset_conversation()
            print_info("Conversation reset")
        elif cmd == "/docs":
            await self.handle_docs_command(args)
        elif cmd == "/index-code":
            await self.handle_index_code(args)
        elif cmd == "/lesson":
            await self.handle_lesson_command(args)
        elif cmd in ("/skill", "/skills"):
            self.handle_skill_command(args)
        elif cmd == "/config":
            self.handle_config_command(args)
        else:
            print_error(f"Unknown command: {cmd}")
            print_info("Type /help for available commands")

        return True

    def show_help(self) -> None:
        """Show help message."""
        help_text = """
[bold cyan]PenguinCode Commands:[/bold cyan]

[yellow]General:[/yellow]
  /help              Show this help message
  /exit, /quit       Exit PenguinCode
  /clear             Clear screen and reset conversation
  /reset             Reset conversation history
  /history           Show conversation history
  /agents            List available agents

[yellow]Agent Commands:[/yellow]
  /explore <query>   Explore codebase (read-only)
  /execute <task>    Execute code changes
  /read <path>       Read a file

[yellow]Documentation RAG:[/yellow]
  /docs status       Show detection and index status
  /docs detect       Re-run project detection
  /docs index [lib]  Index documentation (all or specific library)
  /docs search <q>   Search indexed documentation
  /docs clear [lib]  Clear index (all or specific library)
  /docs cleanup      Remove docs for unused libraries

[yellow]Code Graph:[/yellow]
  /index-code [path] Build the code graph for a local source tree
                     (default: project dir; requires an authenticated
                     ScopeContext and the penguincode.code-graph flag)

[yellow]Lessons-Learned Promotion:[/yellow]
  /lesson promote <text>   Propose <text> as a firm-wide lesson (scrub+verify)
  /lesson pending [status] List the review queue (default: pending)
  /lesson approve <id>     Approve a pending lesson (requires approval scope)
  /lesson reject <id> [reason]  Reject a pending lesson (requires approval scope)

[yellow]Skills:[/yellow]
  /skill             List available skills
  /skill <name>      Activate a skill (guides LLM behavior)
  /skill <name> <ctx> Activate with additional context
  /skill chain <name> Activate skill + all referenced skills
  /skill off         Deactivate current skill

[yellow]Configuration:[/yellow]
  /config            Show config summary
  /config show       Full config as YAML (sensitive values masked)
  /config <key>      Show single value (e.g., /config models.execution)
  /config set <k> <v> Set runtime value (e.g., /config set defaults.context_window 16384)
  /config reset      Reload from config.yaml
  /config save       Persist to ~/.config/penguincode/settings.yaml

[yellow]Chat:[/yellow]
  Just type your message to chat with the orchestrator.
  The orchestrator will automatically delegate to the right agent.

[yellow]Examples:[/yellow]
  > Find all Python files         (uses explorer)
  > What does main.py do?         (uses explorer)
  > Create a new file hello.py    (uses executor)
  > Fix the bug in auth.py        (uses executor)
  > Run the tests                 (uses executor)
"""
        console.print(help_text)

    def show_history(self) -> None:
        """Show conversation history."""
        if not self.session.messages:
            print_info("No messages in this session")
            return

        console.print("\n[bold cyan]Session History:[/bold cyan]\n")
        for msg in self.session.messages:
            role_color = "green" if msg.role == "user" else "blue"
            content = msg.content
            if len(content) > 200:
                content = content[:200] + "..."
            console.print(f"[{role_color}]{msg.role}:[/{role_color}] {content}\n")

    def show_agents(self) -> None:
        """Show available agents."""
        console.print("\n[bold cyan]Available Agents:[/bold cyan]\n")
        for name, agent in self.agents.items():
            console.print(
                f"  [green]{name}[/green]: {agent.config.description} "
                f"[dim](model: {agent.config.model})[/dim]"
            )
        console.print()

    def handle_skill_command(self, args: str) -> None:
        """Handle /skill command for skill management."""
        if not args:
            # List all skills
            skills = self.skill_loader.list_all()
            if not skills:
                print_info("No skills found")
                return

            table = Table(show_header=True, title="Available Skills")
            table.add_column("Name", style="green")
            table.add_column("Description", style="dim")
            table.add_column("Refs", style="cyan")
            table.add_column("Model", style="yellow")

            for name, info in sorted(skills.items()):
                refs = ", ".join(info.references[:3]) if info.references else "-"
                # Truncate description to 60 chars
                desc = (
                    info.description[:60] + "..."
                    if len(info.description) > 60
                    else info.description
                )
                active = " [bold yellow]*[/bold yellow]" if name == self.active_skill else ""
                model_str = info.model or "default"
                table.add_row(f"{name}{active}", desc, refs, model_str)

            console.print(table)
            if self.active_skill:
                console.print(f"\n[yellow]Active skill:[/yellow] {self.active_skill}")
            console.print("[dim]Use /skill <name> to activate, /skill off to deactivate[/dim]")
            return

        parts = args.split(maxsplit=1)
        subcmd = parts[0].lower()

        if subcmd == "off":
            if self.active_skill and self.chat_agent:
                self.chat_agent.deactivate_skill()
            self.active_skill = None
            print_info("Skill deactivated")
            return

        if subcmd == "chain":
            # Activate skill with full chain
            if len(parts) < 2:
                print_error("Usage: /skill chain <name>")
                return
            skill_name = parts[1].strip()
            chain = self.skill_loader.get_chain(skill_name)
            if not chain:
                print_error(f"Skill not found: {skill_name}")
                return

            # Concatenate all chain content
            combined = "\n\n---\n\n".join(s.content for s in chain)
            chain_names = [s.name for s in chain]

            if self.chat_agent:
                self.chat_agent.activate_skill(
                    skill_name,
                    combined,
                    chain=chain_names,
                    model=chain[0].model,
                )
            self.active_skill = skill_name
            model_msg = f" (model: {chain[0].model})" if chain[0].model else ""
            print_success(f"Skill chain activated: {' → '.join(chain_names)}{model_msg}")
            return

        # Activate a single skill
        skill_name = subcmd
        # Allow extra context after skill name
        extra_context = parts[1] if len(parts) > 1 else ""

        skill = self.skill_loader.get(skill_name)
        if not skill:
            print_error(f"Skill not found: {skill_name}")
            print_info("Use /skill to list available skills")
            return

        content = skill.content
        if extra_context:
            content += f"\n\n## Additional Context\n\n{extra_context}"

        if self.chat_agent:
            self.chat_agent.activate_skill(skill_name, content, model=skill.model)
        self.active_skill = skill_name
        model_msg = f" (model: {skill.model})" if skill.model else ""
        print_success(f"Skill activated: {skill_name}{model_msg}")

    def handle_config_command(self, args: str) -> None:
        """Handle /config command for viewing and modifying runtime settings."""
        if not args:
            self._show_config_summary()
            return

        parts = args.split(maxsplit=2)
        subcmd = parts[0].lower()

        if subcmd == "show":
            self._show_config_full()
        elif subcmd == "reset":
            try:
                self.settings = load_settings(self.config_path)
                if self.chat_agent:
                    self.chat_agent.settings = self.settings
                    self.chat_agent.model = self.settings.models.orchestration
                print_success("Configuration reloaded from config.yaml")
            except Exception as e:
                print_error(f"Failed to reload config: {e}")
        elif subcmd == "save":
            try:
                path = save_settings(self.settings)
                print_success(f"Configuration saved to {path}")
            except Exception as e:
                print_error(f"Failed to save config: {e}")
        elif subcmd == "set":
            if len(parts) < 3:
                print_error("Usage: /config set <key> <value>")
                return
            key = parts[1]
            value = parts[2]
            try:
                old_val, new_val = set_config_value(self.settings, key, value)
                print_success(f"{key}: {old_val} -> {new_val}")
                # Live-update chat agent model if orchestration model changed
                if key == "models.orchestration" and self.chat_agent:
                    self.chat_agent.model = str(new_val)
                # Live-update agent concurrency
                if key == "regulators.max_concurrent_agents" and self.chat_agent:
                    self.chat_agent.agent_semaphore.adjust_max(int(new_val))
            except AttributeError:
                print_error(f"Unknown config key: {key}")
            except (ValueError, TypeError) as e:
                print_error(f"Invalid value: {e}")
        else:
            # Treat as a dotpath lookup
            try:
                value = get_config_value(self.settings, subcmd)
                console.print(f"[yellow]{subcmd}:[/yellow] {value}")
            except AttributeError:
                print_error(f"Unknown config key: {subcmd}")

    def _show_config_summary(self) -> None:
        """Show a concise config summary."""
        s = self.settings
        console.print("\n[bold cyan]PenguinCode Configuration[/bold cyan]\n")
        console.print(f"[yellow]Ollama URL:[/yellow]    {s.ollama.api_url}")
        console.print("[yellow]Models:[/yellow]")
        console.print(f"  orchestration: {s.models.orchestration}")
        console.print(f"  execution:     {s.models.execution}")
        console.print(f"  planning:      {s.models.planning}")
        console.print(f"  research:      {s.models.research}")
        console.print(f"[yellow]Context window:[/yellow] {s.defaults.context_window}")
        console.print(f"[yellow]Max agents:[/yellow]     {s.regulators.max_concurrent_agents}")
        console.print(f"[yellow]Agent timeout:[/yellow]  {s.regulators.agent_timeout_seconds}s")
        console.print(
            f"[yellow]Memory:[/yellow]         {'enabled' if s.memory.enabled else 'disabled'}"
        )
        console.print(
            f"[yellow]Docs RAG:[/yellow]       {'enabled' if s.docs_rag.enabled else 'disabled'}"
        )
        console.print()
        console.print(
            "[dim]Use /config show for full config, /config set <key> <value> to modify[/dim]"
        )
        console.print()

    def _show_config_full(self) -> None:
        """Show full config as YAML with sensitive values masked."""
        import yaml as _yaml

        data = settings_to_dict(self.settings)

        # Mask sensitive values
        sensitive_keys = {"jwt_secret", "jwt_token", "api_key", "firecrawl_api_key"}

        def _mask(d):
            if isinstance(d, dict):
                return {
                    k: ("****" if k in sensitive_keys and v else _mask(v)) for k, v in d.items()
                }
            if isinstance(d, list):
                return [_mask(item) for item in d]
            return d

        masked = _mask(data)
        output = _yaml.dump(masked, default_flow_style=False, sort_keys=False)
        console.print("\n[bold cyan]Full Configuration:[/bold cyan]\n")
        console.print(output)

    async def handle_read(self, path: str) -> None:
        """Handle /read command."""
        if not path:
            print_error("Usage: /read <path>")
            return

        explorer = self.agents["explorer"]
        result = await explorer.execute_tool("read", path=path)

        if result.success:
            console.print(result.data)
        else:
            print_error(result.error or "Failed to read file")

    async def handle_explore(self, query: str) -> None:
        """Handle /explore command."""
        if not query:
            print_error("Usage: /explore <query>")
            return

        console.print(f"\n[cyan]Exploring:[/cyan] {query}\n")

        explorer = self.agents["explorer"]
        result = await explorer.run(query)

        if result.success:
            console.print(result.output)
        else:
            print_error(result.error or "Exploration failed")

    async def handle_execute(self, task: str) -> None:
        """Handle /execute command."""
        if not task:
            print_error("Usage: /execute <task>")
            return

        console.print(f"\n[cyan]Executing:[/cyan] {task}\n")

        executor = self.agents["executor"]
        result = await executor.run(task)

        if result.success:
            console.print(result.output)
            print_success("Task completed")
        else:
            print_error(result.error or "Execution failed")

    async def handle_index_code(self, path_arg: str) -> None:
        """Handle `/index-code [path]`: build the tree-sitter code graph for a source tree.

        Drives the server's `IndexCode` RPC (F3) -- `graphs.code.index_code` (T11) itself
        now runs entirely server-side. Identity comes from the WaddleAI JWT
        `self.knowledge_client` attaches to the call, never a local `ScopeContext`.
        """
        if self.knowledge_client is None:
            print_info("Code-graph indexing requires the penguincode server -- not connected")
            return

        target = Path(path_arg).expanduser().resolve() if path_arg else self.project_dir
        if not target.exists():
            print_error(f"Path not found: {target}")
            return
        if not target.is_dir():
            print_error(f"Not a directory: {target}")
            return

        console.print(f"\n[cyan]Indexing code graph for {target}...[/cyan]\n")
        try:
            result = await self.knowledge_client.index_code(root_path=str(target))
        except KnowledgeClientError as e:
            print_error(f"Code-graph indexing failed: {e}")
            return

        if result is None:
            print_info("Code-graph indexing is disabled (penguincode.code-graph flag is off)")
            return

        node_count, edge_count = result
        print_success(f"Code graph: {node_count} node(s), {edge_count} edge(s)")

    async def handle_lesson_command(self, args: str) -> None:
        """Handle `/lesson <subcommand>`: the lessons-learned promotion review workflow
        (T-L2b) -- propose a lesson, list the review queue, approve, or reject.

        Identity comes exclusively from the WaddleAI JWT `self.lessons_client` attaches to
        every call, never a local `ScopeContext`. Every subcommand degrades cleanly (a clear
        message, never a crash) when the server isn't connected, a call fails
        (`LessonsClientError`, e.g. unreachable server), or -- for `approve`/`reject` --
        the caller lacks the elevated approval scope (`LessonsPermissionDeniedError`).
        """
        if self.lessons_client is None:
            print_info("Lessons-promotion requires the penguincode server -- not connected")
            return

        parts = args.split(maxsplit=1)
        if not parts:
            print_error("Usage: /lesson <promote|pending|approve|reject> ...")
            return
        subcmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if subcmd == "promote":
            await self._handle_lesson_promote(rest)
        elif subcmd == "pending":
            await self._handle_lesson_pending(rest)
        elif subcmd == "approve":
            await self._handle_lesson_approve(rest)
        elif subcmd == "reject":
            await self._handle_lesson_reject(rest)
        else:
            print_error(f"Unknown /lesson subcommand: {subcmd}")
            print_info("Usage: /lesson <promote|pending|approve|reject> ...")

    async def _handle_lesson_promote(self, text: str) -> None:
        """`/lesson promote <text>`: propose `text` as a firm-wide lesson.

        Runs the server's scrub+verify pipeline; a blocked result reports every residual
        confidentiality finding, a clean result reports the new pending review id.
        """
        text = text.strip()
        if not text:
            print_error("Usage: /lesson promote <text>")
            return

        assert self.lessons_client is not None  # guarded by handle_lesson_command
        try:
            result = await self.lessons_client.promote(source_content=text)
        except LessonsClientError as e:
            print_error(f"Lesson promotion failed: {e}")
            return

        if result.blocked:
            print_error("Lesson blocked -- residual confidentiality issue(s) found:")
            for finding in result.findings:
                console.print(f"  [red]-[/red] {finding.kind}: {finding.detail}")
            return

        print_success(f"Lesson proposed for firm-wide review (pending id: {result.pending_id})")

    async def _handle_lesson_pending(self, status: str) -> None:
        """`/lesson pending [status]`: list the caller's tenant's review queue."""
        status = status.strip()
        assert self.lessons_client is not None  # guarded by handle_lesson_command
        try:
            items = await self.lessons_client.list_pending(status=status)
        except LessonsClientError as e:
            print_error(f"Could not list pending lessons: {e}")
            return

        if not items:
            suffix = f" with status {status!r}" if status else ""
            print_info(f"No pending lessons{suffix}")
            return

        table = Table(show_header=True, title="Lessons Review Queue")
        table.add_column("ID", style="green")
        table.add_column("Status", style="yellow")
        table.add_column("Lesson", style="dim")
        table.add_column("Proposer", style="cyan")
        table.add_column("Team", style="cyan")
        for item in items:
            preview = (
                item.generalized_text[:60] + "..."
                if len(item.generalized_text) > 60
                else item.generalized_text
            )
            table.add_row(item.id, item.status, preview, item.proposer, item.source_team)
        console.print(table)

    async def _handle_lesson_approve(self, pending_id: str) -> None:
        """`/lesson approve <id>`: approve a pending lesson (requires the approval scope)."""
        pending_id = pending_id.strip()
        if not pending_id:
            print_error("Usage: /lesson approve <id>")
            return

        assert self.lessons_client is not None  # guarded by handle_lesson_command
        try:
            approved = await self.lessons_client.approve(pending_id)
        except LessonsPermissionDeniedError as e:
            print_error(f"You do not have permission to approve lessons: {e}")
            return
        except LessonsClientError as e:
            print_error(f"Approval failed: {e}")
            return

        if approved:
            print_success(f"Lesson {pending_id} approved and shared firm-wide")
        else:
            print_error(f"Lesson {pending_id} was not approved")

    async def _handle_lesson_reject(self, args: str) -> None:
        """`/lesson reject <id> [reason]`: reject a pending lesson (requires the approval
        scope)."""
        parts = args.split(maxsplit=1)
        if not parts or not parts[0]:
            print_error("Usage: /lesson reject <id> [reason]")
            return
        pending_id = parts[0]
        reason = parts[1] if len(parts) > 1 else ""

        assert self.lessons_client is not None  # guarded by handle_lesson_command
        try:
            rejected = await self.lessons_client.reject(pending_id, reason=reason)
        except LessonsPermissionDeniedError as e:
            print_error(f"You do not have permission to reject lessons: {e}")
            return
        except LessonsClientError as e:
            print_error(f"Rejection failed: {e}")
            return

        if rejected:
            print_success(f"Lesson {pending_id} rejected")
        else:
            print_error(f"Lesson {pending_id} was not rejected")

    async def handle_docs_command(self, args: str) -> None:
        """Handle /docs subcommands."""
        if not self.settings.docs_rag.enabled:
            print_error("Docs RAG is disabled in config")
            return

        parts = args.split(maxsplit=1)
        subcmd = parts[0].lower() if parts else "status"
        subargs = parts[1] if len(parts) > 1 else ""

        if subcmd == "status":
            await self._docs_status()
        elif subcmd == "detect":
            await self._docs_detect()
        elif subcmd == "index":
            await self._docs_index(subargs)
        elif subcmd == "search":
            await self._docs_search(subargs)
        elif subcmd == "clear":
            await self._docs_clear(subargs)
        elif subcmd == "cleanup":
            await self._docs_cleanup()
        else:
            print_error(f"Unknown docs command: {subcmd}")
            print_info("Use: /docs status|detect|index|search|clear|cleanup")

    async def _docs_status(self) -> None:
        """Show docs RAG status."""
        console.print("\n[bold cyan]Documentation RAG Status[/bold cyan]\n")

        # Project detection
        if self.project_context:
            console.print("[yellow]Detected Languages:[/yellow]")
            for lang in self.project_context.languages:
                console.print(f"  - {lang.value}")

            console.print(
                f"\n[yellow]Detected Libraries ({len(self.project_context.libraries)}):[/yellow]"
            )
            # Group by language
            by_lang = {}
            for lib in self.project_context.libraries[:20]:  # Show first 20
                lang = lib.language.value
                if lang not in by_lang:
                    by_lang[lang] = []
                by_lang[lang].append(lib.name)

            for lang, libs in by_lang.items():
                console.print(f"  [{lang}] {', '.join(libs[:10])}")
                if len(libs) > 10:
                    console.print(f"        ... and {len(libs) - 10} more")
        else:
            print_info("No project context (run /docs detect)")

        # Index status: server-side now (F3/C1), via the `IndexStatus` RPC --
        # `DocumentationIndexer.get_index_status` is the source of truth, the CLI just
        # renders it.
        if self.knowledge_client is not None:
            console.print("\n[yellow]Index Status:[/yellow]")
            try:
                status = await self.knowledge_client.index_status()
            except KnowledgeClientError as e:
                print_error(f"Failed to fetch index status: {e}")
            else:
                if not status.libraries and not status.languages:
                    print_info("Nothing indexed yet")
                else:
                    for lib_key, lib_status in status.libraries.items():
                        expired = " [red](expired)[/red]" if lib_status.is_expired else ""
                        console.print(
                            f"  [{lib_status.language}] {lib_key}: "
                            f"{lib_status.chunk_count} chunks{expired}"
                        )
                    for lang_key, lang_status in status.languages.items():
                        expired = " [red](expired)[/red]" if lang_status.is_expired else ""
                        console.print(
                            f"  [_lang_] {lang_key}: {lang_status.chunk_count} chunks{expired}"
                        )
                    console.print(f"  Total chunks: {status.total_chunks}")

        # Cache status
        if self.docs_fetcher:
            console.print("\n[yellow]Cache Status:[/yellow]")
            cache_stats = self.docs_fetcher.get_cache_stats()
            console.print(f"  Valid entries: {cache_stats['valid_entries']}")
            console.print(f"  Expired entries: {cache_stats['expired_entries']}")

        console.print()

    async def _docs_detect(self) -> None:
        """Re-run project detection."""
        from penguincode_cli.docs_rag import ProjectDetector

        detector = ProjectDetector(str(self.project_dir))
        self.project_context = detector.detect()

        console.print("\n[bold cyan]Project Detection Results[/bold cyan]\n")

        if self.project_context.languages:
            console.print("[yellow]Languages:[/yellow]")
            for lang in self.project_context.languages:
                console.print(f"  - {lang.value}")

            console.print(f"\n[yellow]Libraries ({len(self.project_context.libraries)}):[/yellow]")
            for lib in self.project_context.libraries[:15]:
                version = f" ({lib.version})" if lib.version else ""
                console.print(f"  - {lib.name}{version} [{lib.language.value}]")

            if len(self.project_context.libraries) > 15:
                console.print(f"  ... and {len(self.project_context.libraries) - 15} more")
        else:
            print_info("No languages or libraries detected")

        console.print()

    async def _docs_index(self, library_name: str = "") -> None:
        """Index documentation for libraries via the server's `Index` RPC (F3)."""
        if not self.project_context:
            print_error("Run /docs detect first")
            return
        if self.knowledge_client is None:
            print_error("Not connected to the penguincode server")
            return

        from penguincode_cli.docs_rag import get_priority_docs_for_project

        if library_name:
            # Index specific library
            lib = next(
                (
                    lib_item
                    for lib_item in self.project_context.libraries
                    if lib_item.name.lower() == library_name.lower()
                ),
                None,
            )
            if not lib:
                print_error(f"Library '{library_name}' not detected in project")
                return

            libs_to_index = [lib]
        else:
            # Index priority libraries
            libs_to_index = get_priority_docs_for_project(
                self.project_context.libraries,
                self.settings.docs_rag.priority_libraries,
                self.settings.docs_rag.max_libraries_to_index,
            )

        console.print(f"\n[cyan]Indexing {len(libs_to_index)} libraries...[/cyan]\n")

        total_chunks = 0
        for lib in libs_to_index:
            console.print(f"  Fetching {lib.name}...")

            # Fetch docs
            docs = await self.docs_fetcher.fetch_library_docs(lib)

            if docs:
                # Index docs via the server
                try:
                    chunks = await self.knowledge_client.index(
                        library_name=lib.name,
                        library_version=lib.version or "",
                        language=lib.language.value,
                        doc_contents=docs,
                    )
                except KnowledgeClientError as e:
                    console.print(f"    [red]Failed: {e}[/red]")
                    continue
                total_chunks += chunks
                console.print(f"    Indexed {chunks} chunks")
            else:
                console.print("    [dim]No docs found[/dim]")

        print_success(f"Indexed {total_chunks} total chunks")

    async def _docs_search(self, query: str) -> None:
        """Search indexed documentation via the server's `Query` RPC (F3)."""
        if not query:
            print_error("Usage: /docs search <query>")
            return

        if self.knowledge_client is None:
            print_error("Not connected to the penguincode server")
            return

        console.print(f"\n[cyan]Searching:[/cyan] {query}\n")

        # Filter to project libraries only -- the `Query` RPC has no `where=` filter of its
        # own (same as the local `search()` it replaces), so filtering stays client-side.
        library_names = (
            {name.lower() for name in self.project_context.library_names}
            if self.project_context
            else None
        )

        try:
            result = await self.knowledge_client.query(query=query, n_vector=5)
        except KnowledgeClientError as e:
            print_error(f"Search failed: {e}")
            return

        hits = [
            hit
            for hit in result.vector_hits
            if not library_names or str(hit.metadata.get("library", "")).lower() in library_names
        ]

        if hits:
            for i, hit in enumerate(hits, 1):
                library = hit.metadata.get("library", "?")
                console.print(f"[bold]{i}. [{library}][/bold] (score: {hit.score:.2f})")
                # Truncate long content
                content = hit.document[:300] + "..." if len(hit.document) > 300 else hit.document
                console.print(f"   {content}\n")
        else:
            print_info("No results found")

    async def _docs_clear(self, library_name: str = "") -> None:
        """Clear indexed documentation for one library, via the server's `ClearIndex` RPC (C1).

        Usage: `/docs clear <library_name>` -- a bare language-only clear isn't exposed as a
        REPL subcommand (the CLI has no ambiguity-free way to distinguish a language name from
        a library name here); use `KnowledgeClient.clear_index(language=...)` directly for that.
        """
        if self.knowledge_client is None:
            print_error("Not connected to the penguincode server")
            return
        if not library_name:
            print_error("Usage: /docs clear <library_name>")
            return

        try:
            removed = await self.knowledge_client.clear_index(library_name=library_name)
        except KnowledgeClientError as e:
            print_error(f"Failed to clear index: {e}")
            return

        if removed:
            print_success(f"Cleared {removed} chunks for '{library_name}'")
        else:
            print_info(f"Nothing indexed for '{library_name}'")

    async def _docs_cleanup(self) -> None:
        """Remove docs for libraries no longer in project.

        Cleans up both the local doc-fetch cache (client-side) and the server-side docs
        index (via the `CleanupIndex` RPC, C1), which is told the CLI's own already-detected
        project state (`self.project_context`) -- the server has no independent way to know
        what a project still references.
        """
        if not self.project_context:
            print_error("Run /docs detect first")
            return

        # Cleanup cache (client-side -- raw doc fetch cache, not the vector index)
        cache_removed = self.docs_fetcher.cleanup_unused_libraries(self.project_context.libraries)

        if cache_removed:
            console.print("\n[cyan]Cleanup Results:[/cyan]")
            for lib, count in cache_removed.items():
                console.print(f"  Cache: removed {count} pages for {lib}")

        index_removed: dict[str, int] = {}
        if self.knowledge_client is not None:
            try:
                index_removed = await self.knowledge_client.cleanup_index(
                    current_libraries=[
                        LibraryRef(
                            name=lib.name, language=lib.language.value, version=lib.version or ""
                        )
                        for lib in self.project_context.libraries
                    ],
                    current_languages=[lang.value for lang in self.project_context.languages],
                )
            except KnowledgeClientError as e:
                print_error(f"Failed to clean up server-side index: {e}")

        if index_removed:
            console.print("\n[cyan]Server-side Index Cleanup:[/cyan]")
            for name, count in index_removed.items():
                console.print(f"  Index: removed {count} chunks for {name}")

        if not cache_removed and not index_removed:
            print_info("Nothing to clean up")
        else:
            console.print()

    def _detect_languages_in_message(self, message: str) -> list:
        """Detect programming languages mentioned in user message.

        Args:
            message: User's message

        Returns:
            List of detected language names
        """
        msg_lower = message.lower()
        detected = []

        # Language patterns to detect
        language_patterns = {
            "python": ["python", "py ", ".py", "pip ", "pytest", "django", "flask", "fastapi"],
            "javascript": ["javascript", "js ", ".js", "node", "npm ", "react", "vue", "express"],
            "typescript": ["typescript", "ts ", ".ts", ".tsx"],
            "go": [" go ", "golang", ".go", "go mod", "go build"],
            "rust": ["rust", ".rs", "cargo ", "rustc"],
            "hcl": ["terraform", "opentofu", "tofu ", ".tf", "hcl"],
            "ansible": [
                "ansible",
                "playbook",
                "ansible-playbook",
                ".yml playbook",
                ".yaml playbook",
            ],
            "ruby": ["ruby", "rails", "gem ", "rake", "bundler", "sinatra", "rspec", "erb"],
            "php": ["php", "laravel", "symfony", "composer", "artisan", "blade", "eloquent"],
            "dart": ["dart", "flutter", "widget", "pubspec", "riverpod", "provider", "bloc"],
        }

        for lang, patterns in language_patterns.items():
            if any(p in msg_lower for p in patterns):
                detected.append(lang)

        return detected

    async def handle_chat(self, message: str) -> None:
        """
        Handle regular chat messages by sending to the chat agent.

        The chat agent decides whether to respond directly or spawn
        specialized agents for code/file operations.
        """
        # Save user message to session
        self.session.add_message("user", message)

        console.print()  # Add some spacing

        try:
            # On-demand language detection and indexing
            if (
                self.settings.docs_rag.auto_detect_on_request
                and self.settings.docs_rag.auto_index_on_request
            ):
                detected_langs = self._detect_languages_in_message(message)
                for lang in detected_langs:
                    await self._ensure_language_indexed(lang)

            # Inject documentation context if available
            if self.context_injector and self.project_context:
                should_inject = await self.context_injector.should_inject_context(
                    message, self.project_context
                )
                if should_inject:
                    context = await self.context_injector.get_relevant_context(
                        self.scope_ctx, message, self.project_context
                    )
                    if context:
                        # Augment the chat agent's system prompt temporarily
                        original_prompt = self.chat_agent.system_prompt
                        self.chat_agent.system_prompt = (
                            self.context_injector.build_augmented_prompt(original_prompt, context)
                        )
                        console.print("[dim](using documentation context)[/dim]")

            # Use chat agent to process the message
            response = await self.chat_agent.process(message)

            # Restore original prompt if modified
            if hasattr(self, "_original_prompt"):
                self.chat_agent.system_prompt = self._original_prompt

            # Display the response
            console.print("\n[bold blue]Assistant:[/bold blue]")
            console.print(response)
            console.print()

            # Save assistant response to session
            if response:
                self.session.add_message("assistant", response)

        except Exception as e:
            console.print(f"\n[red]Error: {str(e)}[/red]\n")
            console.print("[dim]Make sure Ollama is running: ollama serve[/dim]\n")

    async def run(self) -> None:
        """Run the REPL loop."""
        console.print("[bold cyan]PenguinCode Chat[/bold cyan]")
        console.print(f"Project: {self.project_dir}")
        console.print(
            f"Models: orchestration={self.settings.models.orchestration}, execution={self.settings.models.execution}"
        )
        console.print("\nType [bold]/help[/bold] for commands, [bold]/exit[/bold] to quit\n")

        # Set up prompt_toolkit session with history and styling
        history_file = Path.home() / ".config" / "penguincode" / "history"
        history_file.parent.mkdir(parents=True, exist_ok=True)

        prompt_style = Style.from_dict(
            {
                "prompt": "bold ansigreen",
            }
        )

        session: PromptSession = PromptSession(
            history=FileHistory(str(history_file)),
            auto_suggest=AutoSuggestFromHistory(),
            style=prompt_style,
            enable_history_search=True,  # Ctrl+R to search history
        )

        # Track consecutive Ctrl+C presses
        interrupt_count = 0

        while True:
            try:
                # Get user input with full readline support
                prompt_text = f"You ({self.active_skill}): " if self.active_skill else "You: "
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: session.prompt(prompt_text)
                )

                # Reset interrupt count on successful input
                interrupt_count = 0

                if not user_input.strip():
                    continue

                # Handle commands
                if user_input.startswith("/"):
                    should_continue = await self.handle_command(user_input)
                    if not should_continue:
                        break
                else:
                    # Regular chat message - send to orchestrator
                    await self.handle_chat(user_input)

            except EOFError:
                # Ctrl+D - exit
                break
            except KeyboardInterrupt:
                # Ctrl+C handling
                interrupt_count += 1
                if interrupt_count >= 2:
                    console.print()
                    break
                console.print("\n[yellow]Press Ctrl+C again to exit[/yellow]")
                continue
            except Exception as e:
                if "interrupt" in str(e).lower():
                    interrupt_count += 1
                    if interrupt_count >= 2:
                        break
                    console.print("\n[yellow]Press Ctrl+C again to exit[/yellow]")
                    continue
                print_error(f"Error: {str(e)}")
                interrupt_count = 0
                continue

        print_info("Goodbye!")


async def start_repl(project_dir: str = ".", config_path: str = "config.yaml") -> None:
    """
    Start the REPL session.

    Args:
        project_dir: Project directory
        config_path: Path to config file
    """
    async with REPLSession(project_dir, config_path) as repl:
        await repl.run()
