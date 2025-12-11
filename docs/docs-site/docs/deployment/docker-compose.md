# Docker Compose Deployment

Deploy WaddleAI using Docker Compose for development and production.

## Quick Start

```bash
# Clone repository
git clone https://github.com/yourusername/WaddleAI.git
cd WaddleAI

# Copy environment file
cp .env.example .env.dev

# Edit configuration
nano .env.dev

# Start services
docker-compose -f docker-compose.env.yml up -d

# View logs
docker-compose -f docker-compose.env.yml logs -f
```

## Environment Configuration

### .env.dev

```bash
# Database
DATABASE_URL=postgresql://waddleai:waddleai_pass@postgres:5432/waddleai
POSTGRES_USER=waddleai
POSTGRES_PASSWORD=waddleai_pass
POSTGRES_DB=waddleai

# Redis
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_PASSWORD=redis_pass

# Security
JWT_SECRET=your_jwt_secret_here_min_32_chars

# Servers
PROXY_PORT=8000
MANAGEMENT_PORT=8001

# Routing
ROUTING_LLM_PROVIDER=ollama
ROUTING_LLM_MODEL=llama3.2:1b
ROUTING_LLM_ENDPOINT=http://ollama:11434

# Memory
ENABLE_MEMORY=true
CHROMADB_HOST=chromadb
CHROMADB_PORT=8000

# MCP
MCP_PORT=8765
MCP_ENABLE_WEBSOCKET=true
```

## Docker Compose File

### Complete Configuration

```yaml
version: '3.8'

services:
  # WaddleAI Proxy Server
  proxy:
    build:
      context: ./proxy
      dockerfile: Dockerfile
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - REDIS_HOST=${REDIS_HOST}
      - REDIS_PORT=${REDIS_PORT}
      - REDIS_PASSWORD=${REDIS_PASSWORD}
      - JWT_SECRET=${JWT_SECRET}
      - ROUTING_LLM_ENDPOINT=${ROUTING_LLM_ENDPOINT}
      - ENABLE_MEMORY=${ENABLE_MEMORY}
      - CHROMADB_HOST=${CHROMADB_HOST}
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      chromadb:
        condition: service_started
    volumes:
      - ./proxy:/app
      - proxy_logs:/var/log/waddleai
    restart: unless-stopped
    networks:
      - waddleai

  # Management Portal
  management:
    build:
      context: ./management
      dockerfile: Dockerfile
    ports:
      - "8001:8001"
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - REDIS_HOST=${REDIS_HOST}
      - JWT_SECRET=${JWT_SECRET}
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    volumes:
      - ./management:/app
    restart: unless-stopped
    networks:
      - waddleai

  # PostgreSQL Database
  postgres:
    image: postgres:16
    environment:
      - POSTGRES_USER=${POSTGRES_USER}
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
      - POSTGRES_DB=${POSTGRES_DB}
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped
    networks:
      - waddleai

  # Redis Cache
  redis:
    image: redis:7
    command: redis-server --requirepass ${REDIS_PASSWORD}
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "--raw", "incr", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5
    restart: unless-stopped
    networks:
      - waddleai

  # ChromaDB for Memory
  chromadb:
    image: chromadb/chroma:latest
    ports:
      - "8000:8000"
    volumes:
      - chromadb_data:/chroma/chroma
    environment:
      - CHROMA_SERVER_AUTH_CREDENTIALS_PROVIDER=chromadb.auth.token_authn.TokenAuthenticationServerProvider
      - CHROMA_SERVER_AUTH_TOKEN_TRANSPORT_HEADER=X-Chroma-Token
    restart: unless-stopped
    networks:
      - waddleai

  # Ollama for Local LLMs
  ollama:
    image: ollama/ollama:latest
    ports:
      - "11434:11434"
    volumes:
      - ollama_data:/root/.ollama
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    restart: unless-stopped
    networks:
      - waddleai

  # Prometheus Monitoring
  prometheus:
    image: prom/prometheus:latest
    ports:
      - "9090:9090"
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
    restart: unless-stopped
    networks:
      - waddleai

  # Grafana Dashboards
  grafana:
    image: grafana/grafana:latest
    ports:
      - "3333:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASSWORD:-admin}
      - GF_USERS_ALLOW_SIGN_UP=false
    volumes:
      - grafana_data:/var/lib/grafana
      - ./monitoring/grafana:/etc/grafana/provisioning
    depends_on:
      - prometheus
    restart: unless-stopped
    networks:
      - waddleai

volumes:
  postgres_data:
  redis_data:
  chromadb_data:
  ollama_data:
  prometheus_data:
  grafana_data:
  proxy_logs:

networks:
  waddleai:
    driver: bridge
```

## Service Details

### Proxy Server

**Purpose**: Main API server for LLM requests

**Ports**: 8000

**Dependencies**: PostgreSQL, Redis, ChromaDB, Ollama

**Health Check**:
```bash
curl http://localhost:8000/healthz
```

### Management Portal

**Purpose**: Web UI for configuration

**Ports**: 8001

**Dependencies**: PostgreSQL, Redis

**Access**: http://localhost:8001

### PostgreSQL

**Purpose**: Primary database

**Ports**: 5432

**Data**: Persistent in `postgres_data` volume

**Backup**:
```bash
docker-compose exec postgres pg_dump -U waddleai waddleai > backup.sql
```

### Redis

**Purpose**: Cache and routing instructions

**Ports**: 6379

**Data**: Persistent in `redis_data` volume

