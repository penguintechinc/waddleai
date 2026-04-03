---
name: integration-testing
description: "Cross-service testing patterns for microservice architectures"
model: qwen2.5-coder:7b
---

# Integration Testing

## Overview
Test interactions between services, databases, and external dependencies to verify the system works as a whole.

## Test Categories
1. **Service-to-service**: API calls between microservices
2. **Database integration**: CRUD operations with real database
3. **Message queue**: Pub/sub message flow verification
4. **External API**: Third-party service integration (use mocks in CI)

## Setup Pattern
```python
# Use docker-compose for test dependencies
# tests/conftest.py
import pytest

@pytest.fixture(scope="session")
def docker_services():
    """Start required services for integration tests."""
    # docker-compose -f docker-compose.test.yml up -d
    yield
    # docker-compose -f docker-compose.test.yml down
```

## Best Practices
- Use a separate test database (never test against production)
- Clean up test data after each test
- Use fixtures for common setup/teardown
- Mock external services that you don't control
- Run integration tests in CI but allow skipping locally
- Keep tests independent — no shared state between tests

## Running
```bash
make test-integration
# Or manually:
pytest tests/integration/ -v --tb=short
```
