"""Chat agent - the main orchestrating agent for PenguinCode.

This is the primary agent that users interact with. It serves two roles:

1. **Knowledge Base** - Answers general questions directly without spawning agents
2. **Foreman** - Delegates work to specialized agents, reviews their output,
   and can dispatch follow-up agents to fix issues if needed

For complex tasks, it uses the PlannerAgent to break down work and can
execute multiple agents in parallel (up to max_concurrent_agents).
"""

import asyncio
import json
import logging

from penguincode_cli.config.settings import Settings
from penguincode_cli.core.debug import (
    debug,
    log_agent_result,
    log_agent_spawn,
    log_error,
    log_intent_detection,
    log_llm_request,
    log_llm_response,
    warning,
)
from penguincode_cli.ollama import Message, OllamaClient
from penguincode_cli.tools.mcp import MCPToolManager
from penguincode_cli.ui import console

from .intent import detect_user_intent, estimate_complexity, suggest_skill
from .prompts import (
    AGENT_TOOLS,
    CHAT_SYSTEM_PROMPT,
    ESCALATION_PROMPT,
    REVIEW_PROMPT,
)

logger = logging.getLogger(__name__)


class AgentSemaphore:
    """Dynamic semaphore for controlling concurrent agent execution."""

    def __init__(self, max_concurrent: int = 5):
        self._max = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_count = 0
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Acquire a slot for agent execution."""
        await self._semaphore.acquire()
        async with self._lock:
            self._active_count += 1

    def release(self):
        """Release a slot after agent completion."""
        self._semaphore.release()
        asyncio.create_task(self._decrement_count())

    async def _decrement_count(self):
        async with self._lock:
            self._active_count -= 1

    @property
    def active_agents(self) -> int:
        return self._active_count

    @property
    def available_slots(self) -> int:
        return self._max - self._active_count

    def adjust_max(self, new_max: int):
        """Dynamically adjust max concurrent agents (for resource regulation)."""
        # Note: This is a simplified version. Full implementation would
        # need to handle in-flight tasks carefully.
        self._max = max(1, new_max)


class ChatAgent:
    """Main chat agent - knowledge base and job foreman.

    This agent understands user requests and either:
    1. Answers directly (knowledge base role)
    2. Delegates to agents, reviews their work, and supervises fixes (foreman role)
    3. Uses planner for complex tasks, then executes the plan with parallel agents

    Features:
    - Conversation history with auto-compaction
    - Long-term memory via mem0 (cross-session persistence)
    - Context injection from relevant memories
    """

    # Context management constants
    # These are percentages of the model's context window
    CONTEXT_THRESHOLD_PERCENT = 70  # Compact when history exceeds this % of context
    CONTEXT_RESERVE_PERCENT = 30    # Reserve this % for new messages + response
    MAX_MEMORY_RESULTS = 5          # Max memories to inject
    # Approximate chars per token (for estimation)
    CHARS_PER_TOKEN = 4

    def __init__(
        self,
        ollama_client: OllamaClient,
        settings: Settings,
        project_dir: str,
        memory_manager=None,
        session_id: str = None,
    ):
        self.client = ollama_client
        self.settings = settings
        self.project_dir = project_dir
        self.model = settings.models.orchestration

        # Conversation history with compaction support
        self.conversation_history: list[Message] = []
        self.conversation_summary: str = ""  # Summary of compacted history

        # Memory integration (cross-session persistence)
        self.memory_manager = memory_manager
        self.session_id = session_id or "default"

        # Lazy-loaded specialized agents
        self._explorer_agent = None
        self._executor_agent = None
        self._planner_agent = None
        self._researcher_agent = None

        # System prompt
        self.system_prompt = CHAT_SYSTEM_PROMPT.format(project_dir=project_dir)

        # Skill system
        self.active_skill: str | None = None
        self.skill_content: str | None = None
        self.skill_chain: list[str] = []
        self._saved_model: str | None = None  # Saved model for skill overrides

        # Max supervision iterations (prevent infinite loops)
        self.max_supervision_rounds = 3

        # Agent concurrency control
        max_agents = settings.regulators.max_concurrent_agents
        self.agent_semaphore = AgentSemaphore(max_concurrent=max_agents)
        self.agent_timeout = settings.regulators.agent_timeout_seconds

        # MCP tool manager — lazy discovers tools from configured MCP servers
        self._mcp_manager = MCPToolManager(settings.mcp)
        self._mcp_tools: tuple[dict, list] | None = None

    def _get_explorer_agent(self, lite: bool = False):
        """
        Get explorer agent, optionally using lightweight model.

        Args:
            lite: If True, use lightweight model for simple searches
        """
        # For lite mode, always create fresh with lite model
        if lite:
            from .explorer import ExplorerAgent
            model = getattr(self.settings.models, 'exploration_lite', self.settings.models.orchestration)
            return ExplorerAgent(
                ollama_client=self.client,
                working_dir=self.project_dir,
                model=model,
                mcp_tools=self._mcp_tools,
            )

        # Standard explorer (cached)
        if self._explorer_agent is None:
            from .explorer import ExplorerAgent
            model = getattr(self.settings.models, 'exploration', self.settings.models.orchestration)
            self._explorer_agent = ExplorerAgent(
                ollama_client=self.client,
                working_dir=self.project_dir,
                model=model,
                mcp_tools=self._mcp_tools,
            )
        return self._explorer_agent

    def _get_executor_agent(self, lite: bool = False):
        """
        Get executor agent, optionally using lightweight model.

        Args:
            lite: If True, use lightweight model for simple edits
        """
        # For lite mode, always create fresh with lite model
        if lite:
            from .executor import ExecutorAgent
            model = getattr(self.settings.models, 'execution_lite', self.settings.models.execution)
            console.print(f"[dim](using lite model: {model})[/dim]")
            return ExecutorAgent(
                ollama_client=self.client,
                working_dir=self.project_dir,
                model=model,
                mcp_tools=self._mcp_tools,
            )

        # Standard executor (cached)
        if self._executor_agent is None:
            from .executor import ExecutorAgent
            self._executor_agent = ExecutorAgent(
                ollama_client=self.client,
                working_dir=self.project_dir,
                model=self.settings.models.execution,
                mcp_tools=self._mcp_tools,
            )
        return self._executor_agent

    def _get_planner_agent(self):
        """Lazy-load planner agent."""
        if self._planner_agent is None:
            from .planner import PlannerAgent
            self._planner_agent = PlannerAgent(
                ollama_client=self.client,
                model=self.settings.models.planning,
            )
        return self._planner_agent

    def _get_researcher_agent(self):
        """Lazy-load researcher agent."""
        if self._researcher_agent is None:
            from .researcher import ResearcherAgent
            # Get research model from settings, fallback to orchestration model
            model = getattr(self.settings.models, 'research', self.settings.models.orchestration)
            self._researcher_agent = ResearcherAgent(
                ollama_client=self.client,
                research_config=self.settings.research,
                working_dir=self.project_dir,
                model=model,
                mcp_tools=self._mcp_tools,
            )
        return self._researcher_agent

    async def _get_mcp_tools(self) -> tuple[dict, list] | None:
        """Lazy-discover MCP tools. Returns (tools_dict, tool_defs) or None."""
        if self._mcp_tools is not None:
            return self._mcp_tools

        try:
            tools, defs = await self._mcp_manager.get_tools()
            if tools:
                self._mcp_tools = (tools, defs)
                logger.info("Discovered %d MCP tool(s)", len(tools))
                return self._mcp_tools
        except Exception as exc:
            logger.warning("MCP tool discovery failed: %s", exc)

        return None

    async def shutdown(self) -> None:
        """Shutdown MCP tool manager (stop stdio server processes)."""
        await self._mcp_manager.shutdown()

    async def _spawn_agent(
        self,
        agent_type: str,
        task: str,
        force_lite: bool = False,
        force_full: bool = False,
    ) -> tuple[bool, str]:
        """
        Spawn a specialized agent to handle a task.

        Automatically selects lite or full model based on task complexity,
        unless force_lite or force_full is specified.

        Args:
            agent_type: "explorer", "executor", or "planner"
            task: Task description
            force_lite: Force use of lightweight model
            force_full: Force use of full model

        Returns:
            Tuple of (success, output)
        """
        # Determine model tier based on complexity
        complexity = estimate_complexity(task)
        use_lite = force_lite or (complexity == "simple" and not force_full)

        # Log the agent spawn
        log_agent_spawn(agent_type, task, complexity)

        if agent_type == "explorer":
            tier = "lite" if use_lite else "standard"
            console.print(f"[cyan]> Spawning explorer agent ({tier})...[/cyan]")
            agent = self._get_explorer_agent(lite=use_lite)
        elif agent_type == "executor":
            tier = "lite" if use_lite else "full"
            console.print(f"[cyan]> Spawning executor agent ({tier})...[/cyan]")
            agent = self._get_executor_agent(lite=use_lite)
        elif agent_type == "planner":
            console.print("[cyan]> Spawning planner agent...[/cyan]")
            agent = self._get_planner_agent()
        elif agent_type == "researcher":
            console.print("[cyan]> Spawning researcher agent...[/cyan]")
            agent = self._get_researcher_agent()
        else:
            warning(f"Unknown agent type requested: {agent_type}")
            return False, f"Unknown agent type: {agent_type}"

        try:
            # Acquire semaphore slot
            await self.agent_semaphore.acquire()
            try:
                # Run with timeout
                result = await asyncio.wait_for(
                    agent.run(task),
                    timeout=self.agent_timeout
                )

                # Check for escalation request
                if result.needs_escalation:
                    console.print("[yellow]> Agent requesting orchestrator help[/yellow]")
                    # Return special escalation result
                    return False, f"ESCALATION_NEEDED:{result.escalation_context}"

                success = result.success
                # On failure, combine error + summary (output has the summary from base agent)
                if result.success:
                    output = result.output
                else:
                    error_msg = result.error or "Unknown error"
                    output = f"{error_msg}\n\n{result.output}" if result.output else error_msg

                # Log the result
                log_agent_result(agent_type, success, output)
                return success, output
            finally:
                self.agent_semaphore.release()
        except TimeoutError:
            warning(f"Agent {agent_type} timed out after {self.agent_timeout}s")
            return False, f"Agent timed out after {self.agent_timeout} seconds"
        except Exception as e:
            log_error(f"_spawn_agent({agent_type})", e)
            return False, f"Agent failed: {str(e)}"

    async def _spawn_agents_parallel(
        self,
        tasks: list[tuple[str, str]]  # List of (agent_type, task)
    ) -> list[tuple[bool, str]]:
        """
        Spawn multiple agents in parallel, respecting max_concurrent_agents.

        Args:
            tasks: List of (agent_type, task_description) tuples

        Returns:
            List of (success, output) tuples in same order as input
        """
        console.print(f"[cyan]> Spawning {len(tasks)} agents (max {self.agent_semaphore._max} concurrent)...[/cyan]")

        async def run_task(agent_type: str, task: str, index: int) -> tuple[int, bool, str]:
            success, output = await self._spawn_agent(agent_type, task)
            return index, success, output

        # Create tasks
        coroutines = [
            run_task(agent_type, task, i)
            for i, (agent_type, task) in enumerate(tasks)
        ]

        # Run with concurrency control (semaphore is checked in _spawn_agent)
        results_unordered = await asyncio.gather(*coroutines, return_exceptions=True)

        # Sort back to original order and handle exceptions
        results = [None] * len(tasks)
        for result in results_unordered:
            if isinstance(result, Exception):
                # Find first empty slot for exception
                for i in range(len(results)):
                    if results[i] is None:
                        results[i] = (False, str(result))
                        break
            else:
                index, success, output = result
                results[index] = (success, output)

        return results

    async def _execute_plan(self, plan, user_request: str) -> str:
        """
        Execute a plan by running agents according to parallel groups.

        Args:
            plan: Plan object from PlannerAgent
            user_request: Original user request

        Returns:
            Combined results from all steps
        """

        console.print(f"\n[bold cyan]Executing plan ({len(plan.steps)} steps)...[/bold cyan]")

        step_results: dict[int, tuple[bool, str]] = {}
        all_outputs = []

        for group_num, group in enumerate(plan.parallel_groups, 1):
            # Get steps for this group
            group_steps = [s for s in plan.steps if s.step_num in group]

            if not group_steps:
                continue

            console.print(f"\n[cyan]> Group {group_num}: executing {len(group_steps)} step(s) in parallel[/cyan]")

            # Build tasks for parallel execution
            tasks = [(step.agent_type, step.description) for step in group_steps]

            # Execute in parallel
            results = await self._spawn_agents_parallel(tasks)

            # Store results and update plan file
            for step, (success, output) in zip(group_steps, results, strict=False):
                step_results[step.step_num] = (success, output)
                status = "[green]✓[/green]" if success else "[red]✗[/red]"
                console.print(f"  {status} Step {step.step_num}: {step.description[:50]}...")
                all_outputs.append(f"### Step {step.step_num}: {step.description}\n{output}")

                # Persist step status to disk
                if hasattr(plan, 'plan_file') and plan.plan_file:
                    planner = self._get_planner_agent()
                    step_status = "completed" if success else "failed"
                    planner.update_plan_step(plan, step.step_num, step_status)

        # Combine outputs
        combined = "\n\n".join(all_outputs)

        # Review the overall results
        return await self._review_plan_execution(user_request, plan, combined, step_results)

    async def _review_plan_execution(
        self,
        user_request: str,
        plan,
        combined_output: str,
        step_results: dict[int, tuple[bool, str]]
    ) -> str:
        """Review the results of plan execution."""
        failed_steps = [num for num, (success, _) in step_results.items() if not success]

        if failed_steps:
            console.print(f"[yellow]> {len(failed_steps)} step(s) failed, reviewing...[/yellow]")

        # Use foreman to review and potentially fix
        return await self._review_and_supervise(
            user_request,
            "plan_execution",
            combined_output,
            len(failed_steps) == 0,
            round_num=1
        )

    def _parse_tool_calls(self, response_text: str) -> list[dict]:
        """Parse tool calls from response text."""
        tool_calls = []
        valid_tools = {"spawn_explorer", "spawn_executor", "spawn_planner", "spawn_researcher"}

        try:
            if "{" in response_text and "}" in response_text:
                start = 0
                while True:
                    start = response_text.find("{", start)
                    if start == -1:
                        break

                    brace_count = 0
                    end = start
                    for i, char in enumerate(response_text[start:], start):
                        if char == "{":
                            brace_count += 1
                        elif char == "}":
                            brace_count -= 1
                            if brace_count == 0:
                                end = i + 1
                                break

                    if end > start:
                        try:
                            json_str = response_text[start:end]
                            data = json.loads(json_str)
                            if "name" in data and data["name"] in valid_tools:
                                tool_calls.append(data)
                        except json.JSONDecodeError:
                            pass
                    start = end
        except Exception:
            pass

        return tool_calls

    async def _call_llm(self, messages: list[Message], use_tools: bool = True, timeout: float = 60.0) -> tuple[str, list[dict]]:
        """Call the LLM and return response text and tool calls.

        Note: Most local models don't support Ollama's native tool calling API.
        We don't pass tools to avoid empty responses, and instead rely on
        JSON parsing from the text response. The system prompt instructs the
        model to output JSON tool calls.

        Models that DO support native tools: llama3.1, mistral-nemo, firefunction-v2, command-r+
        Models that DON'T: llama3.2, qwen2.5-coder, codellama, deepseek-coder
        """
        response_text = ""
        tool_calls = []

        # Check if model supports native tool calling
        # See: https://ollama.com/search?c=tools for full list
        # See: https://ollama.com/blog/tool-support for implementation details
        native_tool_models = {
            # Llama family
            "llama3.1", "llama3.2", "llama3.3", "llama4",
            # Mistral family
            "mistral", "mistral-nemo", "mistral-small", "mistral-large", "mixtral",
            # Cohere Command-R family
            "command-r", "command-r-plus", "command-r7b",
            # Qwen family
            "qwen2.5", "qwen2.5-coder", "qwen3",
            # Others
            "firefunction-v2", "hermes3",
        }
        model_base = self.model.split(":")[0].lower()
        supports_native_tools = any(m in model_base for m in native_tool_models)

        # Only pass tools if model supports them AND caller wants tools
        pass_tools = use_tools and supports_native_tools

        # Debug logging
        log_llm_request(self.model, messages, AGENT_TOOLS if pass_tools else None)

        try:
            async with asyncio.timeout(timeout):
                async for chunk in self.client.chat(
                    model=self.model,
                    messages=messages,
                    tools=AGENT_TOOLS if pass_tools else None,
                    stream=True,
                ):
                    if chunk.message and chunk.message.content:
                        response_text += chunk.message.content

                    # Check for tool_calls in ANY chunk, not just done=true
                    # Ollama sends tool_calls in early chunks with done=false
                    if hasattr(chunk, "message") and chunk.message:
                        msg = chunk.message
                        if hasattr(msg, "tool_calls") and msg.tool_calls:
                            # Convert ToolCall objects to dict format
                            for tc in msg.tool_calls:
                                func = tc.function if hasattr(tc, 'function') else tc
                                tool_calls.append({
                                    "name": func.get("name", "") if isinstance(func, dict) else getattr(func, "name", ""),
                                    "arguments": func.get("arguments", {}) if isinstance(func, dict) else getattr(func, "arguments", {}),
                                })
        except TimeoutError:
            warning("LLM response timed out after %s seconds", timeout)
            console.print("[yellow]LLM response timed out[/yellow]")
            return "", []
        except Exception as e:
            log_error("_call_llm", e)
            console.print(f"[red]LLM error: {e}[/red]")
            return "", []

        # Debug log the response
        log_llm_response(response_text, tool_calls)

        # Try parsing tool calls from text if none structured
        if not tool_calls:
            tool_calls = self._parse_tool_calls(response_text)

        # Check for agent keywords in response - more robust detection
        if not tool_calls:
            response_lower = response_text.lower()

            # Check for explicit function mentions
            if "spawn_planner" in response_lower or "planner agent" in response_lower:
                tool_calls = [{"name": "spawn_planner", "arguments": {"task": ""}}]
            elif "spawn_researcher" in response_lower or "researcher agent" in response_lower:
                tool_calls = [{"name": "spawn_researcher", "arguments": {"task": ""}}]
            elif "spawn_explorer" in response_lower or "explorer agent" in response_lower:
                tool_calls = [{"name": "spawn_explorer", "arguments": {"task": ""}}]
            elif "spawn_executor" in response_lower or "executor agent" in response_lower or any(kw in response_lower for kw in [
                "create the file", "write the file", "create a file",
                "write to file", "creating file", "writing file",
                "let me create", "i'll create", "i will create",
                "let me write", "i'll write", "i will write",
                "execute", "run the command", "run command",
                "add the file", "make the file",
            ]):
                tool_calls = [{"name": "spawn_executor", "arguments": {"task": ""}}]
            # Detect read/search oriented language that needs explorer
            elif any(kw in response_lower for kw in [
                "let me search", "let me look", "let me find",
                "searching for", "looking for", "i'll search",
                "read the file", "check the file", "examine",
            ]):
                tool_calls = [{"name": "spawn_explorer", "arguments": {"task": ""}}]
            # Detect research/documentation lookup language
            elif any(kw in response_lower for kw in [
                "search the web", "web search", "look up documentation",
                "find documentation", "research this", "let me research",
                "i'll look up", "search online", "check the docs",
            ]):
                tool_calls = [{"name": "spawn_researcher", "arguments": {"task": ""}}]

        return response_text, tool_calls

    async def _review_and_supervise(
        self,
        user_request: str,
        agent_type: str,
        agent_output: str,
        agent_success: bool,
        round_num: int,
    ) -> str:
        """
        Review agent work and decide if follow-up is needed.

        Returns final response for the user.
        """
        if round_num >= self.max_supervision_rounds:
            console.print("[yellow]> Max supervision rounds reached[/yellow]")
            return agent_output

        # Check if this is an escalation request from the agent
        if agent_output.startswith("ESCALATION_NEEDED:"):
            escalation_context = agent_output[len("ESCALATION_NEEDED:"):]
            return await self._handle_escalation(user_request, escalation_context, round_num)

        # Build review prompt
        review_content = REVIEW_PROMPT.format(
            user_request=user_request,
            agent_type=agent_type,
            agent_output=agent_output if agent_success else f"AGENT ERROR: {agent_output}",
        )

        messages = [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=review_content),
        ]

        console.print("[dim]Reviewing work...[/dim]", end="\r")

        try:
            response_text, tool_calls = await self._call_llm(messages)
            console.print("                  ", end="\r")

            # Extract tool call info
            if tool_calls:
                tc = tool_calls[0]
                name = tc.get("name") or tc.get("function", {}).get("name")
                args = tc.get("arguments") or tc.get("function", {}).get("arguments", {})

                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                task = args.get("task", "")
                if not task:
                    task = f"Follow up on: {user_request}"

                if name == "spawn_explorer":
                    console.print("[yellow]> Foreman requesting explorer follow-up[/yellow]")
                    success, output = await self._spawn_agent("explorer", task)
                    return await self._review_and_supervise(
                        user_request, "explorer", output, success, round_num + 1
                    )
                elif name == "spawn_executor":
                    console.print("[yellow]> Foreman requesting executor follow-up[/yellow]")
                    success, output = await self._spawn_agent("executor", task)
                    return await self._review_and_supervise(
                        user_request, "executor", output, success, round_num + 1
                    )

            # No follow-up needed - return the review summary or original output
            return response_text if response_text else agent_output

        except Exception:
            console.print("                  ", end="\r")
            return agent_output  # Fall back to original output on error

    async def _handle_escalation(
        self,
        user_request: str,
        escalation_context: str,
        round_num: int,
    ) -> str:
        """
        Handle an escalation request from an agent that got stuck.

        The orchestrator will analyze the failure and reformulate the task.
        """
        console.print("[cyan]> Orchestrator analyzing escalation...[/cyan]")

        # Build escalation prompt for the orchestrator
        escalation_content = ESCALATION_PROMPT.format(
            user_request=user_request,
            escalation_context=escalation_context,
        )

        messages = [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=escalation_content),
        ]

        try:
            response_text, tool_calls = await self._call_llm(messages)

            # Extract tool call info
            if tool_calls:
                tc = tool_calls[0]
                name = tc.get("name") or tc.get("function", {}).get("name")
                args = tc.get("arguments") or tc.get("function", {}).get("arguments", {})

                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                task = args.get("task", "")
                if not task:
                    # If no task provided, try to extract from response
                    task = user_request

                if name == "spawn_planner":
                    console.print("[cyan]> Orchestrator using planner to break down task[/cyan]")
                    success, output = await self._spawn_agent("planner", task)
                    if success:
                        # Parse and execute the plan
                        planner = self._get_planner_agent()
                        plan = planner._parse_plan(output)
                        if plan.steps:
                            plan.user_request = user_request
                            planner.save_plan(plan, self.project_dir)
                            return await self._execute_plan(plan, user_request)
                    return output

                elif name == "spawn_explorer":
                    console.print("[cyan]> Orchestrator gathering more info first[/cyan]")
                    success, output = await self._spawn_agent("explorer", task)
                    return await self._review_and_supervise(
                        user_request, "explorer", output, success, round_num + 1
                    )

                elif name == "spawn_executor":
                    console.print("[cyan]> Orchestrator retrying with reformulated task[/cyan]")
                    # Force full model for retry after escalation
                    success, output = await self._spawn_agent("executor", task, force_full=True)
                    return await self._review_and_supervise(
                        user_request, "executor", output, success, round_num + 1
                    )

            # If orchestrator just responds with text, return it
            if response_text:
                return response_text

            return "The task could not be completed. The orchestrator was unable to find a solution."

        except Exception as e:
            log_error("_handle_escalation", e)
            return f"Escalation handling failed: {str(e)}"

    async def process(self, user_message: str) -> str:
        """
        Process a user message.

        The chat agent will either:
        1. Respond directly (knowledge base role)
        2. Delegate to agents and supervise (foreman role)
        3. Use planner for complex tasks, then execute with parallel agents

        Context management:
        - Searches long-term memory for relevant context
        - Includes conversation summary if history was compacted
        - Auto-compacts history when approaching context window limit
        - Extracts and stores important facts after each exchange
        """
        # Check if we need to compact history before processing
        if self._needs_compaction():
            await self._compact_history()

        # Search long-term memory for relevant context
        memories = await self._search_memories(user_message)

        # Build context from memories and summary
        context = self._build_context_with_memories(memories, self.conversation_summary)

        # Build system prompt with context and active skill
        system_content = self.system_prompt
        if self.skill_content:
            system_content = (
                f"# Active Skill: {self.active_skill}\n\n"
                f"{self.skill_content}\n\n---\n\n{system_content}"
            )
        if context:
            system_content = f"{context}---\n\n{system_content}"

        messages = [
            Message(role="system", content=system_content),
        ]
        messages.extend(self.conversation_history[-10:])
        messages.append(Message(role="user", content=user_message))

        # Suggest a skill if none is active
        if not self.active_skill:
            suggested = suggest_skill(user_message)
            if suggested:
                console.print(f"[dim](tip: /skill {suggested} might help here)[/dim]")

        console.print("[dim]Routing request...[/dim]")

        try:
            response_text, tool_calls = await self._call_llm(messages)

            # Debug: show what we got back
            if response_text:
                console.print(f"[dim]LLM response: {response_text[:100]}{'...' if len(response_text) > 100 else ''}[/dim]")

            # If no tool calls from LLM, try to detect intent from user message
            if not tool_calls:
                user_intent = detect_user_intent(user_message)
                if user_intent:
                    # Upgrade complex executor tasks to planner to avoid
                    # overwhelming smaller models with multi-step work
                    if user_intent == "spawn_executor":
                        complexity = estimate_complexity(user_message)
                        if complexity == "complex":
                            user_intent = "spawn_planner"
                            console.print("[dim](complex task detected — routing through planner)[/dim]")

                    log_intent_detection(user_message, user_intent)
                    console.print(f"[dim](detected intent: {user_intent})[/dim]")
                    tool_calls = [{"name": user_intent, "arguments": {"task": user_message}}]

            # If tool calls, spawn agents and supervise
            if tool_calls:
                tc = tool_calls[0]
                name = tc.get("name") or tc.get("function", {}).get("name")
                args = tc.get("arguments") or tc.get("function", {}).get("arguments", {})

                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                task = args.get("task", user_message)

                if name == "spawn_planner":
                    # Get a plan first
                    success, plan_output = await self._spawn_agent("planner", task)

                    if success:
                        # Parse and execute the plan
                        planner = self._get_planner_agent()
                        plan = planner._parse_plan(plan_output)

                        if plan.steps:
                            # Attach metadata and persist before execution
                            plan.user_request = user_message
                            planner.save_plan(plan, self.project_dir)

                            console.print(f"\n[bold]Plan created ({plan.complexity} complexity, {len(plan.steps)} steps)[/bold]")
                            console.print(f"[dim]{plan.analysis}[/dim]\n")

                            final_response = await self._execute_plan(plan, user_message)
                        else:
                            final_response = f"Plan created but no executable steps found:\n{plan_output}"
                    else:
                        final_response = f"Planning failed: {plan_output}"

                elif name == "spawn_explorer":
                    success, output = await self._spawn_agent("explorer", task)
                    final_response = await self._review_and_supervise(
                        user_message, "explorer", output, success, round_num=1
                    )
                elif name == "spawn_executor":
                    success, output = await self._spawn_agent("executor", task)
                    final_response = await self._review_and_supervise(
                        user_message, "executor", output, success, round_num=1
                    )
                elif name == "spawn_researcher":
                    success, output = await self._spawn_agent("researcher", task)
                    final_response = await self._review_and_supervise(
                        user_message, "researcher", output, success, round_num=1
                    )
                else:
                    final_response = response_text

                self.conversation_history.append(Message(role="user", content=user_message))
                self.conversation_history.append(Message(role="assistant", content=final_response))

                # Extract and store important memories (best-effort, non-blocking)
                asyncio.create_task(self._extract_and_store_memories(user_message, final_response))

                return final_response

            # No tool calls - direct response (knowledge base role)
            self.conversation_history.append(Message(role="user", content=user_message))
            self.conversation_history.append(Message(role="assistant", content=response_text))

            # Extract and store important memories (best-effort, non-blocking)
            asyncio.create_task(self._extract_and_store_memories(user_message, response_text))

            return response_text

        except Exception as e:
            console.print("            ", end="\r")
            return f"Error: {str(e)}"

    def reset_conversation(self) -> None:
        """Reset the conversation history."""
        self.conversation_history = []
        self.conversation_summary = ""

    def activate_skill(
        self,
        name: str,
        content: str,
        chain: list[str] | None = None,
        model: str | None = None,
    ) -> None:
        """Activate a skill, injecting its content into the system prompt.

        Args:
            name: Skill name
            content: Full skill markdown content
            chain: Optional list of chained skill names
            model: Optional model override from skill frontmatter
        """
        self.active_skill = name
        self.skill_content = content
        self.skill_chain = chain or [name]

        # Apply model override if specified
        if model:
            self._saved_model = self.model
            self.model = model

    def deactivate_skill(self) -> None:
        """Clear active skill state and restore original model."""
        self.active_skill = None
        self.skill_content = None
        self.skill_chain = []

        # Restore saved model if it was overridden
        if self._saved_model is not None:
            self.model = self._saved_model
            self._saved_model = None

    def get_agent_status(self) -> dict:
        """Get current agent concurrency status."""
        return {
            "active_agents": self.agent_semaphore.active_agents,
            "available_slots": self.agent_semaphore.available_slots,
            "max_concurrent": self.agent_semaphore._max,
        }

    # ==================== Context Management ====================

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count from text (rough approximation)."""
        return len(text) // self.CHARS_PER_TOKEN

    def _get_context_window(self) -> int:
        """Get the context window size for the current model."""
        return self.settings.defaults.context_window

    def _get_history_tokens(self) -> int:
        """Estimate total tokens in conversation history."""
        total = 0
        if self.conversation_summary:
            total += self._estimate_tokens(self.conversation_summary)
        for msg in self.conversation_history:
            total += self._estimate_tokens(msg.content)
        return total

    def _needs_compaction(self) -> bool:
        """Check if conversation history needs compaction."""
        context_window = self._get_context_window()
        threshold = int(context_window * self.CONTEXT_THRESHOLD_PERCENT / 100)
        current_tokens = self._get_history_tokens()
        return current_tokens > threshold

    async def _compact_history(self) -> None:
        """Compact conversation history by summarizing older messages.

        Keeps recent messages and creates a summary of older ones.
        This preserves context while staying within token limits.
        """
        if len(self.conversation_history) < 4:
            return  # Not enough to compact

        # Keep the last few messages
        keep_count = min(4, len(self.conversation_history) // 2)
        to_summarize = self.conversation_history[:-keep_count]
        to_keep = self.conversation_history[-keep_count:]

        # Build summary prompt
        history_text = "\n".join([
            f"{msg.role}: {msg.content[:500]}..."
            if len(msg.content) > 500 else f"{msg.role}: {msg.content}"
            for msg in to_summarize
        ])

        summary_prompt = f"""Summarize this conversation history concisely, preserving key facts, decisions, and context:

{history_text}

Provide a brief summary (2-4 sentences) of what was discussed and any important outcomes."""

        try:
            messages = [Message(role="user", content=summary_prompt)]
            response_text, _ = await self._call_llm(messages, use_tools=False, timeout=30.0)

            if response_text:
                # Combine with existing summary if any
                if self.conversation_summary:
                    self.conversation_summary = f"{self.conversation_summary}\n\nMore recently: {response_text}"
                else:
                    self.conversation_summary = response_text

                # Keep only recent messages
                self.conversation_history = to_keep
                console.print("[dim](conversation compacted)[/dim]")

        except Exception as e:
            # If summarization fails, just truncate
            debug(f"Compaction failed: {e}")
            self.conversation_history = self.conversation_history[-6:]

    # ==================== Memory Integration ====================

    async def _search_memories(self, query: str) -> list[str]:
        """Search long-term memory for relevant context.

        Args:
            query: The user's message to find relevant memories for

        Returns:
            List of relevant memory strings
        """
        if not self.memory_manager or not self.memory_manager.is_enabled():
            return []

        try:
            results = await self.memory_manager.search_memories(
                query=query,
                user_id=self.session_id,
                limit=self.MAX_MEMORY_RESULTS,
            )
            return [r.get("memory", r.get("content", "")) for r in results if r]
        except Exception as e:
            debug(f"Memory search failed: {e}")
            return []

    async def _store_memory(self, content: str, metadata: dict = None) -> None:
        """Store important information in long-term memory.

        Args:
            content: The content to remember
            metadata: Optional metadata
        """
        if not self.memory_manager or not self.memory_manager.is_enabled():
            return

        try:
            await self.memory_manager.add_memory(
                content=content,
                user_id=self.session_id,
                metadata=metadata or {},
            )
        except Exception as e:
            debug(f"Memory store failed: {e}")

    async def _extract_and_store_memories(self, user_msg: str, assistant_msg: str) -> None:
        """Extract key facts from an exchange and store in memory.

        This runs after each successful exchange to build long-term memory.
        """
        if not self.memory_manager or not self.memory_manager.is_enabled():
            return

        # Only store if there's meaningful content
        if len(assistant_msg) < 50:
            return

        # Build extraction prompt
        extract_prompt = f"""Extract any important facts, decisions, or preferences from this exchange that should be remembered for future conversations.

User: {user_msg[:500]}
Assistant: {assistant_msg[:500]}

If there are important facts (e.g., user preferences, project decisions, file locations mentioned), list them briefly. If nothing important, respond with "None"."""

        try:
            messages = [Message(role="user", content=extract_prompt)]
            response_text, _ = await self._call_llm(messages, use_tools=False, timeout=20.0)

            if response_text and "none" not in response_text.lower()[:20]:
                await self._store_memory(
                    content=response_text,
                    metadata={"type": "extracted", "session": self.session_id},
                )
        except Exception:
            pass  # Memory extraction is best-effort

    def _build_context_with_memories(
        self, memories: list[str], summary: str = ""
    ) -> str:
        """Build context string from memories and summary.

        Args:
            memories: List of relevant memories
            summary: Conversation summary if any

        Returns:
            Context string to prepend to system prompt
        """
        parts = []

        if summary:
            parts.append(f"Previous conversation summary:\n{summary}")

        if memories:
            memory_text = "\n".join(f"- {m}" for m in memories[:5])
            parts.append(f"Relevant memories:\n{memory_text}")

        if parts:
            return "\n\n".join(parts) + "\n\n"
        return ""
