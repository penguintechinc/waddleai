"""Interactive REPL loop for PenguinCode chat."""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.table import Table

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
    from penguincode_cli.tools.memory import MemoryManager


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

        # Docs RAG components (initialized if enabled)
        self.project_context = None
        self.docs_fetcher = None
        self.docs_indexer = None
        self.context_injector = None

        # Memory manager for cross-session persistence (initialized in async context)
        self.memory_manager: MemoryManager | None = None

        # Skill system
        self.skill_loader = SkillLoader()
        self.skill_loader.discover()
        self.active_skill: str | None = None

    async def __aenter__(self):
        """Async context manager entry."""
        # Lazy import agents to avoid circular import
        from penguincode_cli.agents import ChatAgent, ExecutorAgent, ExplorerAgent
        from penguincode_cli.tools.memory import MemoryManager

        # Initialize Ollama client
        self.ollama_client = OllamaClient(
            base_url=self.settings.ollama.api_url,
            timeout=self.settings.ollama.timeout,
        )
        await self.ollama_client.__aenter__()

        # Initialize memory manager for cross-session persistence
        if self.settings.memory.enabled:
            try:
                self.memory_manager = MemoryManager(
                    config=self.settings.memory,
                    ollama_url=self.settings.ollama.api_url,
                    llm_model=self.settings.models.orchestration,
                )
                if self.memory_manager.is_enabled():
                    print_info("Memory layer initialized")
            except Exception as e:
                print_info(f"Memory layer unavailable: {e}")
                self.memory_manager = None

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
                    print_info(f"MCP: {len(mcp_tools[0])} tool(s) from {len(self.settings.mcp.servers)} server(s)")
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
                        self.settings.mcp.servers.append(MCPServerConfig(
                            name=srv["name"],
                            enabled=srv.get("enabled", True),
                            transport=srv.get("transport", "stdio"),
                            command=srv.get("command", ""),
                            args=srv.get("args", []),
                            url=srv.get("url", ""),
                            env=srv.get("env", {}),
                            headers=srv.get("headers", {}),
                            timeout=srv.get("timeout", 30),
                        ))
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
                DocumentationIndexer,
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

            self.docs_indexer = DocumentationIndexer(
                collection_name=self.settings.docs_rag.collection,
                embedding_model=self.settings.memory.embedding_model,
                chunk_size=self.settings.docs_rag.chunk_size,
                chunk_overlap=self.settings.docs_rag.chunk_overlap,
                ollama_base_url=self.settings.ollama.api_url,
            )

            self.context_injector = ContextInjector(
                indexer=self.docs_indexer,
                max_context_tokens=self.settings.docs_rag.max_context_tokens,
                max_chunks=self.settings.docs_rag.max_chunks_per_query,
            )

            # Cleanup expired cache entries
            expired = self.docs_fetcher.expunge_expired()
            if expired > 0:
                print_info(f"Cleaned up {expired} expired doc cache entries")

            # Cleanup unused library docs
            if self.project_context:
                removed = self.docs_fetcher.cleanup_unused_libraries(
                    self.project_context.libraries
                )
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
        """Auto-index documentation for detected/configured languages."""
        if not self.project_context or not self.docs_fetcher or not self.docs_indexer:
            return

        from penguincode_cli.docs_rag import get_language_doc_source

        indexed_count = 0
        for lang in self.project_context.languages:
            # Check if already indexed (fresh)
            if self.docs_indexer.is_language_indexed(lang.value):
                continue

            # Get doc source for language
            doc_source = get_language_doc_source(lang)
            if not doc_source:
                continue

            console.print(f"[dim]Indexing {lang.value} documentation...[/dim]")

            try:
                # Fetch language docs
                docs = await self.docs_fetcher.fetch_language_docs(lang)
                if docs:
                    chunks = await self.docs_indexer.index_language(lang, docs)
                    indexed_count += chunks
                    console.print(f"[dim]  Indexed {chunks} chunks for {lang.value}[/dim]")
            except Exception as e:
                console.print(f"[dim]  Failed to index {lang.value}: {e}[/dim]")

        if indexed_count > 0:
            print_info(f"Auto-indexed {indexed_count} documentation chunks")

    async def _ensure_language_indexed(self, language: str) -> bool:
        """Ensure a language's documentation is indexed (on-demand).

        Args:
            language: Language name (e.g., "python", "javascript")

        Returns:
            True if indexed successfully or already indexed
        """
        if not self.docs_fetcher or not self.docs_indexer:
            return False

        from penguincode_cli.docs_rag import Language, get_language_doc_source

        # Check if already indexed
        if self.docs_indexer.is_language_indexed(language):
            return True

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
                chunks = await self.docs_indexer.index_language(lang_enum, docs)
                console.print(f"[dim]  Indexed {chunks} chunks[/dim]")
                return True
        except Exception as e:
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
                desc = info.description[:60] + "..." if len(info.description) > 60 else info.description
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
                    skill_name, combined, chain=chain_names,
                    model=chain[0].model,
                )
            self.active_skill = skill_name
            model_msg = f" (model: {chain[0].model})" if chain[0].model else ""
            print_success(
                f"Skill chain activated: {' → '.join(chain_names)}{model_msg}"
            )
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
        import yaml as _yaml

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
        console.print(f"[yellow]Models:[/yellow]")
        console.print(f"  orchestration: {s.models.orchestration}")
        console.print(f"  execution:     {s.models.execution}")
        console.print(f"  planning:      {s.models.planning}")
        console.print(f"  research:      {s.models.research}")
        console.print(f"[yellow]Context window:[/yellow] {s.defaults.context_window}")
        console.print(f"[yellow]Max agents:[/yellow]     {s.regulators.max_concurrent_agents}")
        console.print(f"[yellow]Agent timeout:[/yellow]  {s.regulators.agent_timeout_seconds}s")
        console.print(f"[yellow]Memory:[/yellow]         {'enabled' if s.memory.enabled else 'disabled'}")
        console.print(f"[yellow]Docs RAG:[/yellow]       {'enabled' if s.docs_rag.enabled else 'disabled'}")
        console.print()
        console.print("[dim]Use /config show for full config, /config set <key> <value> to modify[/dim]")
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
                    k: ("****" if k in sensitive_keys and v else _mask(v))
                    for k, v in d.items()
                }
            if isinstance(d, list):
                return [_mask(item) for item in d]
            return d

        masked = _mask(data)
        output = _yaml.dump(masked, default_flow_style=False, sort_keys=False)
        console.print(f"\n[bold cyan]Full Configuration:[/bold cyan]\n")
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

            console.print(f"\n[yellow]Detected Libraries ({len(self.project_context.libraries)}):[/yellow]")
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

        # Index status
        if self.docs_indexer:
            console.print("\n[yellow]Index Status:[/yellow]")
            status = self.docs_indexer.get_index_status()

            if status["libraries"]:
                table = Table(show_header=True)
                table.add_column("Library")
                table.add_column("Chunks")
                table.add_column("Indexed")
                table.add_column("Status")

                for lib, info in status["libraries"].items():
                    status_str = "[red]expired[/red]" if info["is_expired"] else "[green]valid[/green]"
                    table.add_row(
                        lib,
                        str(info["chunk_count"]),
                        info["indexed_at"][:10],
                        status_str,
                    )
                console.print(table)
            else:
                print_info("No libraries indexed")

            console.print(f"\nTotal chunks: {status['total_chunks']}")

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
        """Index documentation for libraries."""
        if not self.project_context:
            print_error("Run /docs detect first")
            return

        from penguincode_cli.docs_rag import get_priority_docs_for_project

        if library_name:
            # Index specific library
            lib = next(
                (lib_item for lib_item in self.project_context.libraries if lib_item.name.lower() == library_name.lower()),
                None
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
                # Index docs
                chunks = await self.docs_indexer.index_library(lib, docs)
                total_chunks += chunks
                console.print(f"    Indexed {chunks} chunks")
            else:
                console.print("    [dim]No docs found[/dim]")

        print_success(f"Indexed {total_chunks} total chunks")

    async def _docs_search(self, query: str) -> None:
        """Search indexed documentation."""
        if not query:
            print_error("Usage: /docs search <query>")
            return

        if not self.docs_indexer:
            print_error("Docs indexer not initialized")
            return

        console.print(f"\n[cyan]Searching:[/cyan] {query}\n")

        # Filter to project libraries only
        library_names = self.project_context.library_names if self.project_context else None

        results = await self.docs_indexer.search(
            query=query,
            libraries=library_names,
            limit=5,
        )

        if results:
            for i, result in enumerate(results, 1):
                console.print(f"[bold]{i}. [{result.library}][/bold] (score: {result.relevance_score:.2f})")
                # Truncate long content
                content = result.content[:300] + "..." if len(result.content) > 300 else result.content
                console.print(f"   {content}\n")
        else:
            print_info("No results found")

    async def _docs_clear(self, library_name: str = "") -> None:
        """Clear indexed documentation."""
        if not self.docs_indexer:
            print_error("Docs indexer not initialized")
            return

        if library_name:
            count = await self.docs_indexer.clear_library_index(library_name)
            print_success(f"Cleared {count} chunks for {library_name}")
        else:
            # Clear all
            status = self.docs_indexer.get_index_status()
            total = 0
            for lib in list(status["libraries"].keys()):
                count = await self.docs_indexer.clear_library_index(lib)
                total += count
            print_success(f"Cleared {total} total chunks")

    async def _docs_cleanup(self) -> None:
        """Remove docs for libraries no longer in project."""
        if not self.project_context:
            print_error("Run /docs detect first")
            return

        # Cleanup cache
        cache_removed = self.docs_fetcher.cleanup_unused_libraries(
            self.project_context.libraries
        )

        # Cleanup index
        index_removed = await self.docs_indexer.cleanup_unused(
            self.project_context.libraries,
            self.project_context.languages,
        )

        if cache_removed or index_removed:
            console.print("\n[cyan]Cleanup Results:[/cyan]")
            if cache_removed:
                for lib, count in cache_removed.items():
                    console.print(f"  Cache: removed {count} pages for {lib}")
            if index_removed:
                for lib, count in index_removed.items():
                    console.print(f"  Index: removed {count} chunks for {lib}")
            console.print()
        else:
            print_info("Nothing to clean up")

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
            "ansible": ["ansible", "playbook", "ansible-playbook", ".yml playbook", ".yaml playbook"],
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
            if self.settings.docs_rag.auto_detect_on_request and self.settings.docs_rag.auto_index_on_request:
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
                        message, self.project_context
                    )
                    if context:
                        # Augment the chat agent's system prompt temporarily
                        original_prompt = self.chat_agent.system_prompt
                        self.chat_agent.system_prompt = self.context_injector.build_augmented_prompt(
                            original_prompt, context
                        )
                        console.print("[dim](using documentation context)[/dim]")

            # Use chat agent to process the message
            response = await self.chat_agent.process(message)

            # Restore original prompt if modified
            if hasattr(self, '_original_prompt'):
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
        console.print(f"Models: orchestration={self.settings.models.orchestration}, execution={self.settings.models.execution}")
        console.print("\nType [bold]/help[/bold] for commands, [bold]/exit[/bold] to quit\n")

        # Set up prompt_toolkit session with history and styling
        history_file = Path.home() / ".config" / "penguincode" / "history"
        history_file.parent.mkdir(parents=True, exist_ok=True)

        prompt_style = Style.from_dict({
            'prompt': 'bold ansigreen',
        })

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
                prompt_text = (
                    f"You ({self.active_skill}): "
                    if self.active_skill
                    else "You: "
                )
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
