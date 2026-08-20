# Installation Guide

This guide walks you through installing and setting up WaddleAI in various environments.

## System Requirements

- Python 3.13+
- PostgreSQL 13+ (recommended) or SQLite for development
- Redis (optional, for caching and rate limiting)
- 4GB+ RAM for production deployment
- Linux/macOS/Windows

## Quick Start (Development)

### 1. Clone the Repository

```bash
git clone https://github.com/your-org/waddleai.git
cd waddleai
```

### 2. Set up Python Environment

```bash
# Create virtual environment
python3.13 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Initialize Database

```bash
# Set database URL (SQLite for development)
export DATABASE_URL=sqlite://waddleai.db

# Initialize database and create admin user
cd shared/database
python models.py
```

This will create an admin user and display the API key. **Save this API key!**

### 4. Configure Environment

Create `.env` file in the project root:

```bash
# Database
DATABASE_URL=sqlite://waddleai.db

# Security
JWT_SECRET=your-jwt-secret-key-change-in-production
SECURITY_POLICY=balanced

# Servers
PROXY_HOST=0.0.0.0
PROXY_PORT=8000
MGMT_HOST=0.0.0.0
MGMT_PORT=8001

# External Services (optional)
OPENAI_API_KEY=sk-your-openai-key
ANTHROPIC_API_KEY=your-anthropic-key
OLLAMA_URL=http://localhost:11434
```

### 5. Start the Servers

```bash
# Terminal 1: Start Proxy Server
cd proxy/apps/proxy_server
python main.py

# Terminal 2: Start Management Server
cd management/apps/management_server
python main.py
```

### 6. Verify Installation

Set your virtual key first:

```bash
export WADDLEAI_API_KEY="wa-..."   # from the WaddleAI WebUI: Virtual Keys
```

```bash
# Check proxy server health
curl http://localhost:8000/healthz

# Check management server health
curl http://localhost:8001/healthz

# Test OpenAI-compatible API
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer $WADDLEAI_API_KEY"
```

## Production Installation

### Docker Compose (Recommended)

1. Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  waddleai-proxy:
    build:
      context: .
      dockerfile: proxy/Dockerfile
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql://waddleai:password@postgres:5432/waddleai
      - MANAGEMENT_SERVER_URL=http://waddleai-mgmt:8001
      - JWT_SECRET=${JWT_SECRET}
      - SECURITY_POLICY=balanced
    depends_on:
      postgres:
        condition: service_healthy
      waddleai-mgmt:
        condition: service_started
    restart: unless-stopped

  waddleai-mgmt:
    build:
      context: .
      dockerfile: management/Dockerfile
    ports:
      - "8001:8001"
    environment:
      - DATABASE_URL=postgresql://waddleai:password@postgres:5432/waddleai
      - JWT_SECRET=${JWT_SECRET}
    depends_on:
      postgres:
        condition: service_healthy
    restart: unless-stopped

  postgres:
    image: postgres:15
    environment:
      - POSTGRES_DB=waddleai
      - POSTGRES_USER=waddleai
      - POSTGRES_PASSWORD=password
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./scripts/init.sql:/docker-entrypoint-initdb.d/init.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U waddleai"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    restart: unless-stopped

volumes:
  postgres_data:
```

2. Create environment file:

```bash
# .env
JWT_SECRET=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 16)
```

3. Start the stack:

```bash
docker-compose up -d
```

### Kubernetes Deployment

1. Apply the manifests:

```bash
kubectl apply -f deployment/kubernetes/
```

2. Configure ingress and TLS:

```yaml
# ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: waddleai-ingress
spec:
  tls:
  - hosts:
    - api.waddleai.com
    - mgmt.waddleai.com
    secretName: waddleai-tls
  rules:
  - host: api.waddleai.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: waddleai-proxy
            port:
              number: 8000
  - host: mgmt.waddleai.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: waddleai-mgmt
            port:
              number: 8001
```

## Configuration

### Database Setup