**CLI Access**:
```bash
docker-compose exec redis redis-cli -a $REDIS_PASSWORD
```

### ChromaDB

**Purpose**: Vector database for memory

**Ports**: 8000

**Data**: Persistent in `chromadb_data` volume

### Ollama

**Purpose**: Local LLM inference

**Ports**: 11434

**Pull Models**:
```bash
docker-compose exec ollama ollama pull llama3.2:1b
docker-compose exec ollama ollama pull codellama
```

## Common Operations

### Start Services

```bash
# Start all
docker-compose -f docker-compose.env.yml up -d

# Start specific service
docker-compose -f docker-compose.env.yml up -d proxy
```

### Stop Services

```bash
# Stop all
docker-compose -f docker-compose.env.yml down

# Stop and remove volumes
docker-compose -f docker-compose.env.yml down -v
```

### View Logs

```bash
# All services
docker-compose -f docker-compose.env.yml logs -f

# Specific service
docker-compose -f docker-compose.env.yml logs -f proxy

# Last 100 lines
docker-compose -f docker-compose.env.yml logs --tail=100 proxy
```

### Restart Services

```bash
# Restart all
docker-compose -f docker-compose.env.yml restart

# Restart specific
docker-compose -f docker-compose.env.yml restart proxy
```

### Update Images

```bash
# Pull latest images
docker-compose -f docker-compose.env.yml pull

# Rebuild and restart
docker-compose -f docker-compose.env.yml up -d --build
```

### Execute Commands

```bash
# Run command in container
docker-compose -f docker-compose.env.yml exec proxy python -m shared.database.init_db

# Shell access
docker-compose -f docker-compose.env.yml exec proxy bash
```

## Initialization

### First-Time Setup

```bash
# Start services
docker-compose -f docker-compose.env.yml up -d

# Wait for services to be healthy
docker-compose -f docker-compose.env.yml ps

# Initialize database
docker-compose -f docker-compose.env.yml exec proxy python -m shared.database.init_db

# Create admin user
docker-compose -f docker-compose.env.yml exec proxy python -m management.create_admin

# Pull Ollama models
docker-compose -f docker-compose.env.yml exec ollama ollama pull llama3.2:1b
```

## Monitoring

### Health Checks

```bash
# All services
docker-compose -f docker-compose.env.yml ps

# Proxy health
curl http://localhost:8000/healthz

# Management health
curl http://localhost:8001/healthz
```

### Resource Usage

```bash
# CPU and memory
docker stats

# Disk usage
docker system df
```

### Access Monitoring

- **Prometheus**: http://localhost:9090
- **Grafana**: http://localhost:3333 (admin/admin)

## Backup and Restore

### Backup

```bash
# Database
docker-compose exec postgres pg_dump -U waddleai waddleai > backup.sql

# Redis
docker-compose exec redis redis-cli -a $REDIS_PASSWORD --rdb /data/dump.rdb

# ChromaDB
docker cp $(docker-compose ps -q chromadb):/chroma/chroma ./chromadb_backup/
```

### Restore

```bash
# Database
cat backup.sql | docker-compose exec -T postgres psql -U waddleai waddleai

# Redis
docker cp dump.rdb $(docker-compose ps -q redis):/data/dump.rdb
docker-compose restart redis

# ChromaDB
docker cp ./chromadb_backup/. $(docker-compose ps -q chromadb):/chroma/chroma/
docker-compose restart chromadb
```

## Production Configuration

### Environment Variables

```bash
# Strong passwords
JWT_SECRET=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -base64 24)
REDIS_PASSWORD=$(openssl rand -base64 24)
GRAFANA_PASSWORD=$(openssl rand -base64 24)

# Production URLs
PROXY_HOST=0.0.0.0
MANAGEMENT_HOST=127.0.0.1  # Internal only

# Security
ENABLE_PROMPT_INJECTION_DETECTION=true
RATE_LIMIT_PER_MINUTE=1000

# Performance
WORKERS=8
DATABASE_POOL_SIZE=50
REDIS_POOL_SIZE=100
```

### SSL/TLS

Add reverse proxy (nginx):

```yaml
services:
  nginx:
    image: nginx:latest
    ports:
      - "443:443"
      - "80:80"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf
      - ./nginx/ssl:/etc/nginx/ssl
    depends_on:
      - proxy
      - management
    networks:
      - waddleai
```

### Resource Limits

```yaml
services:
  proxy:
    deploy:
      resources:
        limits:
          cpus: '4'
          memory: 4G
        reservations:
          cpus: '2'
          memory: 2G
```

## Troubleshooting

### Service won't start

```bash
# Check logs
docker-compose -f docker-compose.env.yml logs service_name

# Check network
docker network ls
docker network inspect waddleai_waddleai

# Recreate service
docker-compose -f docker-compose.env.yml up -d --force-recreate service_name
```

### Database connection error

```bash
# Check database is healthy
docker-compose -f docker-compose.env.yml ps postgres

# Test connection
docker-compose -f docker-compose.env.yml exec postgres psql -U waddleai -d waddleai

# Reset database
docker-compose -f docker-compose.env.yml down -v
docker-compose -f docker-compose.env.yml up -d
```

### Out of disk space

```bash
# Clean up
docker system prune -a

# Remove unused volumes
docker volume prune
```

## Next Steps

- [Kubernetes Deployment](kubernetes.md)
- [Production Checklist](production-checklist.md)
- [Monitoring Guide](../administration/monitoring.md)