# WaddleAI Testing Setup with OpenWebUI

This guide provides instructions for setting up a complete testing environment with WaddleAI proxy server and OpenWebUI for comprehensive LLM testing.

## 🚀 Quick Start

### Prerequisites
- Docker and Docker Compose installed
- At least 4GB RAM available for containers
- Ports 3001, 8000, 8001 available on your system

### 1. Environment Setup
```bash
# Copy environment template
cp .env.testing .env

# Edit .env file with your configuration
# At minimum, set your WaddleAI API key:
# WADDLEAI_API_KEY=wa-your-api-key-here
```

### 2. Launch Testing Environment
```bash
# Start all services
docker-compose -f docker-compose.testing.yml up -d

# Check service status
docker-compose -f docker-compose.testing.yml ps
```

### 3. Access Interfaces

| Service | URL | Purpose |
|---------|-----|---------|
| **OpenWebUI** | http://localhost:3001 | Modern chat interface for testing |
| **WaddleAI Proxy** | http://localhost:8000 | OpenAI-compatible API endpoint |
| **WaddleAI Management** | http://localhost:8001 | Admin and monitoring interface |
| **Documentation** | http://localhost:8080 | WaddleAI documentation |
| **Website** | http://localhost:3000 | Marketing website |

## 🧪 Testing Scenarios

### OpenWebUI Testing
1. **First Time Setup**:
   - Go to http://localhost:3001
   - Create an account (signup enabled in testing)
   - OpenWebUI will automatically detect WaddleAI models

2. **Model Testing**:
   - Test different models: GPT-4, Claude, LLaMA, etc.
   - Verify model switching works correctly
   - Check response streaming functionality

3. **Advanced Features**:
   - Upload documents for RAG testing
   - Test conversation memory
   - Verify chat history persistence

### API Testing
```bash
# Test WaddleAI health endpoint
curl http://localhost:8000/health

# List available models
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer your-api-key"

# Test chat completion
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hello, World!"}],
    "stream": false
  }'
```

### VS Code Extension Testing
1. **Setup Extension**:
   - Open `/vscode-extension/waddleai-copilot/` in VS Code
   - Press F5 to launch Extension Development Host
   - Configure API key: "WaddleAI: Set API Key"

2. **Test Chat Participant**:
   - Open VS Code Chat panel
   - Type `@waddleai Hello, can you help me code?`
   - Verify responses stream correctly

## 🔧 Configuration Options

### WaddleAI Proxy Settings
- `SECURITY_POLICY`: balanced, strict, or permissive
- `CORS_ALLOWED_ORIGINS`: Configure for your domain
- `OPENAI_COMPATIBILITY_MODE`: Enable full OpenAI API compatibility

### OpenWebUI Settings
- `ENABLE_SIGNUP`: Allow new user registration
- `DEFAULT_USER_ROLE`: Default permissions for new users
- `ENABLE_MODEL_FILTER`: Filter available models
- `RAG_EMBEDDING_ENGINE`: Configure document processing

## 🐛 Troubleshooting

### Common Issues

**OpenWebUI can't connect to WaddleAI**:
```bash
# Check if WaddleAI proxy is healthy
docker-compose -f docker-compose.testing.yml exec openwebui curl http://waddleai-proxy:8000/health
```

**Models not appearing in OpenWebUI**:
- Verify API key is set correctly
- Check WaddleAI proxy logs: `docker-compose -f docker-compose.testing.yml logs waddleai-proxy`

**Database connection issues**:
```bash
# Check PostgreSQL health
docker-compose -f docker-compose.testing.yml exec postgres pg_isready -U waddleai
```

### Logs and Debugging
```bash
# View all logs
docker-compose -f docker-compose.testing.yml logs

# View specific service logs
docker-compose -f docker-compose.testing.yml logs waddleai-proxy
docker-compose -f docker-compose.testing.yml logs openwebui

# Follow logs in real-time
docker-compose -f docker-compose.testing.yml logs -f waddleai-proxy
```

## 🧹 Cleanup

### Stop Services
```bash
# Stop all containers
docker-compose -f docker-compose.testing.yml down

# Stop and remove volumes (WARNING: Deletes all data)
docker-compose -f docker-compose.testing.yml down -v
```

### Reset Environment
```bash
# Complete cleanup
docker-compose -f docker-compose.testing.yml down -v --remove-orphans
docker system prune -f
```

## 🏗️ Architecture Overview

```
┌─────────────────┐    ┌─────────────────┐    ┌──────────────────┐
│   OpenWebUI     │────│  WaddleAI Proxy │────│   LLM Providers  │
│  (Port 3001)    │    │   (Port 8000)   │    │ (GPT, Claude,etc)│
└─────────────────┘    └─────────────────┘    └──────────────────┘
         │                       │
         │              ┌─────────────────┐
         │              │ WaddleAI Mgmt   │
         │              │  (Port 8001)    │
         │              └─────────────────┘
         │                       │
    ┌─────────────────────────────────────┐
    │           PostgreSQL + Redis        │
    │         (Ports 5432, 6379)          │
    └─────────────────────────────────────┘
```

## 🚀 Production Deployment

For production deployment:
1. Use `docker-compose.yml` instead of `docker-compose.testing.yml`
2. Set secure passwords in `.env`
3. Configure proper SSL/TLS certificates
4. Set up monitoring and backup strategies
5. Review security policies and CORS settings

## 📝 API Compatibility

WaddleAI provides OpenAI-compatible endpoints:
- `/v1/models` - List available models
- `/v1/chat/completions` - Chat completions with streaming
- `/v1/completions` - Text completions
- `/v1/embeddings` - Text embeddings (if supported)

This ensures compatibility with:
- OpenWebUI
- VS Code Extension
- OpenAI Python/JavaScript clients
- Any OpenAI-compatible tool

## llama.cpp Integration Testing

### Prerequisites

A running llama-server. Quick local setup via Docker:

```bash
docker run -p 8080:8080 ghcr.io/ggerganov/llama.cpp:server \
    --hf-repo ggml-org/models --hf-file tinyllamas/stories15M-q8_0.gguf \
    --port 8080 --host 0.0.0.0
```

### Running integration tests

```bash
export LLAMACPP_ENDPOINT=http://localhost:8080
pytest tests/integration/test_llamacpp_integration.py -v
```

Without `LLAMACPP_ENDPOINT`, only `test_llamacpp_connector_importable` runs (always passes).