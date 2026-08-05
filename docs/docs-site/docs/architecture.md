# System Architecture

WaddleAI is a sophisticated LLM proxy server with intelligent routing, memory integration, and advanced security features built on a high-performance architecture.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client Applications                       │
│   (Claude Code, Cursor IDE, OpenAI SDK, Custom Apps, MCP)      │
└───────────────┬─────────────────────────────────────────────────┘
                │
                │ HTTPS/WSS
                ▼
┌───────────────────────────────────────────────────────────────────┐
│                    XDP/AF_XDP Layer (Optional)                    │
│  ● Kernel-level packet processing for DDoS protection            │
│  ● Rate limiting at packet level (10-100x faster)                │
│  ● Zero-copy WebSocket acceleration                              │
└───────────────┬───────────────────────────────────────────────────┘
                │
                ▼
┌───────────────────────────────────────────────────────────────────┐
│                        WaddleAI Proxy Server                      │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  API Endpoints Layer                                        │ │
│  │  ● /v1/chat/completions (OpenAI)                           │ │
│  │  ● /v1/messages (Claude Messages API)                      │ │
│  │  ● /v1/models, /v1/embeddings                              │ │
│  │  ● MCP WebSocket endpoint                                  │ │
│  └─────────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  Security Layer                                             │ │
│  │  ● API Key Authentication & RBAC                           │ │
│  │  ● Prompt injection detection (pattern + heuristic)        │ │
│  │  ● Content filtering                                       │ │
│  │  ● Rate limiting & quota enforcement                       │ │
│  └─────────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  Intelligent Routing Layer                                  │ │
│  │  ● Routing LLM (llama3.2:1b / o1-mini)                    │ │
│  │  ● Redis-based routing instructions                        │ │
│  │  ● Request classification (programming, analysis, chat)    │ │
│  │  ● Model selection hierarchy (request → key → user → org)  │ │
│  └─────────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  Connection Link Abstraction                                │ │
│  │  ● Provider adapters (OpenAI, Anthropic, Ollama)          │ │
│  │  ● Failover & load balancing                               │ │
│  │  ● Response streaming support                              │ │
│  └─────────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  Token Tracking & Usage Logging                             │ │
│  │  ● Dual token system (WaddleAI + LLM tokens)              │ │
│  │  ● Real-time usage tracking                                │ │
│  │  ● Cost calculation                                        │ │
│  └─────────────────────────────────────────────────────────────┘ │
└────┬──────────────┬──────────────┬──────────────┬───────────────┘
     │              │              │              │
     ▼              ▼              ▼              ▼
┌─────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐
│ Redis   │  │ PostgreSQL│  │ ChromaDB │  │ LLM Providers│
│ Cache   │  │ Database  │  │ (mem0)   │  │ ● OpenAI     │
│         │  │           │  │          │  │ ● Anthropic  │
│ Routing │  │ Users     │  │ Memory   │  │ ● Ollama     │
│ Rules   │  │ API Keys  │  │ Context  │  │ ● Azure      │
│         │  │ Usage Log │  │ Search   │  │              │
└─────────┘  └──────────┘  └──────────┘  └──────────────┘
                    │
                    ▼
     ┌──────────────────────────────────┐
     │  WaddleAI Management Portal      │
     │  ● Web-based configuration       │
     │  ● Multi-tenant org management   │
     │  ● RBAC (admin, resource mgr)    │
     │  ● Analytics & dashboards        │
     │  ● Provider & routing config     │
     └──────────────────────────────────┘
