# WaddleAI Documentation

Enterprise-grade AI proxy and management system with OpenAI-compatible APIs, advanced routing, security, and token management.

## Quick Start

WaddleAI provides a comprehensive AI proxy solution that acts as a drop-in replacement for OpenAI's API while adding advanced features like multi-LLM support, security scanning, and enterprise-grade management.

### What's New in v0.1.0

- **4-tier content filtering** with NER (Presidio/spaCy) and ShieldGemma safety auditing — PII/PCI patterns are now toggleable per organization
- **llama.cpp provider** — run models locally on GPU nodes via K8s DaemonSet, with exact tokenization
- **Multi-credential provider pools** — rotate credentials across LLM provider accounts with automatic failover
- **pgvector memory** — semantic conversation memory backed by PostgreSQL pgvector extension

### Key Features

✨ **OpenAI-Compatible API** - Drop-in replacement for OpenAI API
🔀 **Multi-LLM Support** - Route to OpenAI, Anthropic, Ollama, and more
👥 **Role-Based Access Control** - Admin, Resource Manager, Reporter, User roles
🪙 **Dual Token System** - WaddleAI tokens for billing, LLM tokens for analytics
🛡️ **Security Scanning** - Prompt injection and jailbreak detection
📊 **Token Management** - Quota enforcement and usage tracking
🏢 **Multi-Tenant** - Organization-based isolation
📈 **Monitoring** - Prometheus metrics and health checks
🧠 **Memory Integration** - Conversation memory with mem0/ChromaDB
🔒 **4-Tier Content Filtering** - PII/PCI regex, NER (Presidio/spaCy), ShieldGemma safety auditor
⚡ **Local Inference** - llama.cpp provider for edge/air-gapped deployments
🔑 **Multi-Credential Pools** - Rotate API keys across provider accounts

## Getting Started

### Docker Compose (Recommended)

```bash
# Clone repository
git clone https://github.com/penguintechinc/waddleai.git
cd waddleai

# Create environment file
echo "JWT_SECRET=$(openssl rand -hex 32)" > .env
echo "POSTGRES_PASSWORD=$(openssl rand -hex 16)" >> .env

# Start all services
docker-compose up -d

# Check status
docker-compose ps
```

### Services

- **Proxy Server**: http://localhost:8000 (OpenAI-compatible API)
- **Management Portal**: http://localhost:8001 (Admin interface)
- **Documentation**: http://localhost:8080 (MkDocs site)

### First Steps

1. **Get Admin API Key**: Check logs for the admin API key created on first startup
   ```bash
   docker-compose logs waddleai-mgmt | grep "Admin API Key"
   ```

2. **Test the API**:
   ```bash
   export WADDLEAI_API_KEY="wa-..."   # from the WaddleAI WebUI: Virtual Keys
   ```

   ```bash
   curl http://localhost:8000/v1/models \
     -H "Authorization: Bearer $WADDLEAI_API_KEY"
   ```

3. **Access Management Portal**: Visit http://localhost:8001 and login with `admin/admin123`

## Architecture Overview

WaddleAI consists of two main components:

### Proxy Server (Stateless)
- OpenAI-compatible API endpoints
- Request routing and load balancing
- Security scanning and prompt injection detection
- Token counting and quota enforcement
- Prometheus metrics and health checks

### Management Server (Stateful)
- Web-based administration portal
- User and organization management
- API key management with RBAC
- Usage analytics and reporting
- LLM provider configuration

## Integration Guide

For detailed integration instructions with various tools and platforms, see the [Claude Integration](claude.md) guide, which provides comprehensive examples for:

- Python applications with OpenAI SDK
- Node.js applications
- cURL/HTTP requests
- VS Code extension integration
- Management API usage
- Role-based access control

## Navigation

- **[Getting Started](getting-started/installation.md)** - Installation and setup
- **[API Reference](api/openai-compatible.md)** - Complete API documentation
- **[Administration](administration/user-management.md)** - System management
- **[Integrations](integrations/ollama-setup.md)** - Third-party integrations
- **[Deployment](deployment/docker-compose.md)** - Production deployment
- **[Troubleshooting](troubleshooting/common-issues.md)** - Common issues and solutions

---

**Ready to get started?** Follow the [installation guide](getting-started/installation.md) or try the Docker Compose setup above!
