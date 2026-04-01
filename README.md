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

**For developers and teams**: Run cutting-edge AI assistance on your own hardware, with your data staying local, using open-source models like Llama and Mistral. No expensive subscriptions. No vendor lock-in. Full control.

---

## 🎯 Philosophy: "Open First"

WaddleAI is built on the principle that powerful AI capabilities should be accessible to everyone, not just those who can afford Claude Max or GPT-4 Pro subscriptions. We use:

- **Open-source models** (Ollama, Llama 3.2, Mistral, CodeLlama)
- **Open embeddings** (Nomic, open-source alternatives to proprietary APIs)
- **Self-hosted infrastructure** (Docker, Kubernetes — your servers, your data)
- **Permissive licensing** (AGPL with commercial exception)

While our models may not match Claude's capability, WaddleAI empowers teams to run powerful AI locally with full infrastructure control.

---

## 📦 Platform Components

This repo contains the complete WaddleAI ecosystem:

### 1. **WaddleAI Platform** — Intelligent AI Gateway & Management
**For**: Platform engineers, DevOps teams, enterprise AI infrastructure

Enterprise-grade management platform that orchestrates AI provider connections (both open-source and commercial), Ollama model deployments, and intelligent routing through MarchProxy's AI Load Balancer (AILB).

**Key Features**:
- 🔌 **Multi-provider support**: OpenAI, Anthropic, Gemini, AWS Bedrock, Azure, Cohere, local Ollama
- 🔄 **Auto-sync to AILB**: Automatic route creation with rate limits and quotas
- 🚀 **Ollama orchestration**: Docker, Kubernetes, external instances
- 📊 **Usage tracking**: LiteLLM-style token counting and cost analytics
- 💰 **Budget enforcement**: Daily/monthly quotas per user, team, or API key
- 🔑 **Virtual key management**: Granular permissions and expiration

**Quick Links**: [Platform Docs](docs/DEVELOPMENT.md) | [Architecture](docs/ARCHITECTURE.md) | [API Reference](docs/api/) | [Deployment](docs/DEPLOYMENT.md)

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

```bash
# Clone and setup
git clone https://github.com/penguintechinc/waddleai.git
cd waddleai

# Create environment
cat > .env << EOF
POSTGRES_PASSWORD=$(openssl rand -hex 16)
JWT_SECRET=$(openssl rand -hex 32)
FLASK_SECRET_KEY=$(openssl rand -hex 32)
MARCHPROXY_AILB_HOST=localhost
MARCHPROXY_AILB_GRPC_PORT=50051
MARCHPROXY_AILB_HTTP_PORT=8080
WEBHOOK_SECRET=$(openssl rand -hex 16)
EOF

# Start Platform + Assistant
docker compose up -d

# Check health
curl http://localhost:8001/healthz

# WaddleAI Assistant now connects to Platform for advanced features
cd services/penguincode
./penguincode chat
```

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
- **[API Reference](docs/api/README.md)** — Complete REST API documentation
- **[Deployment Guide](docs/DEPLOYMENT.md)** — Production deployment on Kubernetes
- **[Standards & Guidelines](docs/STANDARDS.md)** — Code standards, architecture patterns
- **[K8s Deployment](docs/k8s-deployment.md)** — Kubernetes setup and configuration

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
   │                          │                       │
   ▼                          ▼                       ▼
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│  WaddleAI    │      │  WaddleAI    │      │  MarchProxy  │
│  Platform    │◄────►│  Assistant   │      │  AILB        │
│              │      │              │      │              │
│• Management  │      │• Multi-agent │      │• High-perf   │
│  API         │      │  system      │      │  gateway     │
│• Provider    │      │• Ollama      │      │• Route sync  │
│  mgmt        │      │  integration │      │• Rate limits │
│• Usage       │      │• RAG + mem0  │      │• Webhooks    │
│  tracking    │      │• CLI/VS Code │      │              │
│• Quotas      │      │  extension   │      │              │
└──────────────┘      └──────────────┘      └──────────────┘
        │                    │                       │
        │ gRPC Sync         │ Integrated CLI       │ HTTP/gRPC
        │                    │ Optional Platform    │
        ▼                    ▼                       ▼
