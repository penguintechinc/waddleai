# VS Code Extension Integration

WaddleAI provides seamless integration with VS Code and AI coding assistants like GitHub Copilot, Continue, and Cursor through its OpenAI-compatible proxy API.

## Overview

By configuring VS Code extensions to use WaddleAI as a proxy, you gain:

- **Centralized Token Management**: Track and control AI assistant usage across your organization
- **Cost Visibility**: Monitor token consumption and costs per developer
- **Smart Routing**: Automatically route requests to optimal LLM providers
- **Enhanced Security**: Prompt injection detection and security scanning
- **Quota Management**: Set usage limits per developer or team
- **Memory Integration**: Persistent conversation context across sessions

## Supported Extensions

### Continue (Recommended)

Continue is a powerful open-source AI coding assistant with excellent WaddleAI integration.

**Installation:**
1. Install Continue from VS Code Marketplace
2. Open Continue settings (`.continue/config.json`)
3. Configure WaddleAI proxy

**Configuration:**

```json
{
  "models": [
    {
      "title": "GPT-4 via WaddleAI",
      "provider": "openai",
      "model": "gpt-4",
      "apiKey": "wa-your-api-key-here",
      "apiBase": "https://your-waddleai-proxy.com/v1"
    },
    {
      "title": "Claude 3 Sonnet via WaddleAI",
      "provider": "openai",
      "model": "claude-3-sonnet",
      "apiKey": "wa-your-api-key-here",
      "apiBase": "https://your-waddleai-proxy.com/v1"
    },
    {
      "title": "Smart Router",
      "provider": "openai",
      "model": "smart-router",
      "apiKey": "wa-your-api-key-here",
      "apiBase": "https://your-waddleai-proxy.com/v1"
    }
  ],
  "tabAutocompleteModel": {
    "title": "Codestral via WaddleAI",
    "provider": "openai",
    "model": "codestral",
    "apiKey": "wa-your-api-key-here",
    "apiBase": "https://your-waddleai-proxy.com/v1"
  },
  "embeddingsProvider": {
    "provider": "openai",
    "model": "text-embedding-ada-002",
    "apiKey": "wa-your-api-key-here",
    "apiBase": "https://your-waddleai-proxy.com/v1"
  }
}
```

### Cursor

Cursor is an AI-first code editor based on VS Code.

**Configuration:**

1. Open Cursor Settings: `Settings > Cursor Settings > Models`
2. Add custom API endpoint
3. Configure WaddleAI proxy

**Settings JSON:**

```json
{
  "cursor.overrideModels": {
    "gpt-4": {
      "apiKey": "wa-your-api-key-here",
      "apiURL": "https://your-waddleai-proxy.com/v1"
    },
    "claude-3-sonnet": {
      "apiKey": "wa-your-api-key-here",
      "apiURL": "https://your-waddleai-proxy.com/v1"
    }
  }
}
```

### GitHub Copilot (Enterprise)

GitHub Copilot can use WaddleAI as a proxy for enterprise deployments.

!!! note
    GitHub Copilot proxy configuration requires GitHub Enterprise and may have limitations.

**Configuration via HTTP Proxy:**

```bash
# Set environment variables before launching VS Code
export HTTPS_PROXY=https://your-waddleai-proxy.com
export COPILOT_PROXY=https://your-waddleai-proxy.com
code .
```

**Settings JSON:**

```json
{
  "http.proxy": "https://your-waddleai-proxy.com",
  "http.proxyStrictSSL": true,
  "github.copilot.advanced": {
    "debug.overrideProxyUrl": "https://your-waddleai-proxy.com"
  }
}
```

### Cody by Sourcegraph

Cody supports custom LLM endpoints.

**Configuration:**

```json
{
  "cody.serverEndpoint": "https://your-waddleai-proxy.com/v1",
  "cody.customHeaders": {
    "Authorization": "Bearer wa-your-api-key-here"
  }
}
```

## Team Configuration

### Centralized Config Distribution

For teams, distribute a standardized configuration:

**`.continue/config.json` (template):**

```json
{
  "models": [
    {
      "title": "GPT-4 (Primary)",
      "provider": "openai",
      "model": "gpt-4",
      "apiKey": "${WADDLEAI_API_KEY}",
      "apiBase": "https://company-waddleai.com/v1"
    }
  ],
  "systemMessage": "You are an expert developer at ACME Corp. Follow our coding standards..."
}
```

**Team Setup Script:**

