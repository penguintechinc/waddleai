# Installation Guide

This guide walks you through installing WaddleAI on your system.

## Prerequisites

### Required

- **Python 3.13+**: WaddleAI requires Python 3.13 or later
- **PostgreSQL 14+**: Database for user data, API keys, and usage logs
- **Redis 6+**: Cache for routing instructions and session data
- **Docker & Docker Compose**: For containerized deployment (recommended)

### Optional

- **ChromaDB**: For memory integration (mem0)
- **Ollama**: For local LLM providers
- **Linux Kernel 5.10+**: For XDP acceleration (production only)

## Quick Install (Docker Compose)

The fastest way to get started is using Docker Compose:

```bash
# Clone the repository
git clone https://github.com/yourusername/WaddleAI.git
cd WaddleAI

# Copy environment template
cp .env.example .env.dev

# Edit environment variables
nano .env.dev

# Start all services
docker-compose -f docker-compose.env.yml up -d

# Check logs
docker-compose -f docker-compose.env.yml logs -f
```

**Services Started**:
- WaddleAI Proxy (port 8000)
- Management Portal (port 8001)
- PostgreSQL (port 5432)
- Redis (port 6379)
- ChromaDB (port 8000)
- Prometheus (port 9090)
- Grafana (port 3333)

## Manual Installation

### 1. Install System Dependencies

#### Ubuntu/Debian

```bash
sudo apt update
sudo apt install -y python3.13 python3.13-venv python3-pip \
  postgresql postgresql-contrib redis-server \
  build-essential libpq-dev
```

#### macOS

```bash
brew install python@3.13 postgresql redis
brew services start postgresql
brew services start redis
```

### 2. Clone Repository

```bash
git clone https://github.com/yourusername/WaddleAI.git
cd WaddleAI
```

### 3. Create Virtual Environment

```bash
python3.13 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 4. Install Python Dependencies

```bash
# Install core dependencies
pip install -r requirements.txt

# Install optional dependencies
pip install -r requirements-optional.txt  # For mem0, ChromaDB, etc.
```

### 5. Configure Database

```bash
# Create PostgreSQL database
sudo -u postgres psql
CREATE DATABASE waddleai;
CREATE USER waddleai_user WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE waddleai TO waddleai_user;
\q
```

### 6. Configure Environment

Create `.env` file:

```bash
# Database Configuration
DATABASE_URL=postgresql://waddleai_user:your_secure_password@localhost:5432/waddleai

# Redis Configuration
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_DB=0

# JWT Secret (generate with: openssl rand -hex 32)
JWT_SECRET=your_jwt_secret_here

# Server Configuration
PROXY_PORT=8000
MANAGEMENT_PORT=8001
ENABLE_XDP=false  # Set to true for production with XDP support

# Routing LLM Configuration
ROUTING_LLM_PROVIDER=ollama
ROUTING_LLM_MODEL=llama3.2:1b
ROUTING_LLM_ENDPOINT=http://localhost:11434

# Default Routing Instructions
ROUTING_INSTRUCTIONS=Route programming queries to codellama. Route analysis to GPT-4. Route simple queries to ollama.

# Memory Configuration (Optional)
ENABLE_MEMORY=true
CHROMADB_HOST=localhost
CHROMADB_PORT=8000

# MCP Configuration
MCP_PORT=8765
MCP_ENABLE_WEBSOCKET=true

# Security
ENABLE_PROMPT_INJECTION_DETECTION=true
MAX_DAILY_TOKENS_DEFAULT=100000
MAX_MONTHLY_TOKENS_DEFAULT=3000000
```

### 7. Initialize Database

```bash
# Run database migrations
python -m shared.database.init_db
```

### 8. Create Admin User

```bash
python -m management.create_admin
```

You'll be prompted to enter:
- Username
- Email
- Password
- Organization name

### 9. Start Services

#### Start Proxy Server

```bash
cd proxy
python -m apps.proxy_server.main
```

#### Start Management Portal (in new terminal)

```bash
cd management
python -m apps.management_server.main
```

## Verify Installation

### 1. Check Proxy Server

```bash
curl http://localhost:8000/healthz
```

Expected response:
```json
{
  "status": "healthy",
  "version": "1.0.0",
  "services": {
    "database": "connected",
    "redis": "connected",
    "memory": "connected"
  }
}
```

### 2. Access Management Portal

Open browser to `http://localhost:8001`

Login with admin credentials created earlier.

### 3. Test API Endpoint

```bash
# Generate API key from management portal first
export WADDLEAI_API_KEY="wai_your_api_key_here"

# Test OpenAI-compatible endpoint
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $WADDLEAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-3.5-turbo",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## Optional Components

### Install Ollama (Local LLM)

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull models
ollama pull llama3.2:1b  # Routing LLM
ollama pull llama3.2:3b  # General purpose
ollama pull codellama    # Code generation
```

### Install ChromaDB (Memory)

```bash
# Using Docker
docker run -d -p 8000:8000 chromadb/chroma

# Or install locally
pip install chromadb
chroma run --path ./chroma_data
```

### Enable XDP Acceleration (Linux only)

**Prerequisites**: Linux kernel 5.10+, root access

```bash
# Install BPF compiler and tools
sudo apt install -y clang llvm libelf-dev libbpf-dev bpftool

# Compile XDP program
cd shared/networking
make xdp

# Enable XDP in .env
ENABLE_XDP=true

# Run proxy with CAP_NET_ADMIN capability
sudo setcap cap_net_admin=eip $(which python3.13)
```

## Troubleshooting

### Database Connection Failed

```bash
# Check PostgreSQL is running
sudo systemctl status postgresql

# Check connection
psql -U waddleai_user -d waddleai -h localhost
```

### Redis Connection Failed

```bash
# Check Redis is running
redis-cli ping

# Should return: PONG
```

### Port Already in Use

```bash
# Find process using port
lsof -i :8000

# Kill process or change port in .env
PROXY_PORT=8080
```

### Permission Denied (XDP)

```bash
# XDP requires elevated privileges
sudo python -m apps.proxy_server.main

# Or add capability
sudo setcap cap_net_admin,cap_net_raw=eip $(which python3.13)
```

## Upgrade

### Upgrade Python Dependencies

```bash
source venv/bin/activate
pip install --upgrade -r requirements.txt
```

### Upgrade Database Schema

```bash
python -m shared.database.migrate
```

### Upgrade Docker Deployment

```bash
docker-compose -f docker-compose.env.yml pull
docker-compose -f docker-compose.env.yml up -d
```

## Next Steps

- [Quick Start Guide](quick-start.md)
- [Configuration Reference](configuration.md)
- [Connect Claude Code](../integrations/claude-code.md)
- [Connect Cursor IDE](../integrations/cursor-ide.md)