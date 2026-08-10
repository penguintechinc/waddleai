# WaddleAI Testing Setup with OpenWebUI

This guide provides instructions for setting up a manual testing environment with the WaddleAI proxy server and OpenWebUI for interactive LLM testing.

## 🚀 Quick Start

### Prerequisites
- Docker installed
- At least 4GB RAM available for containers
- Ports 3000, 3001, 8001, 8080 available on your system
- WaddleAI's own Postgres + Valkey dependencies and the management/proxy services running — see [Local Development Guide](DEVELOPMENT.md) to bring those up first

### 1. Environment Setup

Export the values OpenWebUI needs to reach the proxy — there is no `.env.testing` template in this repo, so set these directly:

```bash
export WADDLEAI_API_KEY=wa-your-api-key-here
```

### 2. Launch OpenWebUI

WaddleAI does not ship a Compose file for this (Docker Compose is deprecated for this project) — run OpenWebUI as a single standalone container pointed at the proxy you already have running on the host:

```bash
docker run -d --name openwebui \
  -p 3001:8080 \
  -e OPENAI_API_BASE_URL=http://host.docker.internal:8080/v1 \
  -e OPENAI_API_KEY="$WADDLEAI_API_KEY" \
  -e WEBUI_AUTH=true \
  -v openwebui-data:/app/backend/data \
  ghcr.io/open-webui/open-webui:main
```

`host.docker.internal` resolves to the host from inside the container on Docker Desktop (macOS/Windows). On Linux, either add `--add-host=host.docker.internal:host-gateway` to the `docker run` command above, or use the host's LAN/bridge IP directly.

Check it started:
```bash
docker ps --filter name=openwebui
docker logs -f openwebui
```

### 3. Access Interfaces

| Service | URL | Purpose |
|---------|-----|---------|
| **OpenWebUI** | http://localhost:3001 | Chat interface for manual testing |
| **WaddleAI Proxy** | http://localhost:8080 | OpenAI-compatible API endpoint |
| **WaddleAI Management** | http://localhost:8001 | Admin and monitoring interface |
| **Web UI** | http://localhost:3000 | WaddleAI's own management frontend (`npm run dev` in `services/webui`) |

## 🧪 Testing Scenarios

### OpenWebUI Testing
1. **First Time Setup**:
   - Go to http://localhost:3001
   - Create an account (first account becomes admin)
   - OpenWebUI detects models via the proxy's `/v1/models` endpoint

2. **Model Testing**:
   - Test different configured backends (OpenAI, Anthropic, Ollama, etc. — whatever is configured in the management service)
   - Verify model switching works correctly
   - Check response streaming functionality

3. **Advanced Features**:
   - Upload documents for RAG testing
   - Test conversation memory
   - Verify chat history persistence

### API Testing
```bash
# Test WaddleAI proxy health endpoint
curl http://localhost:8080/healthz

# List available models
curl http://localhost:8080/v1/models \
  -H "Authorization: Bearer $WADDLEAI_API_KEY"

# Test chat completion
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $WADDLEAI_API_KEY" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hello, World!"}],
    "stream": false
  }'
```

### VS Code Extension Testing
1. **Setup Extension**:
   - Open `vscode-extension/waddleai-copilot/` in VS Code
   - Press F5 to launch Extension Development Host
   - Configure API key: "WaddleAI: Set API Key"

2. **Test Chat Participant**:
   - Open VS Code Chat panel
   - Type `@waddleai Hello, can you help me code?`
   - Verify responses stream correctly

See `vscode-extension/waddleai-copilot/TESTING.md` for the extension's own test suite.

## 🔧 Configuration Options

### WaddleAI Proxy Settings
- `SECURITY_POLICY`: `balanced` (default), `strict`, or `permissive`
- `MANAGEMENT_SERVER_URL`: where the proxy reaches the management service (default `http://localhost:8001`)
- `GRPC_PORT` / `HTTP_PORT`: proxy listen ports (defaults `50051` / `8080`)

### OpenWebUI Settings
Pass these as additional `-e` flags on the `docker run` command above:
- `ENABLE_SIGNUP`: allow new user registration
- `DEFAULT_USER_ROLE`: default permissions for new users
- `ENABLE_MODEL_FILTER`: filter available models
- `RAG_EMBEDDING_ENGINE`: configure document processing

## 🐛 Troubleshooting

### Common Issues

**OpenWebUI can't connect to WaddleAI**:
```bash
# Confirm the proxy is healthy and reachable from inside the container
docker exec openwebui curl -f http://host.docker.internal:8080/healthz
```

**Models not appearing in OpenWebUI**:
- Verify `WADDLEAI_API_KEY` is set correctly in the OpenWebUI container's environment
- Check the proxy's own logs (the terminal running `hypercorn apps.proxy_server.main:app`, or `docker logs waddleai-proxy` if running the built image)

**Database connection issues**:
```bash
docker exec waddleai-postgres pg_isready -U waddleai
```

### Logs and Debugging
```bash
# OpenWebUI logs
docker logs -f openwebui

# WaddleAI proxy/management logs — the terminal each is running in
# (or, if running the built images):
docker logs -f waddleai-proxy
docker logs -f waddleai-management
```

## 🧹 Cleanup

```bash
docker rm -f openwebui
docker volume rm openwebui-data   # WARNING: deletes OpenWebUI's chat history/accounts
```

## 🏗️ Architecture Overview

```
┌─────────────────┐    ┌─────────────────┐    ┌──────────────────┐
│   OpenWebUI     │────│  WaddleAI Proxy │────│   LLM Providers  │
│  (Port 3001)    │    │   (Port 8080)   │    │ (GPT, Claude,etc)│
└─────────────────┘    └─────────────────┘    └──────────────────┘
         │                       │
         │              ┌─────────────────┐
         │              │ WaddleAI Mgmt   │
         │              │  (Port 8001)    │
         │              └─────────────────┘
         │                       │
    ┌─────────────────────────────────────┐
    │        PostgreSQL + Valkey          │
    │        (Ports 5432, 6379)           │
    └─────────────────────────────────────┘
```

## 🚀 Production Deployment

Production and beta deployments use the Helm chart at `k8s/helm/waddleai` — see `DEPLOY_K8S.md`. This page covers local/manual testing only.

## 📝 API Compatibility

WaddleAI's proxy provides OpenAI-compatible endpoints:
- `/v1/models` - List available models
- `/v1/chat/completions` - Chat completions with streaming

This ensures compatibility with:
- OpenWebUI
- The WaddleAI VS Code extension
- OpenAI Python/JavaScript clients
- Any OpenAI-compatible tool

## llama.cpp Integration Testing

### Prerequisites

A running llama-server. Quick local setup via Docker:

```bash
docker run -p 8090:8080 ghcr.io/ggerganov/llama.cpp:server \
    --hf-repo ggml-org/models --hf-file tinyllamas/stories15M-q8_0.gguf \
    --port 8080 --host 0.0.0.0
```

(Mapped to host port `8090` here to avoid colliding with the WaddleAI proxy on `8080`.)

### Running integration tests

```bash
export LLAMACPP_ENDPOINT=http://localhost:8090
pytest tests/integration/test_llamacpp_integration.py -v
```

Without `LLAMACPP_ENDPOINT`, only `test_llamacpp_connector_importable` runs (always passes).
