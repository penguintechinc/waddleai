---
name: docker-compose-development
description: "Local development environment setup with Docker Compose"
model: qwen2.5-coder:7b
---

# Docker Compose Development

## Overview
Set up and manage local development environments using Docker Compose with hot-reload and debugging support.

## Development vs Production
- `docker-compose.dev.yml` — development (volumes, hot-reload, debug ports)
- `docker-compose.yml` — production (optimized, no debug)

## Common Commands
```bash
# Start development
docker-compose -f docker-compose.dev.yml up -d

# View logs
docker-compose logs -f <service>

# Rebuild a single service
docker-compose -f docker-compose.dev.yml up -d --build <service>

# Stop all
docker-compose down

# Stop and remove volumes
docker-compose down -v
```

## Development Features
- **Volume mounts** for live code reloading
- **Debug ports** exposed for IDE attachment
- **Environment overrides** via `.env` file
- **Health checks** for dependency ordering

## Seed Data
```bash
# Populate with test data (3-4 items per feature)
make seed-mock-data
```

## Troubleshooting
- **Port conflicts**: `lsof -i :<port>` to find conflicts
- **Volume permissions**: check UID/GID mapping
- **Network issues**: `docker network ls` and `docker network inspect`
- **Rebuild from scratch**: `docker-compose down -v && docker-compose up -d --build`
