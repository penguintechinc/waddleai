# WaddleAI Network Architecture

## Overview

WaddleAI uses a **secure Docker bridge network** architecture where all services communicate internally, with **minimal public exposure**.

## Network Topology

```
┌────────────────────────────────────────────────────────────────┐
│  Public Internet                                                │
└────────────────┬───────────────┬──────────────┬────────────────┘
                 │               │              │
        ┌────────▼────┐  ┌───────▼──────┐  ┌───▼────────┐
        │ :8000       │  │ :8002        │  │ :8081      │
        │ WaddleAI    │  │ Management   │  │ Docs       │
        │ Proxy       │  │ Portal       │  │ (Optional) │
        └────────┬────┘  └───────┬──────┘  └────────────┘
                 │               │
                 └───────┬───────┘
                         │
        ┌────────────────▼────────────────────────────────┐
        │  waddleai-dev Bridge Network (PRIVATE)          │
        │                                                  │
        │  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
        │  │ postgres │  │  redis   │  │  chromadb    │  │
        │  │  :5432   │  │  :6379   │  │   :8000      │  │
        │  │ INTERNAL │  │ INTERNAL │  │  INTERNAL    │  │
        │  └──────────┘  └──────────┘  └──────────────┘  │
        │                                                  │
        │  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
        │  │ ollama   │  │prometheus│  │   grafana    │  │
        │  │ :11434   │  │  :9090   │  │   :3000      │  │
        │  │ INTERNAL │  │ INTERNAL │  │ PUBLIC: 3333 │  │
        │  └──────────┘  └──────────┘  └──────────────┘  │
        └──────────────────────────────────────────────────┘
```

## Port Exposure

### ✅ Publicly Exposed (REQUIRED)

| Port | Service | Purpose | Auth Required |
|------|---------|---------|---------------|
| `8000` | WaddleAI Proxy | API endpoints (OpenAI, Claude, MCP) | ✅ API Key |
| `8002` | Management Portal | Admin web interface | ✅ JWT Token |

### 🔓 Publicly Exposed (OPTIONAL)

| Port | Service | Purpose | Production |
|------|---------|---------|------------|
| `8081` | Documentation | MkDocs site | Remove in production |
| `3333` | Grafana | Metrics visualization | Optional, has auth |

### 🔒 Internal Only (NO PUBLIC ACCESS)

| Service | Internal Port | Purpose |
|---------|---------------|---------|
| `postgres` | `5432` | Database |
| `redis` | `6379` | Cache + routing config |
| `chromadb` | `8000` | mem0 conversation storage |
| `ollama` | `11434` | Local LLM backend |
| `prometheus` | `9090` | Metrics collection |

## Security Benefits

### 1. **Attack Surface Minimization**
- Database, cache, and storage are **never** exposed publicly
- Only API gateway and admin portal are accessible
- Reduces risk of direct attacks on infrastructure

### 2. **Network Isolation**
- All services on private `waddleai-dev` bridge network
- Services communicate using **service names** (e.g., `postgres:5432`)
- Host firewall doesn't need to allow internal traffic

### 3. **Service-to-Service Communication**

Services communicate internally using Docker DNS:

```python
# In proxy/management code
DATABASE_URL = "postgresql://waddleai:password@postgres:5432/waddleai"
# ↑ Uses service name "postgres", resolved by Docker DNS

REDIS_URL = "redis://:password@redis:6379/0"
# ↑ Uses service name "redis", no public exposure needed

CHROMADB_URL = "http://chromadb:8000"
# ↑ Uses service name "chromadb", completely internal
```

### 4. **Defense in Depth**

Multiple layers of security:

```
Public Request
     ↓
[Firewall Rules] ← Only allows :8000, :8002
     ↓
[Docker Bridge Network] ← Internal routing
     ↓
[API Key Authentication] ← WaddleAI validates
     ↓
[RBAC Authorization] ← Role-based access
     ↓
[Database Credentials] ← PostgreSQL auth
     ↓
[Data Access]
```

## Configuration Examples

### Development (Current)

```yaml
# docker-compose.env.yml
postgres:
  expose:
    - "5432"  # Internal only
  networks:
    - waddleai-dev

waddleai-proxy:
  ports:
    - "8000:8000"  # Public API
  environment:
    - DATABASE_URL=postgresql://waddleai:pass@postgres:5432/waddleai
  networks:
    - waddleai-dev
```

**Result**: Proxy can access `postgres:5432` internally, but public can't directly connect to PostgreSQL.

