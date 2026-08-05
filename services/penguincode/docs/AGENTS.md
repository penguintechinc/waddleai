# PenguinCode Agent Architecture

PenguinCode uses a sophisticated multi-agent system with specialized agents collaborating through a central orchestrator. Each agent is designed for specific tasks with defined permissions and model assignments.

## Agent System Overview

The agent architecture follows a **hierarchical delegation model**:

1. **ChatAgent** (Orchestrator) - Routes requests to specialized agents
2. **ExplorerAgent** - Reads files, searches code, navigates codebase
3. **ExecutorAgent** - Creates files, edits code, runs bash commands
4. **PlannerAgent** - Breaks down complex tasks into actionable plans
5. **ResearcherAgent** - Web searches, documentation lookup

```
User Request
    ↓
ChatAgent (Orchestrator)
    ├→ ExplorerAgent (Read-only)
    ├→ ExecutorAgent (Write/Execute)
    ├→ PlannerAgent (Planning)
    └→ ResearcherAgent (Web Search)
```

## ChatAgent: Main Orchestrator

The ChatAgent is the primary agent users interact with. It serves **two key roles**:

### Role 1: Knowledge Base
Answers general questions directly without spawning other agents for simple queries.

### Role 2: Foreman
For complex tasks, it delegates work to specialized agents, reviews their output, and can dispatch follow-up agents to fix issues.

### Key Features

**Intent Detection**: Automatically routes requests based on keywords
```python
# Patterns that trigger agent spawning
"read", "show", "find", "search" → spawn_explorer
"create", "write", "edit", "run" → spawn_executor
"how to", "documentation", "tutorial" → spawn_researcher
"implement", "build", "refactor" → spawn_planner
```

**Concurrency Control**: Manages up to N concurrent agents (configurable)
```python
# From config.yaml
regulators:
  max_concurrent_agents: 5  # Limit parallel agent execution
```

**Context Management**:
- Conversation history compaction when exceeding 70% of context window
- Long-term memory search via mem0 for cross-session persistence
- Automatic context injection from relevant memories

**Supervision Loop**: After agents complete tasks, ChatAgent reviews output and may:
- Accept and summarize results
- Spawn follow-up agents for fixes (up to 3 rounds)
- Return final response to user

### Example Flow

```
User: "Create a Python script that counts to 100"
  ↓
ChatAgent: Detects executor pattern → spawn_executor
  ↓
ExecutorAgent: Runs agentic loop → creates file
  ↓
ChatAgent: Reviews output → accepts and returns summary
```

## ExplorerAgent: Codebase Navigation

Read-only agent for understanding codebases.

**Permissions**: `READ` + `SEARCH` (glob, grep)

**Available Tools**:
- `read(path)` - Read file contents with line numbers
- `glob(pattern)` - Find files by pattern (e.g., `**/*.py`)
- `grep(pattern)` - Search with regex across files

**System Prompt Focus**:
- Must call tools immediately, not just describe intentions
- Cannot modify files or run shell commands
- Provides concrete findings with file paths and code snippets

**Use Cases**:
- "Find all Python files in the project"
- "Search for function definitions of 'main'"
- "Show me what's in config.yaml"

```python
# ExplorerAgent initialization
agent = ExplorerAgent(
    ollama_client=client,
    working_dir="/path/to/project",
    model="llama3.2:3b"  # Lightweight model for reading
)

result = await agent.run("Find all test files")
```

## ExecutorAgent: Code Mutations & Execution

Full-featured agent for creating and modifying code.

**Permissions**: `READ` + `SEARCH` + `BASH` + `WRITE`

**Available Tools**:
- `write(path, content)` - Create or overwrite files
- `edit(path, old_text, new_text)` - Targeted text replacements
- `bash(command, timeout)` - Execute shell commands
- `read(path)` - Read files before editing
- `glob`, `grep` - Find and search files

