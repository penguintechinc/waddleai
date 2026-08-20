<div align="center">

[![License](https://img.shields.io/badge/License-Limited%20AGPL--3.0-blue.svg)](LICENSE.md)
[![Python](https://img.shields.io/badge/Python-3.13-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/Docker-Multi--Arch-blue.svg)](https://www.docker.com/)

</div>

# WaddleAI — Open-First AI Platform

An empowering, open-source AI infrastructure suite for developers and teams who want powerful AI capabilities without expensive subscriptions or vendor lock-in.

```
██╗    ██╗ █████╗ ██████╗ ██████╗ ██╗     ███████╗ █████╗ ██╗
██║    ██║██╔══██╗██╔══██╗██╔══██╗██║     ██╔════╝██╔══██╗██║
██║ █╗ ██║███████║██║  ██║██║  ██║██║     █████╗  ███████║██║
██║███╗██║██╔══██║██║  ██║██║  ██║██║     ██╔══╝  ██╔══██║██║
╚███╔███╔╝██║  ██║██████╔╝██████╔╝███████╗███████╗██║  ██║██║
 ╚══╝╚══╝ ╚═╝  ╚═╝╚═════╝ ╚═════╝ ╚══════╝╚══════╝╚═╝  ╚═╝╚═╝
```

**For developers and teams**: Run cutting-edge AI assistance on your own hardware, with your data staying local, using open-source models like Llama and Mistral, or route to commercial providers when you need them. No expensive subscriptions. No vendor lock-in. Full control.

---

## 🎯 Philosophy: "Open First"

WaddleAI is built on the principle that powerful AI capabilities should be accessible to everyone, not just those who can afford Claude Max or GPT-4 Pro subscriptions. We use:

- **Open-source models** (Ollama, llama.cpp, Llama 3.2, Mistral, CodeLlama)
- **Open embeddings** (Nomic, open-source alternatives to proprietary APIs)
- **Self-hosted infrastructure** (Kubernetes — your servers, your data)
- **Permissive licensing** (AGPL with commercial exception)

While our models may not match Claude's capability, WaddleAI empowers teams to run powerful AI locally with full infrastructure control.

---

## 📦 Platform Components

This repo contains the complete WaddleAI ecosystem:

### 1. **WaddleAI Platform** — Intelligent AI Gateway & Management
**For**: Platform engineers, DevOps teams, enterprise AI infrastructure

Enterprise-grade platform that owns the full data plane itself: a Quart-based proxy exposes OpenAI-compatible (`/v1/chat/completions`) and Anthropic-compatible (`/v1/messages`) endpoints and routes them across providers, while a separate Quart management API handles provider config, quotas, and usage analytics.

**Key Features**:
- 🔌 **Multi-provider support**: OpenAI, Anthropic, Google Gemini, xAI, AWS Bedrock, local Ollama, self-hosted llama.cpp
- 🚀 **GPU inference fleet**: Ollama and llama.cpp both deploy as Kubernetes DaemonSets across GPU nodes; the llama.cpp fleet shares one RWX PVC as a model cache across every `llama-server` instance
- 🧠 **Memory scopes**: personal vs. organizational memory, enforced at the API layer
- 📊 **Usage tracking**: token counting and cost analytics, with OpenTelemetry `gen_ai.*` span attributes emitted per request for tracing
- 💰 **Budget enforcement**: Daily/monthly quotas per user, team, or API key
- 🔑 **Virtual key management**: Granular permissions and expiration

**Quick Links**: [Platform Docs](docs/DEVELOPMENT.md) | [Architecture](docs/docs-site/docs/architecture.md) | [API Reference](docs/api/openai-compatible.md) | [Kubernetes Deployment](k8s/helm/waddleai/)

---

### 2. **WaddleAI Assistant** — AI Coding Companion
**For**: Developers, teams, anyone using VS Code or the command line

Intelligent coding assistant that brings AI coding capabilities to your local machine. Uses Ollama-based models locally, or integrates with WaddleAI Platform for advanced features like multi-provider routing and usage tracking.

**Key Features**:
- 🤖 **Multi-agent system**: ChatAgent orchestrates Explorer, Executor, and Planner agents
- 🔍 **Research**: Multi-engine search with MCP protocol (N8N, Flowise, DuckDuckGo, etc.)
- 🧠 **Persistent memory**: mem0 integration — context persists across sessions
- 📚 **Documentation RAG**: Auto-detects your project languages/frameworks, fetches official docs
- 🔌 **MCP Integration**: Extensible tool discovery from any MCP server
- 🌐 **Team mode**: gRPC server + client connections with shared-key auth
- ⚡ **GPU optimized**: Smart model switching for RTX 4060 Ti (8GB) and higher
- 🐧 **Cross-platform**: Linux, macOS, Windows

**Supported Languages**: Python, JavaScript/TypeScript, Go, Rust, Terraform, Ansible, Ruby, PHP, Flutter/Dart

**Quick Links**: [Assistant Docs](docs/penguincode/) | [Usage Guide](docs/penguincode/USAGE.md) | [Configuration](docs/penguincode/CONFIGURATION.md) | [Architecture](docs/penguincode/ARCHITECTURE.md)

---

## 🚀 Quick Start

### Option A: WaddleAI Assistant Only (Local, Offline)

**Best for**: Individual developers, teams with existing Ollama setup, privacy-first workflows

```bash
# Install Ollama first (https://ollama.ai)
curl -fsSL https://ollama.ai/install.sh | sh

# Pull models
ollama pull llama3.2:latest
ollama pull nomic-embed-text

# Install WaddleAI Assistant
cd services/penguincode
./penguincode setup

# Start chatting
./penguincode chat
```

Everything runs locally — your code never leaves your machine. Use open-source models (Llama, Mistral) or commercial APIs if you prefer.

---

### Option B: Full WaddleAI Platform + Assistant (Team, Advanced Features)

**Best for**: Teams, production deployments, need provider routing/quotas/usage tracking

Kubernetes via Helm is the only supported deployment path — Docker Compose is deprecated for every environment.

```bash
# Clone and setup
git clone https://github.com/penguintechinc/waddleai.git
cd waddleai

# The chart manages its own waddleai-secrets Secret by default
# (secrets.manage: true) — do NOT pre-create one with the same name,
# `helm install` will fail with "already exists". Override the two
# secret values instead:
kubectl create namespace waddleai
helm install waddleai k8s/helm/waddleai \
  --namespace waddleai \
  --values k8s/helm/waddleai/values-beta.yaml \
  --set secrets.postgresPassword="$(openssl rand -hex 16)" \
  --set secrets.jwtSecret="$(openssl rand -hex 32)"

# Check health
kubectl port-forward -n waddleai svc/waddleai-management 8001:8001 &
curl http://localhost:8001/healthz

# WaddleAI Assistant now connects to Platform for advanced features
cd services/penguincode
./penguincode chat
```

> **Known chart gap**: `management.secretEnv` requires a `proxy-grpc-auth-token` key on `waddleai-secrets`, but the chart's own `templates/secret.yaml` doesn't generate one — the management pod fails to start (`CreateContainerConfigError`) on a fresh install. Work around it until fixed:
> ```bash
> kubectl patch secret waddleai-secrets -n waddleai --type merge \
>   -p "{\"stringData\":{\"proxy-grpc-auth-token\":\"$(openssl rand -hex 32)\"}}"
> kubectl rollout restart deployment/waddleai-management -n waddleai
> ```

The Assistant automatically detects the Platform and enables:
- Multi-user management
- Quota enforcement
- Usage analytics
- Multi-provider routing (OpenAI, Anthropic, local Ollama)
- Team sharing

---

**For detailed setup**: [WaddleAI Development Guide](docs/DEVELOPMENT.md) | [WaddleAI Assistant Usage Guide](docs/penguincode/USAGE.md)

---

## 📚 Documentation

### WaddleAI Documentation
- **[Installation & Development](docs/DEVELOPMENT.md)** — Local setup, database config, running services
- **[Testing & Validation](docs/TESTING.md)** — Unit tests, integration tests, smoke tests
- **[Architecture](docs/docs-site/docs/architecture.md)** — Data plane, control plane, and inference fleet design
- **[API Reference](docs/api/openai-compatible.md)** — OpenAI-compatible and Anthropic-compatible endpoint docs
- **[Kubernetes Deployment](k8s/helm/waddleai/)** — Helm chart for beta/prod deployment
- **[Standards & Guidelines](docs/STANDARDS.md)** — Code standards, architecture patterns

### Penguin Code Documentation
- **[Usage Guide](docs/penguincode/USAGE.md)** — Installation, native client, server mode, VS Code
- **[Configuration Reference](docs/penguincode/CONFIGURATION.md)** — Complete config.yaml reference
- **[Architecture](docs/penguincode/ARCHITECTURE.md)** — Client-server architecture and deployment modes
- **[Agent Architecture](docs/penguincode/AGENTS.md)** — ChatAgent, Explorer, Executor, Planner
- **[MCP Integration](docs/penguincode/MCP.md)** — Extend with N8N, Flowise, custom servers
- **[Documentation RAG](docs/penguincode/DOCS_RAG.md)** — Project-aware documentation indexing
- **[Tool Support](docs/penguincode/TOOL_SUPPORT.md)** — Ollama models with tool calling
- **[Memory Management](docs/penguincode/MEMORY.md)** — Persistent memory with mem0
- **[Security](docs/penguincode/SECURITY.md)** — Authentication, TLS, secure code generation
- **[Contributing](docs/penguincode/CONTRIBUTING.md)** — How to contribute

---

## 🏗️ Architecture Overview

```
┌────────────────────────────────────────────────────────┐
│          WaddleAI Unified Platform (This Repo)         │
└────────────────────────────────────────────────────────┘
   │                          │
   ▼                          ▼
┌──────────────┐      ┌──────────────┐
│  WaddleAI    │      │  WaddleAI    │
│  Platform    │◄────►│  Assistant   │
│              │      │              │
│• Proxy: data │      │• Multi-agent │
│  plane, 8080 │      │  system      │
│• Management  │      │• Ollama      │
│  API, 8001   │      │  integration │
│• Usage       │      │• RAG + mem0  │
│  tracking    │      │• CLI/VS Code │
│• Quotas      │      │  extension   │
└──────────────┘      └──────────────┘
        │                    │
        │ Shared DB/cache   │ Integrated CLI
        │                    │ Optional Platform
        ▼                    ▼
┌────────────────────────────────────────────────────────┐
│           Your AI Infrastructure                       │
├─ Ollama + llama.cpp DaemonSets (Llama 3.2, Mistral...) │
├─ Kubernetes clusters (model serving)                   │
├─ Commercial providers (OpenAI, Anthropic, Gemini, xAI, │
│  AWS Bedrock)                                           │
└─ Custom LLM deployments (vLLM, LM Studio, etc.)       │
```

**Integration**: WaddleAI Assistant can operate standalone (local Ollama) OR connect to WaddleAI Platform for team features (provider routing, usage tracking, quotas, multi-user management).

---

## 🔌 Integration

### Penguin Code + Ollama

Penguin Code runs agents on **Ollama** or connects to remote LLM servers:

- **Local Mode**: Direct Ollama connection (fastest)
- **Server Mode**: gRPC server for team deployments
- **Bootstrap to OpenCode**: Hands off to external agents if needed

---

## 🛠️ Development

### Prerequisites
- Kubernetes cluster + Helm v4 (deployment)
- Python 3.13+ (for WaddleAI proxy and management)
- Node.js 24+ (for the WebUI)
- Ollama and/or llama.cpp (for local/self-hosted inference)
- PostgreSQL 17 with pgvector (for WaddleAI database)

### Running Services Locally

```bash
# Terminal 1: Start the proxy (data plane, port 8080)
cd proxy
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
hypercorn apps.proxy_server.main:app --bind 0.0.0.0:8080

# Terminal 2: Start the management API (control plane, port 8001)
cd services/management
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
hypercorn asgi:app --bind 0.0.0.0:8001

# Terminal 3: Start Penguin Code
cd services/penguincode
./penguincode chat

# Terminal 4: Start Ollama
ollama serve
```

See [WaddleAI Development Guide](docs/DEVELOPMENT.md) and [Penguin Code Usage Guide](docs/penguincode/USAGE.md).

---

## 📊 Project Statistics

| Component | Type | Language | Status |
|-----------|------|----------|--------|
| WaddleAI Proxy | Service | Python (Quart) | Production Ready |
| WaddleAI Management | Service | Python (Quart) | Production Ready |
| WaddleAI WebUI | Service | React 18 / Vite (Node 24) | Production Ready |
| Penguin Code CLI | CLI Tool | Python | Production Ready |
| Penguin Code VS Code Ext | Extension | TypeScript | Production Ready |

---

## 🔒 Security

Both services follow enterprise security standards:

- **Authentication**: `penguin-aaa` (OIDC/JWT) for WaddleAI, JWT for Penguin Code
- **Encryption**: TLS 1.2+, Fernet-encrypted credentials
- **Input Validation**: Server-side validation on all inputs
- **Audit Logging**: Comprehensive security event logging
- **Rate Limiting**: Request throttling and quota enforcement
- **Supply Chain**: SHA256 digest pinning for all dependencies; CodeQL runs on every PR and weekly on a schedule

See [WaddleAI Security Recommendations](docs/docs-site/docs/administration/ai-security-recommendations.md) and [Penguin Code Security Guide](docs/penguincode/SECURITY.md).

---

## 📄 License

**Limited AGPL-3.0** with commercial use restrictions

- ✅ **Free for Personal/Internal Use**
- ✅ **Open Source Contributions Welcome**
- ⚠️ **Commercial/SaaS Requires License**
- 🏢 **Contributor Employer Exception** (GPL-2.0 grant)

See [LICENSE.md](LICENSE.md) for the full terms.

---

## 🆘 Support & Contact

- **WaddleAI Platform Docs**: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | [k8s/helm/waddleai/](k8s/helm/waddleai/) | [API Reference](docs/api/openai-compatible.md)
- **WaddleAI Assistant Docs**: [docs/penguincode/USAGE.md](docs/penguincode/USAGE.md) | [docs/penguincode/ARCHITECTURE.md](docs/penguincode/ARCHITECTURE.md)
- **Issues**: [GitHub Issues](https://github.com/penguintechinc/waddleai/issues)
- **Email**: support@penguintech.io
- **Company**: [www.penguintech.io](https://www.penguintech.io)

---

## 🙏 Acknowledgments

- [Ollama](https://ollama.ai) — Bringing LLMs to every machine
- [llama.cpp](https://github.com/ggml-org/llama.cpp) — Efficient, self-hosted LLM inference
- [Llama](https://www.llama.com/) — State-of-the-art open-source models
- [mem0](https://mem0.ai/) — Persistent memory for AI systems
- Quart and the Python community
- OpenAI, Anthropic, Google, xAI, AWS, Meta for advancing the field

---

## 🌟 Why WaddleAI?

1. **Open First**: Uses open-source models (Llama, Mistral) where possible
2. **Self-Hosted**: Full control — no SaaS fees, data stays on your servers
3. **Team-Ready**: Multi-user quotas, audit logging, usage tracking
4. **Flexible**: Connect to any LLM provider (local Ollama, OpenAI, Anthropic, etc.)
5. **Powerful**: Enterprise-grade features at open-source prices

---

**Made by [Penguin Tech Inc](https://www.penguintech.io) for developers who value control, privacy, and open-source principles.**
