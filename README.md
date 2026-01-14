<div align="center">

[![License](https://img.shields.io/badge/License-Limited%20AGPL--3.0-blue.svg)](LICENSE.md)
[![Python](https://img.shields.io/badge/Python-3.13-blue.svg)](https://www.python.org/downloads/)
[![Flask](https://img.shields.io/badge/Flask-3.0+-green.svg)](https://flask.palletsprojects.com/)
[![Docker](https://img.shields.io/badge/Docker-Multi--Arch-blue.svg)](https://www.docker.com/)
[![MarchProxy](https://img.shields.io/badge/MarchProxy-AILB%20Compatible-orange.svg)](https://github.com/penguintechinc/marchproxy)

<img src="waddleai-logo.png" alt="WaddleAI Logo" height="300"/>

```
██╗    ██╗ █████╗ ██████╗ ██████╗ ██╗     ███████╗ █████╗ ██╗
██║    ██║██╔══██╗██╔══██╗██╔══██╗██║     ██╔════╝██╔══██╗██║
██║ █╗ ██║███████║██║  ██║██║  ██║██║     █████╗  ███████║██║
██║███╗██║██╔══██║██║  ██║██║  ██║██║     ██╔══╝  ██╔══██║██║
╚███╔███╔╝██║  ██║██████╔╝██████╔╝███████╗███████╗██║  ██║██║
 ╚══╝╚══╝ ╚═╝  ╚═╝╚═════╝ ╚═════╝ ╚══════╝╚══════╝╚═╝  ╚═╝╚═╝
```

</div>

**Intelligent AI Gateway Management Platform for MarchProxy AILB**

WaddleAI is an enterprise-grade management platform that orchestrates AI provider connections, Ollama model deployments, and intelligent routing through MarchProxy's AI Load Balancer (AILB). It provides LiteLLM-style usage tracking, quota management, and model-aware load balancing for production AI infrastructure.

---

## 🎯 Key Features

### AI Provider Management
- 🔌 **7 Provider Support**: OpenAI/ChatGPT, Anthropic/Claude, Google Gemini, AWS Bedrock, Azure OpenAI, Cohere, and Ollama
- 🔄 **Auto-Sync to AILB**: Automatic route creation and updates in MarchProxy
- 🎯 **Model Aliases**: Use friendly names like "chatgpt" → "gpt-4o", "claude" → "claude-3-5-sonnet-latest"
- ⚡ **Health Monitoring**: Continuous provider health checks and failover

### Ollama Orchestration
- 🚀 **Model-Specific Routing**: Assign models to specific nodes (llama3.2 → node-1, mistral → node-2)
- 🐳 **Deployment Management**: Docker, Kubernetes, or external Ollama instances
- ⚖️ **MetalLB Integration**: Generate LoadBalancer Services with per-model IPs
- 📦 **Model Lifecycle**: Pull, remove, and track models across deployments

### Usage Tracking & Quotas
- 📊 **LiteLLM-Style Tracking**: Token usage, costs, and analytics
- 🪙 **WaddleAI Tokens**: Normalized billing units across all providers
- 💰 **Budget Enforcement**: Daily/monthly limits per user, organization, or key
- 📈 **Detailed Analytics**: Cost breakdowns by model, provider, user, and time

### Virtual Key Management
- 🔑 **API Key Generation**: Create virtual keys with granular permissions
- 🚦 **Rate Limiting**: RPM/TPM limits synced to AILB
- 🎫 **Model/Provider Restrictions**: Limit keys to specific models or providers
- ⏰ **Expiration & Rotation**: Automatic key expiration and rotation support

### Enterprise Features
- 🏢 **Multi-Tenancy**: Organization-based isolation
- 👥 **RBAC**: Admin, Maintainer, Viewer roles with OAuth2-style scopes
- 📝 **Audit Logging**: Comprehensive security and usage event logging
- 🔔 **Webhook Events**: Real-time usage events from AILB
- 🔐 **Flask-Security-Too**: Industry-standard authentication with JWT + 2FA

---

## 🏗️ Architecture

WaddleAI **delegates AI proxying to MarchProxy AILB** while managing:

```
┌─────────────────┐      gRPC       ┌──────────────────┐
│   WaddleAI      │◄────────────────►│  MarchProxy      │
│   Management    │  Route Sync      │  AILB Module     │
│                 │  Rate Limits     │                  │
└─────────────────┘                  └──────────────────┘
        │                                     │
        │ Manages                             │ Routes AI
        ▼                                     ▼
┌─────────────────┐                  ┌──────────────────┐
│  Ollama Nodes   │                  │  AI Providers    │
│  - llama3.2     │                  │  - OpenAI        │
│  - mistral      │                  │  - Anthropic     │
│  - codellama    │                  │  - Gemini, etc.  │
└─────────────────┘                  └──────────────────┘
```

**Components**:
- **Management Service**: Flask REST API for configuration, monitoring, and analytics
- **MarchProxy AILB**: High-performance AI load balancer (separate deployment)
- **Ollama Deployments**: Self-hosted LLM instances managed by WaddleAI

---

## 🚀 Quick Start

### Prerequisites

- Docker & Docker Compose
- MarchProxy AILB (running separately) - [Installation Guide](https://github.com/penguintechinc/marchproxy)
- PostgreSQL 15+ (included in docker-compose)

### Installation

```bash
# Clone repository
git clone https://github.com/penguintechinc/waddleai.git
cd waddleai

# Create environment file
cat > .env << EOF
# Database
POSTGRES_PASSWORD=$(openssl rand -hex 16)

# Authentication
JWT_SECRET=$(openssl rand -hex 32)
FLASK_SECRET_KEY=$(openssl rand -hex 32)

# MarchProxy AILB Integration
MARCHPROXY_AILB_HOST=localhost
MARCHPROXY_AILB_GRPC_PORT=50051
MARCHPROXY_AILB_HTTP_PORT=8080

# Webhook Secret (shared with AILB)
WEBHOOK_SECRET=$(openssl rand -hex 16)
EOF

# Start services
docker compose up -d

# Check status
docker compose ps
```

### First Steps

**1. Access Management API**
```bash
# Health check
curl http://localhost:8001/healthz

# Get API documentation
curl http://localhost:8001/api/v1/
```

**2. Login (Default Credentials)**
```bash
# Login to get JWT token
curl -X POST http://localhost:8001/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}'
```

**3. Add AI Provider**
```bash
# Add OpenAI provider
curl -X POST http://localhost:8001/api/v1/providers \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "OpenAI GPT-4",
    "provider_type": "openai",
    "endpoint_url": "https://api.openai.com/v1",
    "api_key": "sk-your-openai-key",
    "model_list": ["gpt-4o", "gpt-4", "gpt-3.5-turbo"],
    "enabled": true,
    "ailb_sync_enabled": true
  }'
```

**4. Export MarchProxy Configuration**
```bash
# Generate import config for MarchProxy
curl http://localhost:8001/api/v1/ailb/marchproxy-import-config?download=true \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -o marchproxy-import.json

# Import to MarchProxy AILB
curl -X POST http://localhost:8080/api/v1/services/import \
  -H "Content-Type: application/json" \
  -d @marchproxy-import.json
```

---

## 📚 Core Workflows

### Model-Specific Ollama Routing

**Scenario**: Route llama3.2 to node-1, mistral to node-2

```bash
# 1. Create Ollama deployments
curl -X POST http://localhost:8001/api/v1/ollama/deployments \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -d '{
    "name": "node-1",
    "endpoint_url": "http://ollama-node-1:11434",
    "deployment_type": "kubernetes"
  }'

curl -X POST http://localhost:8001/api/v1/ollama/deployments \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -d '{
    "name": "node-2",
    "endpoint_url": "http://ollama-node-2:11434",
    "deployment_type": "kubernetes"
  }'

# 2. Assign models to nodes
curl -X POST http://localhost:8001/api/v1/ollama/models/bulk-assign \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -d '{
    "assignments": [
      {"deployment_id": 1, "model_name": "llama3.2"},
      {"deployment_id": 2, "model_name": "mistral"},
      {"deployment_id": 1, "model_name": "codellama"}
    ],
    "sync_to_ailb": true
  }'

# 3. Export MetalLB configuration
curl http://localhost:8001/api/v1/ollama/export/metallb-all \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -o ollama-metallb.yml

# Apply to Kubernetes
kubectl apply -f ollama-metallb.yml
```

**Result**: MarchProxy AILB routes requests to the correct Ollama node based on model name.

### Virtual Key with Quota

```bash
# Create virtual key with budget and rate limits
curl -X POST http://localhost:8001/api/v1/keys \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -d '{
    "name": "Dev Team Key",
    "user_id": 1,
    "organization_id": 1,
    "allowed_models": ["gpt-4o", "claude-3-5-sonnet-latest"],
    "budget_limit_daily": 10.0,
    "budget_limit_monthly": 200.0,
    "rpm_limit": 60,
    "tpm_limit": 50000
  }'

# Key automatically synced to AILB with rate limits
```

### Usage Analytics

```bash
# Get usage summary
curl http://localhost:8001/api/v1/usage/summary?days=30 \
  -H "Authorization: Bearer $JWT_TOKEN"

# Cost breakdown by model
curl http://localhost:8001/api/v1/usage/by-model?days=7 \
  -H "Authorization: Bearer $JWT_TOKEN"

# Export usage to CSV
curl http://localhost:8001/api/v1/usage/export?format=csv&days=30 \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -o usage-report.csv
```

---

## 🔧 Configuration

### Environment Variables

```bash
# Database
DATABASE_URL=postgresql://waddleai:password@postgres:5432/waddleai
DB_TYPE=postgres

# Authentication
JWT_SECRET=your-jwt-secret-min-32-chars
FLASK_SECRET_KEY=your-flask-secret-key

# Cache
REDIS_URL=redis://redis:6379/0

# MarchProxy AILB Integration
MARCHPROXY_AILB_HOST=localhost
MARCHPROXY_AILB_GRPC_PORT=50051
MARCHPROXY_AILB_HTTP_PORT=8080
MARCHPROXY_AILB_TLS_ENABLED=false

# Webhook Authentication
WEBHOOK_SECRET=shared-secret-for-ailb-webhooks
WEBHOOK_CALLBACK_URL=http://waddleai-mgmt:8001/api/v1/webhooks/ailb/usage

# Ollama Management
OLLAMA_MANAGEMENT_MODE=both  # manual, orchestrated, or both
DOCKER_HOST=unix:///var/run/docker.sock

# Feature Flags
ENABLE_OLLAMA_MANAGEMENT=true
ENABLE_USAGE_WEBHOOKS=true
ENABLE_GEMINI=true
ENABLE_BEDROCK=true
ENABLE_AZURE_OPENAI=true
ENABLE_COHERE=true
```

### Supported Databases

- **PostgreSQL** 15+ (recommended for production)
- **MySQL** 8.0+ / **MariaDB** 10.6+ (including Galera clusters)
- **SQLite** 3.35+ (development only)

---

## 📖 API Reference

### Core Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/providers` | GET, POST | AI provider management |
| `/api/v1/ollama/deployments` | GET, POST | Ollama deployment CRUD |
| `/api/v1/ollama/models/assign` | POST | Assign model to node |
| `/api/v1/keys` | GET, POST | Virtual key management |
| `/api/v1/usage/summary` | GET | Usage analytics |
| `/api/v1/ailb/marchproxy-import-config` | GET | Export MarchProxy config |
| `/api/v1/ailb/ollama-routing-table` | GET | Model routing table |

**Full API Documentation**: See [docs/api/README.md](docs/api/README.md)

---

## 🐳 Production Deployment

### Docker Multi-Arch

WaddleAI supports **amd64** and **arm64** architectures:

```bash
# Build for multiple architectures
docker buildx build --platform linux/amd64,linux/arm64 \
  -t waddleai/management:latest \
  -f services/management/Dockerfile \
  services/management/
```

### Kubernetes

```bash
# Generate Kubernetes manifests
kubectl create namespace waddleai

# Deploy management service
kubectl apply -f deployment/kubernetes/management.yml

# Deploy Ollama with MetalLB
curl http://waddleai-mgmt:8001/api/v1/ollama/export/metallb-all | kubectl apply -f -
```

### MarchProxy Integration

**1. Configure AILB Module** (in MarchProxy):
```yaml
modules:
  ailb:
    enabled: true
    grpc_port: 50051
    http_port: 8080
    webhook_url: http://waddleai-mgmt:8001/api/v1/webhooks/ailb/usage
    webhook_secret: your-shared-secret
```

**2. Export WaddleAI Config**:
```bash
curl http://waddleai-mgmt:8001/api/v1/ailb/marchproxy-import-config \
  -o /path/to/marchproxy/config/waddleai-import.json
```

**3. Import to MarchProxy**:
```bash
curl -X POST http://marchproxy:8080/api/v1/services/import \
  -d @waddleai-import.json
```

---

## 🛠️ Development

### Setup

```bash
# Create virtual environment
python3.13 -m venv venv
source venv/bin/activate

# Install dependencies
cd services/management
pip install -r requirements.txt

# Run development server
FLASK_ENV=development python wsgi.py
```

### Generate Proto Files

```bash
# Generate gRPC stubs from MarchProxy proto files
./scripts/generate_proto.sh
```

### Testing

```bash
# Unit tests
pytest tests/unit/

# Smoke tests
./tests/smoke/test_management_build.sh

# Build and test Docker image
docker build -t waddleai-mgmt:test -f services/management/Dockerfile services/management/
```

---

## 📊 Monitoring

### Health Checks

```bash
# Management service health
curl http://localhost:8001/healthz

# Readiness check
curl http://localhost:8001/readyz
```

### Metrics

WaddleAI exposes Prometheus-compatible metrics:

```bash
# Management service metrics (future feature)
curl http://localhost:8001/metrics
```

**Grafana Dashboards**: See `deployment/monitoring/grafana/`

---

## 🤝 Integration

### MarchProxy AILB

WaddleAI is designed to work with [MarchProxy](https://github.com/penguintechinc/marchproxy) AILB module:

- **Route Sync**: Automatic provider and model route creation
- **Rate Limits**: Virtual key limits enforced at the gateway
- **Usage Webhooks**: Real-time usage events for tracking
- **Health Checks**: Continuous monitoring of AI providers

### Ollama

Deploy and manage Ollama instances:

- **Docker**: Direct container management via Docker API
- **Kubernetes**: Generate manifests and MetalLB Services
- **Manual**: Export docker-compose.yml for manual deployment

### MetalLB

Generate LoadBalancer Services for model-specific routing:

```bash
# Per-model LoadBalancer IPs
llama3.2 → 192.168.1.100:11434
mistral  → 192.168.1.101:11434
```

---

## 📝 Documentation

- **[Installation Guide](docs/DEVELOPMENT.md)** - Setup instructions
- **[Testing Guide](docs/TESTING.md)** - Testing and validation
- **[Pre-Commit Checklist](docs/PRE_COMMIT.md)** - Before committing
- **[Development Standards](docs/STANDARDS.md)** - Coding standards
- **[API Reference](docs/api/)** - Complete API documentation

---

## 🔒 Security

- **Flask-Security-Too**: Industry-standard authentication
- **JWT Tokens**: Secure API authentication
- **RBAC**: Role-based access control
- **Audit Logging**: Comprehensive security event logging
- **Input Validation**: All inputs sanitized and validated
- **TLS Support**: HTTPS for all API endpoints

---

## 📄 License

**Limited AGPL-3.0** with commercial use restrictions

- ✅ **Free for Personal/Internal Use**
- ✅ **Open Source Contributions Welcome**
- ⚠️ **Commercial/SaaS Requires License**
- 🏢 **Contributor Employer Exception** (GPL-2.0 grant)

See [LICENSE.md](LICENSE.md) for details.

---

## 🙏 Acknowledgments

- [MarchProxy](https://github.com/penguintechinc/marchproxy) - High-performance AI load balancer
- [Ollama](https://ollama.ai) - Local LLM deployment
- Flask + PyDAL teams for excellent frameworks
- OpenAI, Anthropic, Google for API specifications

---

## 🆘 Support

- **Documentation**: [docs/](docs/)
- **Issues**: [GitHub Issues](https://github.com/penguintechinc/waddleai/issues)
- **Email**: support@penguintech.io
- **Company**: [www.penguintech.io](https://www.penguintech.io)

---

**Maintained by [Penguin Tech Inc](https://www.penguintech.io)** | **Integrates with [MarchProxy](https://github.com/penguintechinc/marchproxy)**
