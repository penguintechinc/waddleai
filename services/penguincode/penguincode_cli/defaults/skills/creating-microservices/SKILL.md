---
name: creating-microservices
description: "Service scaffolding, Dockerfile, Docker Compose, and CI template generation"
model: qwen2.5-coder:7b
---

# Creating Microservices

## Overview
Scaffold new microservices with proper structure, containerization, and CI integration.

## Service Structure
```
services/<service-name>/
├── Dockerfile
├── Makefile
├── README.md
├── src/
│   ├── main.py (or main.go)
│   ├── config.py
│   ├── routes/
│   └── models/
├── tests/
│   ├── conftest.py
│   └── test_*.py
└── requirements.txt (or go.mod)
```

## Scaffold Steps
1. Create service directory: `mkdir -p services/<name>/src`
2. Initialize with entry point and config
3. Add Dockerfile (see waddlepowers:building-docker-images)
4. Add to docker-compose.yml
5. Design API endpoints (see waddlepowers:api-design)
6. Set up CI pipeline
7. Add to Kubernetes manifests (see waddlepowers:deploying-to-kubernetes)

## Dockerfile Template
```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
USER nobody
EXPOSE 8080
CMD ["python", "src/main.py"]
```

## Docker Compose Addition
```yaml
services:
  new-service:
    build: ./services/new-service
    ports:
      - "8082:8080"
    environment:
      - DATABASE_URL=${DATABASE_URL}
    depends_on:
      - database
```

## Best Practices
- Each service owns its own data
- Communication via API or message queue
- Independent deployability
- Shared nothing architecture
