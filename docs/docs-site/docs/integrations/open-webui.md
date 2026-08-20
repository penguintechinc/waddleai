# Open WebUI Integration

Connect Open WebUI to WaddleAI for a beautiful web interface with access to all your configured LLMs.

## Overview

Open WebUI (formerly Ollama WebUI) is a feature-rich web interface for interacting with LLMs. By pointing it at WaddleAI, you get:

- **Unified LLM Access** - Use OpenAI, Claude, Ollama, and other models from one interface
- **Intelligent Routing** - WaddleAI automatically routes to the best model
- **Usage Tracking** - Monitor token usage across all conversations
- **Model Selection** - Choose models manually or let WaddleAI route intelligently
- **Conversation Memory** - All chats stored in mem0 for semantic search

## Prerequisites

- WaddleAI running and accessible
- Docker installed (for Open WebUI)
- Valid WaddleAI API key

## Quick Start

### 1. Deploy Open WebUI with Docker

```bash
docker run -d \
  --name open-webui \
  -p 3000:8080 \
  -e OPENAI_API_BASE_URL=http://host.docker.internal:8000/v1 \
  -e OPENAI_API_KEY=wa-your-api-key-here \
  -v open-webui:/app/backend/data \
  ghcr.io/open-webui/open-webui:main
```

**For Linux hosts**, use `--add-host=host.docker.internal:host-gateway` instead of `host.docker.internal`.

### 2. Access Open WebUI

Open your browser to `http://localhost:3000`

### 3. Create an Account

On first launch, create an admin account. This account is local to Open WebUI, separate from WaddleAI authentication.

### 4. Configure Models

Open WebUI will automatically fetch available models from WaddleAI's `/v1/models` endpoint.

## Configuration Options

### Environment Variables

Configure Open WebUI to work with WaddleAI:

```bash
# Required: Point to WaddleAI proxy
OPENAI_API_BASE_URL=http://host.docker.internal:8000/v1

# Required: Your WaddleAI API key
OPENAI_API_KEY=wa-your-api-key-here

# Optional: Enable experimental features
WEBUI_SECRET_KEY=your-secret-key-change-this

# Optional: Custom title
WEBUI_NAME="WaddleAI Chat"

# Optional: Enable authentication
ENABLE_SIGNUP=true
ENABLE_LOGIN_FORM=true

# Optional: Set default model
DEFAULT_MODELS=gpt-4,claude-3-opus,llama3.2

# Optional: Disable OpenAI API (use only WaddleAI)
OPENAI_API_ENABLED=true
```

### Docker Compose Setup

Create `docker-compose.open-webui.yml`:

```yaml
version: '3.8'

services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: open-webui
    ports:
      - "3000:8080"
    environment:
      - OPENAI_API_BASE_URL=http://waddleai-proxy:8000/v1
      - OPENAI_API_KEY=${WADDLEAI_API_KEY}
      - WEBUI_SECRET_KEY=${WEBUI_SECRET_KEY}
      - WEBUI_NAME=WaddleAI Chat
      - DEFAULT_MODELS=gpt-4,claude-3-opus,llama3.2
    volumes:
      - open-webui-data:/app/backend/data
    networks:
      - waddleai-network
    depends_on:
      - waddleai-proxy

  # Include your WaddleAI services here or use external network
  waddleai-proxy:
    image: waddleai/proxy:latest
    # ... rest of WaddleAI config

networks:
  waddleai-network:
    driver: bridge

volumes:
  open-webui-data:
```

Start with:

```bash
WADDLEAI_API_KEY=wa-your-key WEBUI_SECRET_KEY=secret docker-compose -f docker-compose.open-webui.yml up -d
```

## Using Open WebUI with WaddleAI

### Selecting Models

Open WebUI shows all models available through WaddleAI:

1. Click the model dropdown in the chat interface
2. Select from available models:
   - **OpenAI models**: gpt-4, gpt-3.5-turbo, etc.
   - **Claude models**: claude-3-opus, claude-3-sonnet, etc.
   - **Ollama models**: llama3.2, codellama, etc.
   - **Custom models**: Any models configured in WaddleAI

### Automatic Routing

To use WaddleAI's intelligent routing:

1. Select the model named `auto` (if configured)
2. Or use the default model setting
3. WaddleAI's routing LLM will choose the best model based on your prompt

### Model Switching Mid-Conversation

Open WebUI allows switching models within a conversation:

1. Click the model dropdown
2. Select a different model
3. The conversation continues with the new model

All messages are still stored in WaddleAI's mem0 with full context.

## Features

### 1. Chat Interface

Open WebUI provides a modern chat interface with:

- Markdown rendering
- Code syntax highlighting
- Image support (for vision models)
- File uploads
- Conversation history

### 2. Model Comparison

Compare responses from different models:

1. Create a new chat
2. Enable "Compare Models" mode
3. Select multiple models
4. See side-by-side responses

### 3. Prompt Templates

Create reusable prompt templates:

1. Go to Settings → Prompts
2. Create a new prompt template
3. Use variables like `{{question}}` for dynamic content
4. Apply templates in any chat

### 4. Document Upload

Upload documents for RAG (Retrieval Augmented Generation):

1. Click the paperclip icon
2. Upload PDF, TXT, DOCX, or other documents
3. Open WebUI extracts and indexes content
4. Ask questions about the document

