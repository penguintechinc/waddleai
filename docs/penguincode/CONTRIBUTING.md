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

### Live-DB Guard Pattern (Mandatory for Any Test Touching Postgres)

A test that needs a real Postgres connection must do exactly one of the
following — never a bare, unguarded connect:

- **Needs a live DB to make an assertion meaningful** (e.g. a recursive-CTE
  traversal, a real `UPDATE ... WHERE status = 'pending'` race): gate the
  whole test/class behind the `requires_postgres = pytest.mark.skipif(not
  TEST_DATABASE_URL, reason="...")` pattern used throughout `tests/*.py`
  (e.g. `tests/test_stores_graph.py`, `tests/test_lessons_store.py`).
  `TEST_DATABASE_URL` absent → **skip with a reason**, never silently pass
  and never hang. `TEST_DATABASE_URL` set but unreachable → the test must
  **fail fast** (a direct `psycopg.connect()`/`run_migrations()` call against
  a refused/closed port raises immediately; it never needs its own timeout
  wrapper for that failure mode).
- **Pure logic — scope checks, authz, field mapping, response shaping**:
  inject a fake double for every store/graph-store/memory-manager dependency
  the servicer accepts, the same way `tests/test_server_knowledge_service.py`
  always passes `indexer=_FakeIndexer()` and
  `tests/test_server_lessons_service.py`'s `_service()` helper always passes
  `graph_store=_FakeGraphStoreForApprove()`. **Never rely on a service
  class's own constructor default** for a dependency that can open a real
  DB connection (e.g. `LessonsServiceImpl.__init__`'s
  `create_graph_store(settings.graph)` fallback) — a unit test that doesn't
  explicitly inject a fake silently inherits whatever real backend that
  default wires up. This bit us directly: `PromoteLesson`/`ApproveLesson`
  both unconditionally call `_known_identifiers` (a tenant-wide graph
  lookup), so any test exercising either RPC without an injected
  `graph_store` opened a real connection through the shared pool
  (`db/pool.py`) against a nonexistent database — each of the 3 graph kinds
  queried paid the pool's own 30s borrow timeout (~90s per call), and the
  pool, never explicitly closed, then stalled interpreter shutdown at the
  end of the whole run. The fix was test-only: inject the fake, the same as
  every other dependency.
- **A live test needs the production code's *own* DSN resolution to see the
  ephemeral DB it just stood up**: don't assume `Settings()`'s default
  env-var wiring reaches it. `TEST_DATABASE_URL` and `PGVECTOR_URL` are two
  different env vars — CI's `test-penguincode` job
  (`.github/workflows/docker-build.yml`) sets only the former. A live test
  that constructs a dependency via `Settings()` defaults instead of an
  explicit `dsn=` pointed at its own fixture's DSN will quietly talk to the
  wrong (or no) database — see
  `TestApproveLessonLiveFirmWideVisibility`'s explicit
  `PostgresGraphStore(dsn=lessons_live_dsn)` injection for the pattern.
- **A whole suite needs its own throwaway Postgres** (not just a skip gate):
  follow `tests/integration/conftest.py`'s `pgvector_dsn` fixture — reuse
  `TEST_DATABASE_URL` when set, otherwise start an ephemeral
  `pgvector/pgvector:pg17` container with a bounded `_wait_for_postgres(...,
  timeout=...)` readiness loop that raises (never loops forever) if the
  container never comes up.

# regression: lessons-tests-db-guard (hang, not skip/fail, when no Postgres
# is reachable)

### Lint Gate