```

## Core Components

### 1. XDP/AF_XDP Acceleration Layer (Optional)

**Purpose**: Kernel-level packet processing for extreme performance

**Features**:
- **DDoS Protection**: Filter malicious packets before they reach userspace
- **Rate Limiting**: Enforce rate limits at packet level (10-100x faster than application-level)
- **Zero-Copy WebSocket**: AF_XDP sockets for WebSocket connections without memory copying
- **Packet Filtering**: Early rejection of invalid requests

**Technology**: eBPF/XDP programs, AF_XDP sockets

**When to Use**: High-traffic production deployments, DDoS-prone environments

### 2. Proxy Server (Stateless)

**Purpose**: Primary request handling and routing

**Key Characteristics**:
- **Stateless Design**: No persistent state; can scale horizontally
- **Multi-Protocol Support**: OpenAI API, Claude Messages API, MCP
- **Async Processing**: High-performance async request handling
- **Security First**: Multiple layers of security checks

**Technology**: Python 3.13, py4web, asyncio, aiohttp

### 3. Intelligent Routing System

**Purpose**: Smart model selection based on request type

**Components**:
- **Routing LLM**: Fast model (llama3.2:1b or o1-mini) for classification
- **Redis Cache**: Stores routing instructions and decisions
- **Classification Engine**: Analyzes requests to determine type
- **Model Selection Hierarchy**:
  1. Request override (`model` parameter)
  2. API key default model
  3. User default model
  4. Organization default model
  5. Routing LLM decision
  6. System default

**Example Routing Logic**:
```
Programming query → CodeLlama / Claude
Analysis query → GPT-4
Simple chat → Ollama (local, fast)
```

### 4. Memory Integration (mem0 + ChromaDB)

**Purpose**: Context retention and conversation history

**Features**:
- **Semantic Search**: Find related conversations
- **Long-term Memory**: Store and retrieve conversation context
- **User/Org Isolation**: Secure multi-tenant memory
- **Metadata Storage**: Routing decisions, tokens, performance metrics

**Use Cases**:
- Context-aware routing
- Conversation analytics
- User preference learning

### 5. Management Portal

**Purpose**: Web-based configuration and monitoring

**Features**:
- **Multi-tenant Management**: Organizations, users, API keys
- **RBAC**: Admin, resource manager, reporter, user roles
- **Provider Configuration**: Add/edit LLM provider connections
- **Routing Configuration**: Set routing instructions via Redis
- **Analytics Dashboard**: Token usage, costs, performance metrics
- **Real-time Monitoring**: Active connections, request stats

**Technology**: py4web, Tailwind CSS, Alpine.js, Chart.js

## Data Flow

### Request Flow

1. **Client Request** → Proxy Server
2. **XDP Layer** (optional) → Packet filtering and rate limiting
3. **Authentication** → API key validation
4. **Security Scan** → Prompt injection detection
5. **Quota Check** → Verify user/org limits
6. **Model Selection**:
   - Check request override
   - Check API key default
   - Check user/org defaults
   - Use routing LLM if needed
7. **Provider Request** → Send to selected LLM
8. **Response Processing** → Token counting, logging
9. **Memory Storage** → Save to ChromaDB (if enabled)
10. **Client Response** → Return to client

### Token Tracking Flow

WaddleAI implements a **dual token system**:

1. **WaddleAI Tokens**: Internal accounting units
2. **LLM Tokens**: Actual tokens used by providers

**Conversion Example**:
```
Request: 1000 input tokens, 500 output tokens
Provider: GPT-4 (expensive)
WaddleAI Tokens: 1500 × 1.2 (markup) = 1800
LLM Tokens: 1500 (actual)
Cost: $0.015 (calculated)
```

## Security Architecture

### Multi-Layer Security

1. **XDP Layer**: Packet-level filtering
2. **Network Layer**: TLS/SSL encryption
3. **Application Layer**: API key authentication, RBAC
4. **Content Layer**: Prompt injection detection, content filtering
5. **Output Layer**: Output guardrails — PII/credential masking on LLM responses
6. **Rate Limiting**: XDP + application-level rate limits
7. **Quota Enforcement**: Daily/monthly token limits

### AI-Specific Security Considerations

WaddleAI's LLM proxy architecture introduces threat vectors beyond traditional web security. The three highest-priority AI-specific risks are:

| Threat | Component at Risk | Status |
|--------|------------------|--------|
| **Indirect Prompt Injection** | mem0/ChromaDB memory injection | Requires hardening |
| **Semantic Cache Poisoning** | Redis routing cache | Requires hardening |
| **Insecure Output Handling** | All LLM response paths | Requires output guardrail layer |

> **See [AI Security Recommendations](administration/ai-security-recommendations.md)** for full threat model, OWASP LLM Top 10 mapping, implementation patterns, and Kubernetes hardening guidance (Cilium Tetragon, Kyverno, SPIFFE/SPIRE).

### RBAC Roles

| Role | Permissions |
|------|-------------|
| **Admin** | Full system access, configuration, all organizations |
| **Resource Manager** | Manage organization users, view analytics |
| **Reporter** | View analytics for organization |
| **User** | API access, view personal usage |

## Deployment Modes

### 1. Development (Docker Compose)

```bash
docker-compose -f docker-compose.env.yml up
```

**Includes**: All services, PostgreSQL, Redis, ChromaDB, Prometheus, Grafana

### 2. Production (Kubernetes)

- **Horizontal Scaling**: Multiple proxy replicas
- **Load Balancing**: Distribute requests across proxies
- **High Availability**: Redis/PostgreSQL replication
- **XDP Acceleration**: Enabled on edge nodes

### 3. Cloudflare Pages (Management Portal)

- **Static Deployment**: Management portal as static site
- **API Proxy**: Connect to backend API
- **CDN**: Fast global access

## Performance Characteristics

### Without XDP

- **Throughput**: ~1,000 requests/second per core
- **Latency**: 10-50ms overhead
- **Memory**: ~500MB per proxy instance

### With XDP

- **Throughput**: ~50,000 packets/second per core
- **Latency**: <1ms for rate limiting
- **DDoS Protection**: Drop malicious packets at kernel level

## Monitoring & Observability

### Metrics (Prometheus)

- Request count, duration, errors
- Token usage (WaddleAI + LLM)
- Provider response times
- Routing decision accuracy
- Cache hit rates
- XDP statistics (if enabled)

### Health Checks

- `/healthz` - Proxy server health
- `/api/status` - Detailed status
- `/metrics` - Prometheus metrics

### Dashboards (Grafana)

- System performance
- Token usage trends
- Cost tracking
- Provider comparison
- Security events

## Technology Stack

| Component | Technology |
|-----------|------------|
| **Proxy Server** | Python 3.13, py4web |
| **Management Portal** | py4web, Tailwind CSS, Alpine.js |
| **Database** | PostgreSQL + PyDAL |
| **Cache** | Redis |
| **Memory** | ChromaDB + mem0 |
| **Acceleration** | XDP/eBPF, AF_XDP |
| **Monitoring** | Prometheus, Grafana |
| **Documentation** | MkDocs Material |

## Next Steps

- [Installation Guide](getting-started/installation.md)
- [Quick Start](getting-started/quick-start.md)
- [Configuration Reference](getting-started/configuration.md)
- [API Documentation](api/openai-compatible.md)