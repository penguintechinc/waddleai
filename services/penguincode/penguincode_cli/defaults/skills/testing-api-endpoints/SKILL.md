---
name: testing-api-endpoints
description: "API contract testing, status codes, and response validation"
model: qwen2.5-coder:7b
---

# Testing API Endpoints

## Overview
Verify API endpoints return correct responses, status codes, and handle errors properly.

## Test Categories
1. **Happy path** — valid requests return expected responses
2. **Error handling** — invalid input returns proper error codes
3. **Authentication** — protected routes require valid tokens
4. **Pagination** — list endpoints handle pagination correctly

## HTTP Status Code Testing
- `200 OK` — successful GET/PUT/PATCH
- `201 Created` — successful POST that creates a resource
- `204 No Content` — successful DELETE
- `400 Bad Request` — invalid input
- `401 Unauthorized` — missing or invalid auth
- `403 Forbidden` — valid auth but insufficient permissions
- `404 Not Found` — resource doesn't exist
- `422 Unprocessable Entity` — validation errors
- `500 Internal Server Error` — should never happen in tests

## Test Pattern (Python/pytest)
```python
def test_create_user(client):
    response = client.post("/api/v1/users", json={
        "name": "Test User",
        "email": "test@example.com",
    })
    assert response.status_code == 201
    data = response.get_json()
    assert data["name"] == "Test User"
    assert "id" in data

def test_create_user_missing_email(client):
    response = client.post("/api/v1/users", json={"name": "Test"})
    assert response.status_code == 422
```

## Best Practices
- Test all documented status codes
- Verify response body structure, not just status
- Test with and without authentication
- Test rate limiting if applicable