`make lint` (what CI's `test-penguincode` job runs, via `scripts/lint_gate.py`)
is a ratchet gate, not a pass/fail-on-any-finding gate: it runs `ruff check`,
`ruff format --check`, and `mypy --strict` (the real tools, per this
package's own `[tool.ruff]`/`[tool.mypy] strict = true` in `pyproject.toml`)
and fails only on findings **not already present** in the committed
baselines:

- **`.ruff-baseline.txt`** — `ruff check` findings (`check|`-prefixed) and
  `ruff format --check` files (`format|`-prefixed) in one file, both over
  the whole package tree.
- **`.mypy-baseline.txt`** — `mypy --strict` errors, scoped to
  `penguincode_cli` only (the actual installed package per `pyproject.toml`'s
  `packages = ["penguincode_cli"]`) — pointing mypy at the whole directory
  crashes outright on a duplicate module name (a vendored copy of the
  monorepo's `shared/py_libs` that isn't this package's own code). Three
  stray root-level non-package scripts (`app.py`, `client.py`,
  `server/app.py`) used to contribute to the same crash and also carried
  real bandit findings (Flask `debug=True`, no-timeout HTTP) — deleted
  outright as dead code rather than fixed in place; see RELEASE_NOTES.md.

Both baselines were seeded from this package's pre-existing debt (measured
at write time: 150 combined ruff findings, 613 mypy errors) — this gate does
**not** fix that debt and does **not** re-mask it (no `|| true` anywhere in
the chain); it freezes it and fails hard on anything new.

**Ratcheting a baseline down** (fixing debt, not adding more): fix the
finding(s) in code, then regenerate just that baseline so the fix is
reflected and nothing regresses silently:

```bash
python3 scripts/lint_gate.py --write-baseline
```

**A line-number shift (not a new bug) also reads as "new"** — the baseline
stores exact finding lines, not fuzzy-matched by file+rule, mirroring the
root `scripts/mypy-gate.sh`'s same deliberate tradeoff. Adding code above an
existing (pre-existing, un-fixed) error shifts its line number, which then
needs the same `--write-baseline` regeneration — check the diff only
contains line-number shifts for findings you didn't touch before trusting
it, never exempt the gate to work around this.

### Security Gate

`make test-security` runs `bandit` (`--severity-level medium`), `pip-audit`
against `requirements.txt`, and `gitleaks` — unlike the lint gate above, this
is a real pass/fail (no baseline/ratchet): every finding must be fixed or
justified before merge.

- **bandit/pip-audit run from a package-local `.venv`** (`security-venv`
  target, `uv venv -p 3.13 .venv`), not whatever's on `PATH`. A machine-wide
  `bandit`/`pip-audit` install can be pinned to an older system Python;
  `pip-audit`'s own resolver then silently excludes any PyPI release
  requiring a newer Python (this package pins `quart==0.23.1`, which needs
  `>=3.13`), producing a "could not find a version" error that looks like a
  bad pin or a yanked release when it's neither.
- **B608 ("possible SQL injection") false positives**: bandit's heuristic
  flags any f-string that touches a SQL keyword, regardless of whether the
  interpolated value is actually attacker-controlled. The real pattern in
  this package's `*/store.py` and `stores/vector.py` modules is a
  module-level constant column list (`_SELECT_COLUMNS`) or a `Literal`-typed
  table name interpolated into the query *shape*, with every actual value
  bound separately via psycopg's `%(name)s` / sqlite's `?` placeholders —
  never caller input. Justify with a targeted, trailing `# nosec B608` **on
  the exact line bandit reports** (for a multi-line f-string this is the
  closing `"""` line, never the opening one — a comment placed there would
  land *inside* the string literal, not suppress the finding) plus a short
  rationale comment on the line(s) above explaining why the interpolated
  value is safe. Never a file-level or blanket `# nosec`.
- **B104 ("binding to all interfaces")**: a `0.0.0.0` server-listen default
  is the correct container pattern (reachability is governed by the
  Kubernetes Service + CiliumNetworkPolicy in front of it, not the bind
  address) — justify with `# nosec B104`, matching the convention already
  used in `proxy`/`services/management`.
- **Dead code with a real finding gets deleted, not nosec'd.** If the
  flagged code isn't reachable from any shipped entrypoint (check
  `Dockerfile.server`'s `COPY` list and `pyproject.toml`'s `packages`), fix
  the actual problem by removing it.

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