**Workflow Pattern**:
1. Read existing file (if editing)
2. Call tool (write/edit/bash)
3. See result from tool execution
4. Repeat or provide final summary

**Security Built-in**: OWASP Top 10 compliance requirements in system prompt:
- No hardcoded secrets or API keys
- Input validation and SQL injection prevention
- Proper access control and authorization
- Secure authentication and session management

**Use Cases**:
- "Create a Flask hello world app"
- "Fix the bug in app.py by changing X to Y"
- "Run pytest and show results"
- "Install dependencies with pip"

```python
# ExecutorAgent initialization
agent = ExecutorAgent(
    ollama_client=client,
    working_dir="/path/to/project",
    model="qwen2.5-coder:7b"  # Code-specialized model
)

result = await agent.run("Create app.py with Flask hello world")
```

## PlannerAgent: Task Decomposition

Breaks down complex tasks into structured execution plans.

**Permissions**: None (pure reasoning, no tools)

**Output Format**: Structured plan with:
- **Analysis**: Brief understanding of the task
- **Steps**: Numbered, actionable steps for agents
- **Agent Assignments**: Each step assigned to explorer or executor
- **Dependencies**: Which steps can run in parallel
- **Complexity**: simple | moderate | complex

**Plan Structure**:
```
ANALYSIS: Refactor authentication across codebase

STEPS:
1. [explorer] Find all files using old auth system
2. [explorer] Identify auth functions to replace
3. [executor] Update auth functions (can run with step 2)
4. [executor] Update imports across files (depends on: 3)
5. [executor] Run tests to verify (depends on: 4)

PARALLEL_GROUPS:
- Group 1: steps 1, 2 (can run together)
- Group 2: steps 3 (after exploration)
- Group 3: step 4 (sequential)
- Group 4: step 5 (final verification)

COMPLEXITY: moderate
```

**Parallel Execution**: ChatAgent uses parallel_groups to run multiple agents simultaneously:
```python
# From chat.py _execute_plan()
for group in plan.parallel_groups:
    group_steps = [s for s in plan.steps if s.step_num in group]
    # Run all steps in group in parallel
    results = await self._spawn_agents_parallel(tasks)
```

## Model Selection Per Agent

Models are configured in `config.yaml` with a tiered approach for optimal VRAM usage:

```yaml
models:
  # Orchestration (routing decisions)
  orchestration: "llama3.2:3b"

  # Planning (task breakdown)
  planning: "deepseek-coder:6.7b"

  # Execution (code generation)
  execution: "qwen2.5-coder:7b"       # Complex tasks
  execution_lite: "qwen2.5-coder:7b"  # Simple edits

  # Exploration (code reading)
  exploration: "llama3.2:3b"          # Standard reads
  exploration_lite: "llama3.2:3b"     # Quick reads

  # Research (web lookups)
  research: "llama3.2:3b"
```

**Complexity-Based Selection**:
ChatAgent estimates task complexity and selects appropriate model:
```python
def _estimate_complexity(self, task: str) -> str:
    # "simple" → uses lite model (faster, less VRAM)
    # "moderate" → uses standard model
    # "complex" → uses full model
```

**Per-Agent Overrides**: Each agent can be configured with specific model:
```yaml
agents:
  executor:
    model: "qwen2.5-coder:7b"
  explorer:
    model: "llama3.2:3b"
  planner:
    model: "deepseek-coder:6.7b"
```

## Agent Communication & Handoffs

### Request Flow

```
1. User Input
   ↓
2. ChatAgent._detect_user_intent()
   → Analyzes message for keywords
   ↓
3. ChatAgent._call_llm()
   → LLM makes routing decision (if unclear)
   ↓
4. ChatAgent._spawn_agent()
   → Acquires semaphore slot
   → Runs agent with timeout
   ↓
5. Agent.agentic_loop()
   → LLM calls tools iteratively
   → Executes tools with agent permissions
   ↓
6. AgentResult returned
   → success: bool
   → output: str (final response)
   → tool_calls: list (audit trail)
   ↓
7. ChatAgent._review_and_supervise()
   → Reviews output quality
   → May spawn follow-up agents for fixes
   ↓
8. Final Response to User
```