### 5. Voice Input

Use speech-to-text for hands-free interaction:

1. Click the microphone icon
2. Speak your prompt
3. Open WebUI transcribes and sends to WaddleAI

### 6. Conversation Search

Search your conversation history:

1. Go to Conversations
2. Use the search bar
3. Filter by model, date, or content

## Advanced Configuration

### Custom Model Naming

Map WaddleAI model names to friendly names in Open WebUI:

```bash
# In Open WebUI settings
MODEL_NAMES='{"gpt-4":"GPT-4 (via WaddleAI)","claude-3-opus":"Claude 3 Opus","llama3.2:1b":"Llama 3.2 1B (Local)"}'
```

### Model Visibility

Control which models appear in Open WebUI:

```bash
# Show only specific models
OPENAI_API_MODELS="gpt-4,gpt-3.5-turbo,claude-3-opus"

# Or hide specific models
OPENAI_API_MODELS_EXCLUDE="text-embedding-ada-002,whisper-1"
```

### Default System Prompt

Set a default system prompt for all conversations:

```bash
DEFAULT_USER_ROLE="user"
DEFAULT_SYSTEM_PROMPT="You are a helpful AI assistant powered by WaddleAI. You have access to multiple language models and will route requests intelligently."
```

### RAG Configuration

Configure document processing:

```bash
# Enable RAG
ENABLE_RAG=true

# Chunk size for document processing
RAG_EMBEDDING_MODEL=text-embedding-ada-002
RAG_TOP_K_RESULTS=5
```

## Integration with WaddleAI Features

### Usage Tracking

All conversations through Open WebUI are tracked in WaddleAI:

1. Go to WaddleAI Management Portal (http://localhost:8001)
2. Navigate to Analytics
3. See token usage, model selection, and costs
4. Filter by user, organization, or model

### Conversation Memory

All Open WebUI conversations are stored in WaddleAI's mem0:

- Automatic conversation storage
- Semantic search across all chats
- Cross-session context retention
- Full conversation history

Query conversations via WaddleAI Management API:

```bash
curl http://localhost:8001/api/memory/conversations?user_id=your-open-webui-user
```

### Intelligent Routing

Open WebUI benefits from WaddleAI's routing LLM:

- Programming questions → codellama or Claude
- Complex reasoning → GPT-4 or Claude Opus
- Simple queries → Local Ollama models
- Analysis tasks → Specialized models

### Model Preferences

Set model preferences in WaddleAI that apply to Open WebUI:

1. Go to WaddleAI Management Portal
2. Navigate to API Keys
3. Set default model for your API key
4. Open WebUI will use this as the fallback

## Troubleshooting

### Models Not Showing Up

**Problem**: Open WebUI doesn't list any models

**Solutions**:
1. Verify WaddleAI is running: `curl http://localhost:8000/healthz`
2. Check API key is valid: `curl http://localhost:8000/v1/models -H "Authorization: Bearer $WADDLEAI_API_KEY"`
3. Restart Open WebUI container
4. Check Open WebUI logs: `docker logs open-webui`

### Connection Refused

**Problem**: "Connection refused" or "Cannot connect to API"

**Solutions**:
1. Verify WaddleAI URL is correct in `OPENAI_API_BASE_URL`
2. On Linux, use `--add-host=host.docker.internal:host-gateway`
3. Or use WaddleAI's external IP instead of `host.docker.internal`
4. Check firewall rules allow connections between containers

### Slow Responses

**Problem**: Responses take a long time to appear

**Solutions**:
1. Check WaddleAI performance metrics
2. Verify routing LLM is running (llama3.2:1b should be fast)
3. Monitor WaddleAI logs for bottlenecks
4. Consider enabling XDP acceleration in WaddleAI

### Authentication Errors

**Problem**: "Invalid API key" or 401 errors

**Solutions**:
1. Verify API key format starts with `wa-`
2. Check API key is active in WaddleAI Management Portal
3. Verify API key has quota remaining
4. Check API key hasn't expired

### Streaming Not Working

**Problem**: Responses appear all at once instead of streaming

**Solutions**:
1. Verify WaddleAI supports streaming (it should by default)
2. Check Open WebUI version supports streaming
3. Try a different browser
4. Disable any proxies or VPNs that might buffer responses

## Best Practices

### 1. Use Meaningful Conversation Names

Rename conversations in Open WebUI for easier searching later.

### 2. Leverage Model Selection

- Use GPT-4 or Claude Opus for complex reasoning
- Use codellama for programming tasks
- Use local Ollama models for quick tests
- Use `auto` routing for general queries

### 3. Organize with Tags

Tag conversations by project, topic, or purpose for easier management.

### 4. Regular Backups

Open WebUI stores data in Docker volumes. Back up regularly:

```bash
docker run --rm \
  -v open-webui:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/open-webui-backup.tar.gz /data
```

### 5. Monitor Usage

Check WaddleAI analytics regularly to:
- Track token consumption
- Identify expensive models
- Optimize model selection
- Monitor API key usage

## See Also

- [WaddleAI API Documentation](../api/openai-compatible.md)
- [Authentication Guide](../api/authentication.md)
- [Analytics Dashboard](../administration/monitoring.md)
- [Memory Systems](memory-systems.md)
- [Open WebUI Documentation](https://docs.openwebui.com)
