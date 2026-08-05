<p align="center">
  <img src="penguincode/penguincode-logo.png" alt="Penguin Code Logo" width="400">
</p>

# Penguin Code

```
    ____                        _          ______          __
   / __ \___  ____  ____ ___  _(_)___     / ____/___  ____/ /__
  / /_/ / _ \/ __ \/ __ `/ / / / / __ \   / /   / __ \/ __  / _ \
 / ____/  __/ / / / /_/ / /_/ / / / / /  / /___/ /_/ / /_/ /  __/
/_/    \___/_/ /_/\__, /\__,_/_/_/ /_/   \____/\____/\__,_/\___/
                 /____/
```

**AI-powered coding assistant CLI and VS Code extension using Ollama**

[![CI](https://github.com/penguintechinc/penguin-code/workflows/CI/badge.svg)](https://github.com/penguintechinc/penguin-code/actions)
[![License](https://img.shields.io/badge/License-AGPL%203.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)

*Penguin Tech Inc © 2025*

## Features

- 🤖 **Multi-Agent System** - ChatAgent orchestrates specialized Explorer/Executor agents
- 🔍 **Multi-Engine Research** - 5 search engines with MCP protocol support
- 🧠 **Persistent Memory** - mem0 integration for context across sessions
- 📚 **Documentation RAG** - Auto-detects your project's languages and libraries, fetches official documentation, and uses it for accurate, syntax-correct answers
- 🔌 **MCP Integration** - Dynamic tool discovery from MCP servers — agents automatically see and use MCP tools alongside built-in tools. Works with N8N, Flowise, and any MCP-compatible server
- 🌐 **Client-Server Mode** - gRPC server for remote Ollama and team deployments with shared-key auth
- 🏢 **Organizational Config** - Clients pull MCP servers, skills, and model configs from a management API at startup
- ⚡ **GPU Optimized** - Smart model switching for RTX 4060 Ti (8GB VRAM) or higher
- 🐧 **Cross-Platform** - Works on Linux, macOS, and Windows

### Supported Languages

| Language | Detection | Doc Sources |
|----------|-----------|-------------|
| Python | `pyproject.toml`, `requirements.txt`, `*.py` | Official docs + PyPI libraries |
| JavaScript/TypeScript | `package.json`, `tsconfig.json` | MDN, npm packages |
| Go | `go.mod`, `*.go` | go.dev, pkg.go.dev |
| Rust | `Cargo.toml`, `*.rs` | docs.rs, crates.io |
| OpenTofu/Terraform | `*.tf`, `*.tofu`, `.terraform.lock.hcl` | OpenTofu docs, provider registries |
| Ansible | `ansible.cfg`, `playbook.yml`, `requirements.yml` | Ansible docs, Galaxy collections |
| Ruby | `Gemfile`, `Rakefile`, `.ruby-version` | Ruby docs, Rails guides |
| PHP | `composer.json`, `artisan`, `index.php` | PHP docs, Laravel/Symfony |
| Flutter/Dart | `pubspec.yaml`, `analysis_options.yaml` | Dart guides, Flutter docs |

## Quick Start

### Prerequisites

```bash
# Install Ollama (https://ollama.ai)
curl -fsSL https://ollama.ai/install.sh | sh

# Pull required models
ollama pull llama3.2:latest
ollama pull nomic-embed-text
```

### Option 1: PenguinCode Native Client (`penguincode chat`)

The native client uses PenguinCode's multi-agent ChatAgent system directly with Ollama.
It features a 3-layer intent fallback (structured tool calls → text-parsed JSON → keyword routing),
foreman-supervised execution with up to 3 review rounds, and built-in file/shell tools.

```bash
./penguincode setup         # first-time config (auto-creates venv if needed)
./penguincode chat
```

Inside the REPL you can ask things like:
- `create a Flask app with login` — routes to Executor agent
- `what is the difference between SQLAlchemy and PyDAL?` — routes to Researcher agent
- `build a Go GUI app` — routes to Executor with planning

### Option 2: PenguinCode Bootstrap → OpenCode (`penguincode launch`)

The bootstrap path provisions Ollama model config and project context, then hands off
to [OpenCode](https://github.com/opencode-ai/opencode) for its own agent system and TUI.

```bash
./penguincode launch        # provisions config, then execs into OpenCode
```

> **Note:** `penguincode launch` requires the `opencode` binary on your PATH.
> OpenCode manages its own agent loop — PenguinCode only provisions the initial config.

**VS Code Extension**: Download VSIX from [Releases](https://github.com/penguintechinc/penguin-code/releases)

### Server Mode (Team Deployment)

```bash
# Start gRPC server (connects to local Ollama)
python -m penguincode.server.main

# Or use Docker
docker compose up -d

# Connect from client
penguincode chat --server localhost:50051
```

See [Architecture Documentation](docs/ARCHITECTURE.md) for remote deployment with TLS and authentication.

### MCP Tool Integration

PenguinCode dynamically discovers tools from configured MCP servers and injects them into all agents. No code changes required — just add servers to `config.yaml`:

```yaml
mcp:
  enabled: true
  servers:
    - name: "duckduckgo"
      transport: "stdio"
      command: "npx"
      args: ["-y", "@nickclyde/duckduckgo-mcp-server"]

    - name: "n8n"
      transport: "http"
      url: "http://localhost:5678/mcp"
      headers:
        X-N8N-API-KEY: "${N8N_API_KEY}"
```

Tools appear as `mcp_duckduckgo_search`, `mcp_n8n_execute_workflow`, etc. — agents call them like any built-in tool.

### Shared-Key Authentication

For team deployments, set a single environment variable on both server and client:

```bash
export PENGUINCODE_SHARED_KEY="your-team-secret"
```

The client exchanges this key for a JWT automatically. No API key distribution needed.

## Documentation

- **[Usage Guide](docs/USAGE.md)** - Installation, configuration, and usage
- **[Configuration Reference](docs/CONFIGURATION.md)** - Complete config.yaml reference
- **[Architecture](docs/ARCHITECTURE.md)** - Client-server architecture and deployment modes
- **[Agent Architecture](docs/AGENTS.md)** - ChatAgent, Explorer, Executor, Planner
- **[Tool Support](docs/TOOL_SUPPORT.md)** - Ollama models with native tool calling
- **[MCP Integration](docs/MCP.md)** - Extend with N8N, Flowise, and custom servers
- **[Memory](docs/MEMORY.md)** - Persistent memory with mem0 integration
- **[Documentation RAG](docs/DOCS_RAG.md)** - Project-aware documentation indexing
- **[Security](docs/SECURITY.md)** - Authentication, TLS, and secure code generation
- **[Contributing](docs/CONTRIBUTING.md)** - How to contribute

## License

AGPL-3.0 - See [LICENSE](LICENSE) for details

**Support**: [support.penguintech.io](https://support.penguintech.io) | **Homepage**: [www.penguintech.io](https://www.penguintech.io)