### Message Format

Agents use JSON tool calls (or natural language detection as fallback):

```json
{
  "name": "write",
  "arguments": {
    "path": "/tmp/app.py",
    "content": "print('hello')"
  }
}
```

Tools are defined in `TOOL_DEFINITIONS` (base.py):
- `read` - file reading
- `write` - file creation
- `edit` - text replacement
- `bash` - shell execution
- `grep` - pattern search
- `glob` - file globbing

### Tool Execution Flow

```python
# From base.py agentic_loop()
1. LLM generates response with tool calls
2. _parse_tool_calls() extracts calls from text/structured
3. For each tool call:
   a. Check if agent has permission
   b. Execute tool (e.g., write file)
   c. Format result as string
   d. Add to message history
4. Pass results back to LLM
5. Repeat until LLM provides final answer (no tool calls)
```

## Intent Detection & Routing

ChatAgent uses **multi-level intent detection**:

### Level 1: LLM Routing
System prompt instructs LLM to output JSON tool calls for agent spawning.

### Level 2: Keyword Patterns
If LLM output unclear, analyze for specific keywords:

```python
# File/code operations
if re.search(r'\b(create|write|make|add)\s+(?:a\s+)?(?:\w+\s+)?(file|script)\b', msg):
    return "spawn_executor"

# Reading/searching
if any(kw in msg for kw in ["read", "show", "find", "search"]):
    return "spawn_explorer"

# Research/documentation
if any(kw in msg for kw in ["how do i", "how to", "documentation"]):
    return "spawn_researcher"

# Complex planning
if any(kw in msg for kw in ["implement", "build a", "refactor"]):
    return "spawn_planner"
```

### Level 3: Natural Language Detection
Fall back to intent extraction from response text if no structured calls detected.

## Agent Response Handling

### AgentResult Structure

```python
@dataclass
class AgentResult:
    agent_name: str           # Which agent ran
    success: bool             # Did it complete?
    output: str               # Final response
    error: Optional[str]      # Error message if failed
    tool_calls: List[Dict]    # Audit trail of tools used
    tokens_used: int          # Token consumption
    duration_ms: float        # Execution time
```

### Streaming & Real-Time Feedback

Responses stream in real-time via async generators:

```python
# Chat endpoint returns streaming response
async for chunk in agent.agentic_loop(task):
    # Display each chunk as it arrives
    console.print(chunk, end="", flush=True)
```

Tool execution is logged and displayed:
```
  > read(path='/path/to/file.py')
  > write(path='/tmp/output.py', content='...')
  > bash(command='npm install')
```

## Extending with Custom Agents

### Creating a New Agent

1. **Inherit from BaseAgent**:
```python
from penguincode.agents.base import BaseAgent, AgentConfig, Permission

class MyCustomAgent(BaseAgent):
    def __init__(self, ollama_client, model="llama3.2:3b"):
        config = AgentConfig(
            name="my_agent",
            model=model,
            description="What this agent does",
            permissions=[Permission.READ, Permission.SEARCH],
            system_prompt="Your custom system prompt here",
            max_iterations=10
        )
        super().__init__(config, ollama_client)

    async def run(self, task: str, **kwargs) -> AgentResult:
        return await self.agentic_loop(task)
```

2. **Register with ChatAgent**:
```python
# In chat.py
def _get_custom_agent(self):
    if self._custom_agent is None:
        from .custom import MyCustomAgent
        self._custom_agent = MyCustomAgent(
            ollama_client=self.client,
            model=self.settings.models.custom
        )
    return self._custom_agent

# Add to spawn logic
elif agent_type == "spawn_custom":
    agent = self._get_custom_agent()
```

