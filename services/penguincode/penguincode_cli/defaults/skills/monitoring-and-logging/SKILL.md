---
name: monitoring-and-logging
description: "Observability setup, structured logging, and alerting"
model: qwen2.5-coder:7b
---

# Monitoring and Logging

## Overview
Set up observability with structured logging, metrics collection, and alerting.

## Structured Logging
```python
import logging
import json

# JSON structured logging
logging.basicConfig(format='%(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

def log_event(event: str, **kwargs):
    logger.info(json.dumps({"event": event, **kwargs}))

# Usage
log_event("user_login", user_id="123", ip="10.0.0.1")
```

## Log Levels
- **ERROR** — something failed, needs attention
- **WARNING** — unexpected but recoverable
- **INFO** — normal operations (requests, deployments)
- **DEBUG** — detailed diagnostic information

## Metrics
Key metrics to track:
- **Request rate** — requests per second
- **Error rate** — 4xx and 5xx responses
- **Latency** — p50, p95, p99 response times
- **Resource usage** — CPU, memory, disk, connections

## Health Checks
```python
@app.route("/health")
def health():
    return {"status": "healthy", "uptime": get_uptime()}

@app.route("/ready")
def ready():
    # Check database, cache, external dependencies
    return {"status": "ready", "db": check_db(), "cache": check_cache()}
```

## Alerting Rules
- Error rate > 1% for 5 minutes
- Latency p99 > 5 seconds
- Service unavailable (health check failing)
- Disk usage > 80%

## Best Practices
- Log at service boundaries (incoming/outgoing requests)
- Include correlation IDs for request tracing
- Don't log sensitive data (passwords, tokens, PII)
- Use structured (JSON) logging for machine parsing