```bash
#!/bin/bash
# setup-waddleai.sh

echo "Setting up WaddleAI integration..."

# Get API key from user
read -sp "Enter your WaddleAI API key: " API_KEY
echo

# Add to shell profile
echo "export WADDLEAI_API_KEY=$API_KEY" >> ~/.bashrc
echo "export WADDLEAI_API_KEY=$API_KEY" >> ~/.zshrc

# Create Continue config directory
mkdir -p ~/.continue

# Copy team config
cat > ~/.continue/config.json << 'EOF'
{
  "models": [
    {
      "title": "GPT-4",
      "provider": "openai",
      "model": "gpt-4",
      "apiKey": "${WADDLEAI_API_KEY}",
      "apiBase": "https://company-waddleai.com/v1"
    }
  ]
}
EOF

echo "WaddleAI integration configured!"
echo "Restart VS Code to apply changes."
```

### Per-Developer API Keys

Each developer should have their own API key:

```bash
# Each developer runs:
export WADDLEAI_API_KEY="wa-their-user-id-xyz"
```

This enables:
- Individual usage tracking
- Personal quota enforcement
- Audit trail per developer
- Granular access control

## Advanced Features

### Memory Integration

Enable conversation memory for persistent context:

**Continue Config:**

```json
{
  "models": [
    {
      "title": "GPT-4 with Memory",
      "provider": "openai",
      "model": "gpt-4",
      "apiKey": "wa-your-api-key-here",
      "apiBase": "https://your-waddleai-proxy.com/v1",
      "requestOptions": {
        "headers": {
          "X-WaddleAI-Memory": "developer-session-${USER}",
          "X-WaddleAI-Memory-Type": "conversation"
        }
      }
    }
  ]
}
```

### Smart Routing

Use the smart router to automatically select the best model:

```json
{
  "models": [
    {
      "title": "Smart Router",
      "provider": "openai",
      "model": "smart-router",
      "apiKey": "wa-your-api-key-here",
      "apiBase": "https://your-waddleai-proxy.com/v1"
    }
  ]
}
```

The router will analyze each request and route to:
- GPT-4 for complex reasoning
- Claude for creative writing
- Codestral for code completion
- Llama for simple queries

### Custom Context

Add custom context for your codebase:

```json
{
  "models": [...],
  "contextProviders": [
    {
      "name": "code",
      "params": {
        "includeGitDiff": true
      }
    },
    {
      "name": "terminal",
      "params": {}
    },
    {
      "name": "web",
      "params": {
        "allowedDomains": ["docs.company.com", "github.com/company"]
      }
    }
  ],
  "systemMessage": "You are helping develop internal tools at ACME Corp. Coding standards: https://docs.company.com/standards"
}
```

## Monitoring Usage

### Check Personal Usage

```bash
# Using curl
curl -H "Authorization: Bearer wa-your-api-key" \
  https://your-waddleai-proxy.com/api/usage

# Using Python
import requests

response = requests.get(
    "https://your-waddleai-proxy.com/api/usage",
    headers={"Authorization": "Bearer wa-your-api-key"}
)

usage = response.json()
print(f"Tokens used today: {usage['daily_usage']}")
print(f"Remaining quota: {usage['daily_remaining']}")
```

### Team Dashboard

Managers can view team usage via management portal:

```
https://your-waddleai-mgmt.com/analytics
```

View:
- Usage per developer
- Token consumption trends
- Cost breakdown by model
- Most active developers
- Quota utilization

## Security Best Practices

### API Key Storage

**DO:**
- Store API keys in environment variables
- Use secret management tools (1Password, HashiCorp Vault)
- Rotate keys regularly
- Use different keys per environment (dev/staging/prod)

**DON'T:**
- Commit API keys to Git
- Share keys between developers
- Store keys in plain text files
- Use the same key for multiple purposes

### Key Rotation Script

```bash
#!/bin/bash
# rotate-api-key.sh

OLD_KEY=$WADDLEAI_API_KEY
NEW_KEY=$(curl -X POST https://waddleai-mgmt.com/api/api_keys \
  -H "Authorization: Bearer $OLD_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "VS Code - Rotated", "expires_days": 90}' \
  | jq -r '.api_key')

echo "New API key: $NEW_KEY"
echo "Update your environment variable:"
echo "export WADDLEAI_API_KEY=$NEW_KEY"

# Disable old key after confirmation
read -p "Disable old key? (y/n) " -n 1 -r
if [[ $REPLY =~ ^[Yy]$ ]]; then
  curl -X DELETE https://waddleai-mgmt.com/api/api_keys/$OLD_KEY_ID \
    -H "Authorization: Bearer $NEW_KEY"
  echo "Old key disabled"
fi
```

## Troubleshooting

### Connection Issues

**Problem:** Cannot connect to WaddleAI proxy

