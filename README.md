# WaddleAI

```
██╗    ██╗ █████╗ ██████╗ ██████╗ ██╗     ███████╗ █████╗ ██╗
██║    ██║██╔══██╗██╔══██╗██╔══██╗██║     ██╔════╝██╔══██╗██║
██║ █╗ ██║███████║██║  ██║██║  ██║██║     █████╗  ███████║██║
██║███╗██║██╔══██║██║  ██║██║  ██║██║     ██╔══╝  ██╔══██║██║
╚███╔███╔╝██║  ██║██████╔╝██████╔╝███████╗███████╗██║  ██║██║
 ╚══╝╚══╝ ╚═╝  ╚═╝╚═════╝ ╚═════╝ ╚══════╝╚══════╝╚═╝  ╚═╝╚═╝
```

Enterprise-grade AI proxy and management system with OpenAI-compatible APIs, advanced routing, security, and token management.

![WaddleAI Architecture](docs/assets/architecture-diagram.svg)

## Features

✨ **OpenAI-Compatible API** - Drop-in replacement for OpenAI API  
🔀 **Multi-LLM Support** - Route to OpenAI, Anthropic, Ollama, and more  
👥 **Role-Based Access Control** - Admin, Resource Manager, Reporter, User roles  
🪙 **Dual Token System** - WaddleAI tokens for billing, LLM tokens for analytics  
🛡️ **Security Scanning** - Prompt injection and jailbreak detection  
📊 **Token Management** - Quota enforcement and usage tracking  
🏢 **Multi-Tenant** - Organization-based isolation  
📈 **Monitoring** - Prometheus metrics and health checks  
🧠 **Memory Integration** - Conversation memory with mem0/ChromaDB

## Quick Start

### Using Docker Compose (Recommended)

```bash
# Clone repository
git clone https://github.com/your-org/waddleai.git
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
- **Website**: http://localhost:3000 (Marketing site)

### First Steps

1. **Get Admin API Key**: Check logs for the admin API key created on first startup
   ```bash
   docker-compose logs waddleai-mgmt | grep "Admin API Key"
   ```

2. **Test the API**:
   ```bash
   curl http://localhost:8000/v1/models \
     -H "Authorization: Bearer wa-your-api-key-here"
   ```

3. **Access Management Portal**: Visit http://localhost:8001 and login with `admin/admin123`

## Usage

### VS Code Integration

Use WaddleAI directly in VS Code Copilot Chat with our official extension:

1. **Install Extension**: Search for "WaddleAI" in VS Code Extensions
2. **Configure**: Set your API key with `Ctrl+Shift+P` → "WaddleAI: Set API Key"
3. **Use in Chat**: Open Copilot Chat (`Ctrl+Shift+I`) and select WaddleAI as provider

**Features:**
- 🚀 Access to all WaddleAI models in Copilot Chat
- 🧠 Conversation memory for context-aware coding assistance
- 🔒 Built-in security scanning for prompt protection
- 📊 Token usage tracking and quota monitoring

See [vscode-extension/waddleai-copilot/README.md](vscode-extension/waddleai-copilot/README.md) for detailed setup instructions.

### OpenAI-Compatible API

```python
import openai

client = openai.OpenAI(
    api_key="wa-your-api-key-here",
    base_url="http://localhost:8000/v1"
)

response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

### Management API

```python
import requests

# Login
auth = requests.post("http://localhost:8001/auth/login", json={
    "username": "admin", "password": "admin123"
})
token = auth.json()["access_token"]

# Get usage statistics
usage = requests.get(
    "http://localhost:8001/analytics/usage",
    headers={"Authorization": f"Bearer {token}"}
).json()
```

## Architecture

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

## Roles & Permissions

| Role | Permissions |
|------|-------------|
| **Admin** | Full system access, configuration, all organizations |
| **Resource Manager** | Token quota management for assigned organizations |
| **Reporter** | Usage analytics and reporting for assigned organizations |
| **User** | OpenAI-compatible API access, personal usage tracking |

## Token System

WaddleAI uses a dual token system:

- **WaddleAI Tokens**: Normalized billing units across all providers
- **LLM Tokens**: Raw provider tokens for detailed analytics

