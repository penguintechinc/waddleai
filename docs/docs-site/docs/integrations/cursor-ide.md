# Connect Cursor IDE to WaddleAI

Configure Cursor IDE to use WaddleAI for intelligent routing and cost optimization.

## Prerequisites

- WaddleAI running
- Cursor IDE installed
- WaddleAI API key

## Quick Setup

### Step 1: Get API Key

1. Open Management Portal: http://localhost:8001
2. Generate new API key named "Cursor IDE"
3. Copy the key (starts with `wai_`)

### Step 2: Configure Cursor

1. Open Cursor Settings (`Cmd/Ctrl + ,`)
2. Search for "API Key"
3. Set:
   - **API Provider**: OpenAI Compatible
   - **Base URL**: `http://localhost:8000/v1`
   - **API Key**: `<your-waddleai-key>`
   - **Model**: Leave empty for auto-routing

### Step 3: Test

1. Open any code file
2. Press `Cmd/Ctrl + K`
3. Type "Add a function to calculate fibonacci"
4. Press Enter

Check Management Portal → Analytics to see the request!

## Advanced Configuration

### Settings JSON

Edit `~/.cursor/settings.json`:

```json
{
  "cursor.ai": {
    "apiProvider": "openai-compatible",
    "apiBaseUrl": "http://localhost:8000/v1",
    "apiKey": "<your-waddleai-key>",
    "model": "auto",
    "streamResponses": true,
    "maxTokens": 2000
  },
  "cursor.routing": {
    "enabled": true,
    "preferLocal": true,
    "costOptimization": true
  }
}
```

### Model Preferences

Set model preferences per language:

```json
{
  "cursor.modelPreferences": {
    "python": "codellama",
    "javascript": "gpt-3.5-turbo",
    "typescript": "gpt-4",
    "markdown": "claude-3-haiku",
    "default": "auto"
  }
}
```

### Context Window

Configure context window size:

```json
{
  "cursor.context": {
    "maxTokens": 8000,
    "includeOpenFiles": true,
    "includeRecentEdits": true,
    "includeDiagnostics": true
  }
}
```

## Features

### Auto-Completion

**Trigger**: Type code and pause

**Configuration**:
```json
{
  "cursor.autocomplete": {
    "enabled": true,
    "delay": 200,
    "model": "codellama",
    "maxSuggestions": 3
  }
}
```

### Chat

**Trigger**: `Cmd/Ctrl + L`

**Usage**:
```
You: Explain this function
AI: [Uses WaddleAI routing to select best model]
```

### Code Generation

**Trigger**: `Cmd/Ctrl + K`

**Example**:
```
Prompt: Create a REST API endpoint for user authentication
Model Selected: codellama (via WaddleAI routing)
```

### Debugging

**Trigger**: Right-click error → "Ask AI"

**Configuration**:
```json
{
  "cursor.debugging": {
    "model": "gpt-4",
    "includeStackTrace": true,
    "includeVariables": true
  }
}
```

## Usage Optimization

### Local Models First

Configure to use Ollama for fast operations:

```json
{
  "cursor.routing": {
    "strategy": "local_first",
    "localModels": ["codellama", "llama3.2:3b"],
    "cloudFallback": true
  }
}
```

### Cost Controls

Set budget limits in Management Portal:
1. Edit API key
2. Set daily limit: 50,000 tokens
3. Set monthly limit: 1,000,000 tokens

Cursor will receive quota errors when limit reached.

### Smart Routing

Let WaddleAI choose the best model:

| Task | WaddleAI Routes To |
|------|-------------------|
| Autocomplete | llama3.2:1b (fast) |
| Code generation | codellama |
| Complex logic | gpt-4 |
| Documentation | claude-3-haiku |

## Monitoring

### View Usage

Management Portal → Analytics:
- Requests from Cursor IDE
- Models used
- Token consumption
- Cost per session

### Export Usage

```bash
# Export Cursor IDE usage
curl -H "Authorization: Bearer <your-admin-token>" \
  "http://localhost:8001/api/analytics/export?application=cursor" \
  -o cursor_usage.csv
```

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Cmd/Ctrl + K` | Generate code |
| `Cmd/Ctrl + L` | Open chat |
| `Cmd/Ctrl + I` | Inline edit |
| `Cmd/Ctrl + Shift + L` | New chat session |

## Troubleshooting

### "API connection failed"

```bash
# Test WaddleAI
curl http://localhost:8000/healthz

# Test with your key
curl -H "Authorization: Bearer <your-waddleai-key>" \
  http://localhost:8000/v1/models
```

### "Quota exceeded"

Check Management Portal → API Keys → Your Key:
- View usage today
- Increase daily limit
- Or wait for reset

### Slow Responses

1. Check routing LLM is running
2. Verify provider connections in Management Portal
3. Check Analytics for response times

### Wrong Model Selected

View routing decisions:
1. Management Portal → Routing Configuration
2. Adjust routing instructions
3. Test decision with "Test Decision" button

## Best Practices

### 1. Use Auto-Routing

Let WaddleAI choose:
```json
{
  "cursor.ai": {
    "model": "auto"
  }
}
```

### 2. Set Reasonable Limits

```json
{
  "cursor.autocomplete": {
    "maxTokens": 500
  },
  "cursor.chat": {
    "maxTokens": 2000
  }
}
```

### 3. Use Local for Simple Tasks

```json
{
  "cursor.autocomplete": {
    "model": "codellama"
  }
}
```

### 4. Monitor Costs

- Check Analytics daily
- Set budget alerts
- Review routing decisions

## Team Configuration

### Shared Configuration

Create `.cursor/team-settings.json`:

```json
{
  "cursor.ai": {
    "apiProvider": "openai-compatible",
    "apiBaseUrl": "https://waddleai.yourcompany.com/v1",
    "model": "auto"
  },
  "cursor.routing": {
    "enabled": true,
    "preferLocal": true
  }
}
```

### Per-User API Keys

Each team member gets their own key:
1. Management Portal → Users → Add User
2. Generate API key per user
3. Track usage per developer

## Integration with WaddleAI Features

### Memory Integration

Enable conversation memory:
```json
{
  "cursor.memory": {
    "enabled": true,
    "retainConversations": true
  }
}
```

WaddleAI stores conversations in ChromaDB.

### Security Scanning

Automatic prompt injection detection:
- Enabled by default in WaddleAI
- Blocks malicious prompts
- Logs security events

### Rate Limiting

Set in Management Portal per key:
- Requests per minute
- Tokens per day
- Tokens per month

## Next Steps

- [Claude Code Integration](claude-code.md)
- [VS Code Extension](vscode-extension.md)
- [API Documentation](../api/openai-compatible.md)
