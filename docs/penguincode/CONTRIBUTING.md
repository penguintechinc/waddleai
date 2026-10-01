# Contributing to PenguinCode

Welcome! This document provides guidance for contributing to PenguinCode, an AI-powered coding assistant using Ollama.

## Project Overview

PenguinCode is a CLI tool that leverages local LLMs (via Ollama) to provide intelligent coding assistance. The project combines agent-based orchestration with modular tools, MCP integrations, and a client-server architecture for flexible deployment.

## Development Setup

### Prerequisites
- Python 3.12 or higher (3.13 supported)
- Ollama with required models installed
- Git for version control

### Installation Steps

1. **Clone the Repository**
   ```bash
   git clone https://github.com/penguintechinc/penguin-code.git
   cd penguin-code
   ```

2. **Install Development Dependencies**
   ```bash
   pip install -e ".[dev]"
   ```
   This installs the package in editable mode with all development tools (pytest, ruff, mypy).

3. **Setup Ollama**
   - Install Ollama from [ollama.ai](https://ollama.ai)
   - Pull required models:
     ```bash
     ollama pull deepseek-coder:6.7b
     ollama pull gemma4:12b-it-qat   # coding roles: orchestration, exploration
     ollama pull gemma4:e4b   # research and other non-code roles
     ollama pull gemma4:12b-it-qat   # recommended for complex ops like coding
     ollama pull qwen2.5-coder:7b
     ollama pull nomic-embed-text
     ```
   - Start Ollama service: `ollama serve`

4. **Verify Installation**
   ```bash
   penguincode --help
   ```

## Project Structure

```
penguincode/
├── penguincode/
│   ├── agents/          # Agent definitions and orchestration
│   ├── tools/           # Tool implementations
│   ├── mcp/             # MCP server integrations
│   ├── server/          # gRPC server and client logic
│   ├── memory/          # Memory layer (mem0 integration)
│   ├── config.py        # Configuration management
│   └── main.py          # CLI entry point
├── tests/               # Test suite
├── config.yaml          # Default configuration
├── pyproject.toml       # Package metadata
└── docs/                # Documentation
```

## Code Style

### Linting with Ruff

All code must pass Ruff linting before submission:

```bash
ruff check penguincode tests
ruff format penguincode tests
```

### Style Guidelines
- **Line Length**: 100 characters (enforced by Ruff)
- **Type Hints**: Required for all function signatures
- **Imports**: Organized with `I` (isort) rules
- **Python**: Follow PEP 8 standards

Configuration is in `pyproject.toml`:
```toml
[tool.ruff]
line-length = 100
```

## Testing

Run tests with pytest:

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=penguincode

# Run specific test file
pytest tests/test_agents.py

# Run tests matching pattern
pytest -k "test_agent_execution"
```

Async tests use `pytest-asyncio` with auto mode enabled.

### Coverage Gate

`make test-coverage` (what CI's `test-penguincode` job runs) enforces two
independent thresholds against the same coverage data file, via
`scripts/coverage_gate.py`:

- **Tier A — platform modules, 90% line + 90% branch.** Scope is an
  explicit file list in `TIER_A_PATHS` (top of `scripts/coverage_gate.py`),
  not a directory glob: `stores/`, `graphs/`, `retrieval/`, `lessons/`,
  `auth/`, `flags/`, `observability/`, `db/` (whole subtrees), plus
  `docs_rag/{indexer,injector}.py`,
  `server/{interceptors.py,services/knowledge.py,services/lessons.py}`,
  `client/{knowledge_client,lessons_client,waddleai_auth}.py`, and
  `tools/memory.py`. A directory-glob version of this list was tried first
  and rejected — `server/**`/`client/**`/`docs_rag/**` pull in legacy,
  non-platform modules (`server/main.py`, `server/services/chat.py`,
  `client/grpc_client.py`, `docs_rag/fetcher.py`, etc.) that aren't this
  cycle's work and understate the real number. **Adding a new
  knowledge-platform module requires adding it to `TIER_A_PATHS`
  explicitly** — this is the real quality bar for that work, and a PR
  touching these modules must keep them at 90%+.
- **Tier B — whole-package ratchet floor.** Everything else (including
  legacy modules like `core/repl.py` that predate this policy), gated
  against the value in `.coverage-floor`. That file records today's
  achievable whole-package number and can only move **up**, never down —
  raise it in the same PR that adds coverage to a legacy module, never lower
  it to make a regression pass.

Both tiers print the files/statements examined and fail hard on a zero
denominator — a scanner pointed at the wrong path reports clean otherwise.
`pytest --cov=penguincode` above is for local iteration; `make test-coverage`
is the actual CI gate.

## Adding New Agents

1. **Create Agent Module** in `penguincode/agents/your_agent.py`
2. **Implement Agent Class** inheriting from base agent
3. **Define Execute Method** with agent logic
4. **Register in Config** in `config.yaml` under `agents:` section
5. **Add Tests** in `tests/test_agents.py`

Reference existing agents in `penguincode/agents/` for patterns.

## Adding New Tools

1. **Create Tool Module** in `penguincode/tools/your_tool.py`
2. **Implement Tool Class** with:
   - `name: str` property
   - `description: str` property
   - `execute(**kwargs)` async method
3. **Register in Main** in `penguincode/main.py`
4. **Add Tests** in `tests/test_tools.py`
5. **Update Config** if tool needs configuration

See `penguincode/tools/` for existing tool examples.

## Adding MCP Integrations

1. **Configure Server** in `config.yaml` under `mcp.servers:`
   ```yaml
   - name: "your-server"
     transport: "stdio"  # or "http"
     command: "your-command"
     args: ["--flag"]
   ```
2. **Implement Handler** in `penguincode/mcp/handlers.py`
3. **Add Tests** in `tests/test_mcp.py`

Reference `docs/MCP.md` for detailed MCP configuration.

## Updating Proto Definitions

1. **Modify `.proto` files** in `penguincode/server/protos/`
2. **Regenerate Python code**:
   ```bash
   python -m grpc_tools.protoc \
     -I./penguincode/server/protos/ \
     --python_out=./penguincode/server/ \
     --grpc_python_out=./penguincode/server/ \
     ./penguincode/server/protos/service.proto
   ```
3. **Update Type Stubs** if needed
4. **Test** with both client and server modes

## Pull Request Process

1. **Create Feature Branch**
   ```bash
   git checkout -b feature/your-feature
   ```

2. **Make Changes** following code style guidelines

3. **Run Pre-commit Checks**
   ```bash
   ruff check .
   mypy penguincode
   pytest
   ```

4. **Commit with Clear Messages**
   ```bash
   git commit -m "Add feature: clear description"
   ```

5. **Push Branch** and create pull request

6. **Respond to Review** feedback promptly

## Code Review Guidelines

Reviews focus on:
- **Correctness**: Does it work as intended?
- **Testing**: Are edge cases covered?
- **Style**: Does it follow project standards?
- **Performance**: Is there room for improvement?
- **Documentation**: Is intent clear?
- **Type Safety**: Are type hints complete?

All changes require passing CI checks and code review approval.

## License

This project uses the **AGPL-3.0** license. All contributions are subject to this license. See `docs/LICENSE.md` for details.

---

For more information, see related documentation:
- [Architecture](ARCHITECTURE.md) - System design
- [Agents](AGENTS.md) - Agent framework
- [MCP Integration](MCP.md) - MCP server setup
- [Tool Support](TOOL_SUPPORT.md) - Available tools