3. **Update Tool Definitions**:
```python
# In base.py TOOL_DEFINITIONS
TOOL_DEFINITIONS["custom_tool"] = {
    "type": "function",
    "function": {
        "name": "custom_tool",
        "description": "What this tool does",
        "parameters": {...}
    }
}
```

### Permission Model

Agents request only needed permissions:

```python
# Explorer - read-only
permissions=[Permission.READ, Permission.SEARCH]

# Executor - full access
permissions=[Permission.READ, Permission.SEARCH, Permission.BASH, Permission.WRITE]

# Custom - specific to needs
permissions=[Permission.READ, Permission.BASH]
```

## Configuration Reference

### Agent Limits (config.yaml)

```yaml
regulators:
  max_concurrent_agents: 5      # Parallel agents (for GPU resource mgmt)
  agent_timeout_seconds: 300    # Max time per agent (5 minutes)
  max_concurrent_requests: 2    # GPU request queue
  min_request_interval_ms: 100  # Spacing between requests
```

### Model Configuration

```yaml
defaults:
  temperature: 0.7              # Randomness (0.0-1.0)
  max_tokens: 4096              # Response length
  context_window: 8192          # Context size
```

### Memory Configuration

```yaml
memory:
  enabled: true                 # Long-term memory
  vector_store: "chroma"        # Storage backend
  embedding_model: "nomic-embed-text"
```

## Agentic Loop Details

Each agent runs the same core loop (from base.py):

```python
async def agentic_loop(self, task: str) -> AgentResult:
    messages = [system_prompt, user_task]
    iteration = 0

    while iteration < max_iterations:
        iteration += 1

        # Call LLM with available tools
        response_text = await client.chat(
            model=config.model,
            messages=messages,
            tools=self.tool_definitions,  # Based on permissions
        )

        # Extract tool calls from response
        tool_calls = parse_tool_calls(response_text)

        if not tool_calls:
            # No more tools - LLM finished
            return AgentResult(success=True, output=response_text)

        # Execute tools and add results to history
        for tool_call in tool_calls:
            result = await execute_tool(tool_call)
            messages.append(Message(role="tool", content=result))

        # Continue loop with new context
```

## Concurrency & Resource Management

ChatAgent uses `AgentSemaphore` to control concurrent execution:

```python
class AgentSemaphore:
    def __init__(self, max_concurrent: int = 5):
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def acquire(self):
        await self._semaphore.acquire()

    def release(self):
        self._semaphore.release()
```

When spawning parallel agents:
```python
# Wait for available slot
await agent_semaphore.acquire()
try:
    result = await agent.run(task)
finally:
    agent_semaphore.release()
```

This prevents GPU memory overload by limiting concurrent model inference.

## Debug & Monitoring

Enable detailed logging:
```python
# In penguincode/core/debug.py
log_llm_request(model, messages, tools)      # Log LLM calls
log_agent_spawn(agent_type, task, complexity) # Log spawning
log_agent_result(agent_type, success, output) # Log results
log_intent_detection(message, intent)         # Log routing
```

Agent status available via:
```python
chat_agent.get_agent_status()
# Returns:
# {
#   "active_agents": 2,
#   "available_slots": 3,
#   "max_concurrent": 5
# }
```

## Summary

The PenguinCode agent system provides:
- **Specialization**: Agents with focused purposes and permissions
- **Orchestration**: Smart routing via ChatAgent as foreman
- **Parallelization**: Concurrent agent execution for speed
- **Supervision**: Quality review and follow-up fixes
- **Extensibility**: Easy custom agent creation
- **Resource Management**: GPU memory-aware concurrency limiting
- **Auditability**: Full tool call logging and result tracking

**Last Updated**: 2025-12-28
**See Also**: [USAGE.md](USAGE.md), [DOCS_RAG.md](DOCS_RAG.md)