**Solution:**
1. Check proxy URL is correct
2. Verify network connectivity
3. Check firewall rules
4. Test with curl:
   ```bash
   curl https://your-waddleai-proxy.com/healthz
   ```

### Authentication Errors

**Problem:** 401 Authentication Required

**Solution:**
1. Verify API key is correct
2. Check API key hasn't expired
3. Ensure API key starts with "wa-"
4. Test authentication:
   ```bash
   curl -H "Authorization: Bearer wa-your-key" \
     https://your-waddleai-proxy.com/api/usage
   ```

### Quota Exceeded

**Problem:** 429 Rate Limit or Quota Exceeded

**Solution:**
1. Check current usage:
   ```bash
   curl -H "Authorization: Bearer wa-your-key" \
     https://your-waddleai-proxy.com/api/quota
   ```
2. Contact admin to increase quota
3. Wait for quota reset (daily/monthly)

### SSL Certificate Errors

**Problem:** SSL verification failed

**Solution:**
1. Ensure proxy uses valid SSL certificate
2. Update CA certificates:
   ```bash
   sudo update-ca-certificates
   ```
3. For self-signed certs (dev only):
   ```json
   {
     "models": [{
       "requestOptions": {
         "verifySsl": false
       }
     }]
   }
   ```

### Model Not Available

**Problem:** Model not found or unavailable

**Solution:**
1. Check available models:
   ```bash
   curl -H "Authorization: Bearer wa-your-key" \
     https://your-waddleai-proxy.com/v1/models
   ```
2. Verify model name spelling
3. Check LLM provider is configured and enabled
4. Contact admin to add model

## Example Workflows

### Code Review Workflow

```json
{
  "models": [
    {
      "title": "Code Review",
      "provider": "openai",
      "model": "gpt-4",
      "apiKey": "${WADDLEAI_API_KEY}",
      "apiBase": "https://waddleai.com/v1",
      "systemMessage": "You are a senior code reviewer. Focus on: security vulnerabilities, performance issues, code clarity, test coverage. Reference our standards: https://docs.company.com/code-review-checklist"
    }
  ],
  "slashCommands": [
    {
      "name": "review",
      "description": "Comprehensive code review",
      "prompt": "Please review the selected code for: 1) Security issues 2) Performance concerns 3) Best practices 4) Test coverage. Provide specific, actionable feedback."
    }
  ]
}
```

### Documentation Generation

```json
{
  "slashCommands": [
    {
      "name": "docs",
      "description": "Generate documentation",
      "prompt": "Generate comprehensive documentation for the selected code including: 1) Purpose and functionality 2) Parameters and return values 3) Usage examples 4) Edge cases. Follow Google docstring format."
    }
  ]
}
```

### Bug Investigation

```json
{
  "slashCommands": [
    {
      "name": "debug",
      "description": "Investigate bug",
      "prompt": "Analyze the selected code and help debug the issue. Consider: 1) Logic errors 2) Edge cases 3) Race conditions 4) Type mismatches. Suggest fixes with explanations."
    }
  ]
}
```

## Performance Tips

1. **Use appropriate models**: Don't use GPT-4 for simple completions
2. **Enable caching**: Cache responses for repeated queries
3. **Optimize context**: Only include relevant code context
4. **Batch requests**: Group related queries when possible
5. **Monitor usage**: Track token consumption to optimize costs

## Team Best Practices

1. **Standardize configs**: Use shared team configuration
2. **Document conventions**: Create coding standards document
3. **Regular training**: Teach team effective prompt engineering
4. **Monitor patterns**: Track common queries to optimize routing
5. **Share learnings**: Create internal wiki of useful prompts

## Integration Checklist

- [ ] WaddleAI proxy deployed and accessible
- [ ] API keys generated for all developers
- [ ] VS Code extension installed (Continue/Cursor/Cody)
- [ ] Configuration file created and tested
- [ ] API keys stored securely (environment variables)
- [ ] Team documentation created
- [ ] Usage monitoring configured
- [ ] Quota limits set appropriately
- [ ] Support contact established
- [ ] Backup authentication method configured

## Additional Resources

- [Continue Documentation](https://continue.dev/docs)
- [Cursor Documentation](https://cursor.sh/docs)
- [WaddleAI Management API](../api/management-api.md)
- [Authentication Guide](../api/authentication.md)
- [Troubleshooting](../troubleshooting/common-issues.md)

## Support

For VS Code integration support:
- Check extension logs in VS Code Output panel
- Review WaddleAI proxy logs
- Test with curl to isolate issues
- Contact support@waddleai.com with:
  - Extension name and version
  - Configuration (redact API keys)
  - Error messages
  - Steps to reproduce