┌────────────────────────────────────────────────────────┐
│           Your AI Infrastructure                       │
├─ Local Ollama instances (Llama 3.2, Mistral, etc.)    │
├─ Kubernetes clusters (model serving)                   │
├─ Commercial providers (OpenAI, Anthropic, Gemini)      │
└─ Custom LLM deployments (vLLM, LM Studio, etc.)       │
```

**Integration**: WaddleAI Assistant can operate standalone (local Ollama) OR connect to WaddleAI Platform for team features (provider routing, usage tracking, quotas, multi-user management).

---

## 🔌 Integration

### WaddleAI + MarchProxy AILB

WaddleAI manages configuration, quotas, and analytics for **MarchProxy AILB**, the high-performance AI load balancer:

- **Route Sync**: Automatic provider/model route creation
- **Rate Limits**: Virtual key limits enforced at gateway
- **Usage Webhooks**: Real-time usage events for tracking
- **Health Checks**: Continuous provider monitoring

### Penguin Code + Ollama

Penguin Code runs agents on **Ollama** or connects to remote LLM servers:

- **Local Mode**: Direct Ollama connection (fastest)
- **Server Mode**: gRPC server for team deployments
- **Bootstrap to OpenCode**: Hands off to external agents if needed

---

## 🛠️ Development

### Prerequisites
- Docker & Docker Compose
- Python 3.13+ (for WaddleAI)
- Go 1.24+ (for MarchProxy integration)
- Ollama (for Penguin Code)
- PostgreSQL 15+ (for WaddleAI database)

### Running Both Services Locally

```bash
# Terminal 1: Start WaddleAI
cd services/management
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python wsgi.py

# Terminal 2: Start Penguin Code
cd services/penguincode
./penguincode chat

# Terminal 3: Start Ollama
ollama serve
```

See [WaddleAI Development Guide](docs/DEVELOPMENT.md) and [Penguin Code Usage Guide](docs/penguincode/USAGE.md).

---

## 📊 Project Statistics

| Component | Type | Language | Status |
|-----------|------|----------|--------|
| WaddleAI Management | Service | Python (Flask) | Production Ready |
| WaddleAI Proxy | Service | Python (Quart) | Production Ready |
| Penguin Code CLI | CLI Tool | Python | Production Ready |
| Penguin Code VS Code Ext | Extension | TypeScript | Production Ready |
| MarchProxy AILB | Separate Repo | Go | Production Ready |

---

## 🔒 Security

Both services follow enterprise security standards:

- **Authentication**: Flask-Security-Too (WaddleAI), JWT (Penguin Code)
- **Encryption**: TLS 1.2+, Fernet-encrypted credentials
- **Input Validation**: Server-side validation on all inputs
- **Audit Logging**: Comprehensive security event logging
- **Rate Limiting**: Request throttling and quota enforcement
- **Supply Chain**: SHA256 digest pinning for all dependencies

See [WaddleAI Security Guide](docs/SECURITY.md) and [Penguin Code Security Guide](docs/penguincode/SECURITY.md).

---

## 📄 License

**Limited AGPL-3.0** with commercial use restrictions

- ✅ **Free for Personal/Internal Use**
- ✅ **Open Source Contributions Welcome**
- ⚠️ **Commercial/SaaS Requires License**
- 🏢 **Contributor Employer Exception** (GPL-2.0 grant)

See [LICENSE.md](LICENSE.md) for details.

---

## 🆘 Support & Contact

- **WaddleAI Platform Docs**: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | [docs/API](docs/api/)
- **WaddleAI Assistant Docs**: [docs/penguincode/USAGE.md](docs/penguincode/USAGE.md) | [docs/penguincode/ARCHITECTURE.md](docs/penguincode/ARCHITECTURE.md)
- **Issues**: [GitHub Issues](https://github.com/penguintechinc/waddleai/issues)
- **Email**: support@penguintech.io
- **Company**: [www.penguintech.io](https://www.penguintech.io)

---

## 🙏 Acknowledgments

- [Ollama](https://ollama.ai) — Bringing LLMs to every machine
- [Llama](https://www.llama.com/) — State-of-the-art open-source models
- [MarchProxy](https://github.com/penguintechinc/marchproxy) — High-performance AI load balancer
- [mem0](https://mem0.ai/) — Persistent memory for AI systems
- Flask, Quart, FastAPI, and the Python community
- OpenAI, Anthropic, Google, Meta for advancing the field

---

## 🌟 Why WaddleAI?

1. **Open First**: Uses open-source models (Llama, Mistral) where possible
2. **Self-Hosted**: Full control — no SaaS fees, data stays on your servers
3. **Team-Ready**: Multi-user quotas, audit logging, usage tracking
4. **Flexible**: Connect to any LLM provider (local Ollama, OpenAI, Anthropic, etc.)
5. **Powerful**: Enterprise-grade features at open-source prices

---

**Made by [Penguin Tech Inc](https://www.penguintech.io) for developers who value control, privacy, and open-source principles.**