Example response:
```json
{
  "usage": {
    "prompt_tokens": 100,        // Raw LLM tokens
    "completion_tokens": 50,     // Raw LLM tokens  
    "total_tokens": 150,         // Total LLM tokens
    "waddleai_tokens": 15        // Normalized billing tokens
  }
}
```

## Configuration

### Environment Variables

```bash
# Database
DATABASE_URL=postgresql://user:pass@localhost/waddleai

# Security
JWT_SECRET=your-jwt-secret
SECURITY_POLICY=balanced  # strict, balanced, permissive

# External APIs
OPENAI_API_KEY=sk-your-openai-key
ANTHROPIC_API_KEY=your-anthropic-key
OLLAMA_URL=http://localhost:11434
```

### LLM Providers

Configure through the management interface or API:

```bash
curl -X POST http://localhost:8001/config/links \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "OpenAI GPT-4",
    "provider": "openai", 
    "endpoint_url": "https://api.openai.com/v1",
    "api_key": "sk-your-openai-key",
    "model_list": ["gpt-4", "gpt-3.5-turbo"],
    "enabled": true
  }'
```

## Security

WaddleAI includes comprehensive security features:

- **Prompt Injection Detection**: Pattern-based and ML-based detection
- **Jailbreak Prevention**: Roleplay and instruction override detection  
- **Data Extraction Blocking**: System prompt and credential protection
- **Rate Limiting**: Per-user and per-API-key limits
- **Audit Logging**: Comprehensive request and security event logging

## Deployment

### Production Deployment

See [deployment documentation](docs/deployment/) for:

- **Kubernetes**: Helm charts and manifests
- **Docker Swarm**: Production stack files  
- **Bare Metal**: systemd service files
- **Cloud**: AWS, GCP, Azure deployment guides

### Monitoring

WaddleAI exposes Prometheus metrics:

```bash
# Proxy metrics
curl http://localhost:8000/metrics

# Management metrics  
curl http://localhost:8001/metrics
```

Example Grafana dashboards included in `deployment/monitoring/`.

## Documentation

- **[Installation Guide](docs/getting-started/installation.md)** - Setup instructions
- **[Claude Integration](docs/CLAUDE.md)** - Integration guide for applications
- **[API Reference](docs/api/)** - Complete API documentation
- **[Administration](docs/administration/)** - User and system management
- **[Troubleshooting](docs/troubleshooting/)** - Common issues and solutions

Full documentation available at: http://localhost:8080 (when running with Docker Compose)

## Development

### Setup Development Environment

```bash
# Create virtual environment
python3.13 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Initialize database
cd shared/database && python models.py

# Start proxy server
cd proxy/apps/proxy_server && python main.py

# Start management server (separate terminal)
cd management/apps/management_server && python main.py
```

### Running Tests

```bash
# Unit tests
pytest tests/unit/

# Integration tests  
pytest tests/integration/

# Load tests
pytest tests/load/
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## Support

- **Documentation**: [docs.waddleai.com](https://docs.waddleai.com)
- **Issues**: [GitHub Issues](https://github.com/your-org/waddleai/issues)
- **Discord**: [Community Discord](https://discord.gg/waddleai)
- **Email**: support@waddleai.com

## License

**License Highlights:**
- **Personal & Internal Use**: Free under AGPL-3.0
- **Commercial Use**: Requires commercial license
- **SaaS Deployment**: Requires commercial license if providing as a service

### Contributor Employer Exception (GPL-2.0 Grant)

Companies employing official contributors receive GPL-2.0 access to community features:

- **Perpetual for Contributed Versions**: GPL-2.0 rights to versions where the employee contributed remain valid permanently, even after the employee leaves the company
- **Attribution Required**: Employee must be credited in CONTRIBUTORS, AUTHORS, commit history, or release notes
- **Future Versions**: New versions released after employment ends require standard licensing
- **Community Only**: Enterprise features still require a commercial license

This exception rewards contributors by providing lasting fair use rights to their employers.

See [LICENSE](docs/LICENSE.%20md) for full details.

## Acknowledgments

- OpenAI for the API specification
- Anthropic for Claude API inspiration
- Ollama community for local LLM support
- py4web and PyDAL for the web framework

---

**Ready to get started?** Check out the [installation guide](docs/getting-started/installation.md) or try the [quick start](#quick-start) above!