### Production Hardening

For production, you can further lock down:

```yaml
networks:
  waddleai-dev:
    driver: bridge
    internal: false  # Set to true to block ALL external access
    ipam:
      config:
        - subnet: 172.25.0.0/16
          gateway: 172.25.0.1

  # Separate network for public-facing services
  waddleai-public:
    driver: bridge

waddleai-proxy:
  networks:
    - waddleai-public  # Can accept public requests
    - waddleai-dev     # Can talk to internal services

postgres:
  networks:
    - waddleai-dev     # ONLY internal network
```

## Firewall Rules

### Docker Host Firewall

```bash
# Allow only necessary public ports
ufw allow 8000/tcp comment "WaddleAI Proxy API"
ufw allow 8002/tcp comment "WaddleAI Management Portal"
ufw allow 22/tcp comment "SSH"

# Deny all other incoming
ufw default deny incoming
ufw default allow outgoing
ufw enable
```

### iptables (Alternative)

```bash
# Accept established connections
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Allow SSH
iptables -A INPUT -p tcp --dport 22 -j ACCEPT

# Allow WaddleAI services
iptables -A INPUT -p tcp --dport 8000 -j ACCEPT
iptables -A INPUT -p tcp --dport 8002 -j ACCEPT

# Drop everything else
iptables -P INPUT DROP
```

### Cloudflare Tunnel (Zero Trust)

For maximum security, don't expose ports at all:

```yaml
# Use Cloudflare Tunnel to expose services
# No public ports needed on host!

services:
  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel run
    environment:
      - TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}
    networks:
      - waddleai-dev

  waddleai-proxy:
    # NO PUBLIC PORTS!
    expose:
      - "8000"
    networks:
      - waddleai-dev
```

Then configure Cloudflare Tunnel to route:
- `api.waddleai.com` → `http://waddleai-proxy:8000`
- `admin.waddleai.com` → `http://waddleai-mgmt:8001`

## Network Commands

### Inspect Network

```bash
# List all networks
docker network ls

# Inspect waddleai-dev network
docker network inspect waddleai-development

# See which containers are on the network
docker network inspect waddleai-development | jq '.[0].Containers'
```

### Test Internal Connectivity

```bash
# From inside proxy container
docker exec waddleai-dev-proxy curl http://postgres:5432
# ✅ Works - same network

# From host
curl http://localhost:5432
# ❌ Fails - not exposed publicly

# But proxy API works
curl http://localhost:8000/healthz
# ✅ Works - explicitly exposed
```

### Monitor Network Traffic

```bash
# See traffic between containers
docker exec waddleai-dev-proxy tcpdump -i eth0 'port 5432'

# Check DNS resolution
docker exec waddleai-dev-proxy nslookup postgres
# Should resolve to internal IP like 172.25.0.2
```

## Security Best Practices

### ✅ DO

1. **Use `expose` for internal services**, not `ports`
2. **Keep database/cache credentials in secrets**, not in compose file
3. **Use strong passwords** for all services
4. **Enable TLS** between services in production
5. **Regularly update** Docker images
6. **Monitor logs** for unauthorized access attempts

### ❌ DON'T

1. **Don't expose PostgreSQL/Redis ports** publicly
2. **Don't use default passwords** in production
3. **Don't disable authentication** on any service
4. **Don't use `network_mode: host`** unless XDP is required
5. **Don't commit secrets** to git

## Troubleshooting

### Service Can't Connect to Database

**Problem**: `connection refused` errors

**Solution**:
1. Check both services are on same network: `docker inspect <container>`
2. Verify service names in connection strings (use `postgres`, not `localhost`)
3. Check health status: `docker-compose ps`

### Can't Access Service from Host

**Problem**: `curl http://localhost:5432` doesn't work

**Solution**: This is **expected**! Internal services use `expose`, not `ports`. Access via proxy API instead.

### Network Performance Issues

**Problem**: Slow inter-service communication

**Solution**:
```yaml
networks:
  waddleai-dev:
    driver: bridge
    driver_opts:
      com.docker.network.driver.mtu: 1500  # Adjust if needed
```

## Summary

✅ **Current Architecture**:
- Private bridge network (`waddleai-dev`)
- Only 2-3 ports exposed publicly (8000, 8002, optional 8081/3333)
- All internal services communicate securely within network
- No direct public access to database, cache, or storage

This provides **defense in depth** while maintaining ease of development! 🔒🦆