#### PostgreSQL (Recommended)

```bash
# Install PostgreSQL
sudo apt-get install postgresql postgresql-contrib

# Create database and user
sudo -u postgres psql
CREATE DATABASE waddleai;
CREATE USER waddleai WITH PASSWORD 'secure_password';
GRANT ALL PRIVILEGES ON DATABASE waddleai TO waddleai;
\q

# Set connection URL
export DATABASE_URL="postgresql://waddleai:secure_password@localhost:5432/waddleai"
```

#### SQLite (Development Only)

```bash
export DATABASE_URL="sqlite://waddleai.db"
```

### LLM Provider Configuration

#### OpenAI

```bash
export OPENAI_API_KEY="sk-your-openai-api-key"
```

Then add connection link via management interface or API.

#### Anthropic

```bash
export ANTHROPIC_API_KEY="your-anthropic-api-key"
```

#### Ollama (Local)

```bash
# Install Ollama
curl -fsSL https://ollama.ai/install.sh | sh

# Pull models
ollama pull llama2
ollama pull mistral

# Configure WaddleAI connection
export OLLAMA_URL="http://localhost:11434"
```

### Security Configuration

#### JWT Secrets

```bash
# Generate secure JWT secret
export JWT_SECRET=$(openssl rand -hex 32)
```

#### Security Policies

Available policies: `strict`, `balanced`, `permissive`

```bash
export SECURITY_POLICY=balanced
```

#### TLS Configuration

For production, configure TLS termination at load balancer or use Caddy:

```bash
# Caddyfile
api.waddleai.com {
    reverse_proxy localhost:8000
}

mgmt.waddleai.com {
    reverse_proxy localhost:8001
}
```

## Post-Installation Setup

### 1. Create Organizations

```bash
curl -X POST http://localhost:8001/orgs \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Default Organization",
    "description": "Default organization for users",
    "token_quota_monthly": 1000000,
    "token_quota_daily": 100000
  }'
```

### 2. Create Users

```bash
curl -X POST http://localhost:8001/users \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "user1",
    "email": "user1@example.com",
    "password": "secure_password",
    "role": "user",
    "organization_id": 1
  }'
```

### 3. Configure LLM Connections

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

### 4. Set up Monitoring

```bash
# Add Prometheus scraping config
- job_name: 'waddleai'
  static_configs:
  - targets: ['localhost:8000', 'localhost:8001']
  metrics_path: /metrics
```

## Verification

### Health Checks

```bash
# Proxy server
curl http://localhost:8000/healthz
curl http://localhost:8000/api/status

# Management server
curl http://localhost:8001/healthz
curl http://localhost:8001/api/health
```

### API Tests

```bash
# List models
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer $API_KEY"

# Chat completion
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{
    "model": "gpt-3.5-turbo",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Management Interface

Visit `http://localhost:8001` and log in with admin credentials.

## Troubleshooting

### Common Issues

1. **Database Connection Failed**
   ```bash
   # Check database is running
   pg_isready -h localhost -p 5432

   # Verify credentials
   psql $DATABASE_URL -c "SELECT 1;"
   ```

2. **Permission Denied**
   ```bash
   # Check file permissions
   chmod +x proxy/apps/proxy_server/main.py
   chmod +x management/apps/management_server/main.py
   ```

3. **Port Already in Use**
   ```bash
   # Find process using port
   lsof -i :8000

   # Kill process or use different port
   export PROXY_PORT=8080
   ```

### Logs

```bash
# Check application logs
tail -f proxy/logs/proxy.log
tail -f management/logs/management.log

# Check system logs
journalctl -u waddleai-proxy
journalctl -u waddleai-mgmt
```

## Next Steps

- [Configure LLM providers](../integrations/)
- [Set up user management](../administration/user-management.md)
- [Configure security policies](../administration/security-policies.md)
- [Set up monitoring](../administration/monitoring.md)

---

Need help? Check the [troubleshooting guide](../troubleshooting/common-issues.md) or review the logs for error details.
