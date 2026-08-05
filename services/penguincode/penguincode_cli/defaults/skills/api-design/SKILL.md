---
name: api-design
description: "REST and gRPC API design, versioning, and documentation"
---

# API Design

## Overview
Design consistent, well-documented APIs following REST conventions and best practices.

## REST API Conventions
- Use nouns for resources: `/api/v1/users` not `/api/v1/getUsers`
- Use HTTP methods correctly: GET (read), POST (create), PUT (replace), PATCH (update), DELETE (remove)
- Use plural nouns: `/users` not `/user`
- Nest for relationships: `/users/{id}/posts`

## Versioning
- URL path versioning: `/api/v1/users` (recommended)
- Always version your API from the start
- Maintain backward compatibility within a version

## Response Format
```json
{
  "data": { "id": 1, "name": "Example" },
  "meta": { "total": 100, "page": 1 }
}
```

### Error Response
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Email is required",
    "details": [{"field": "email", "reason": "required"}]
  }
}
```

## Status Codes
- `200` — Success
- `201` — Created
- `204` — No Content (successful delete)
- `400` — Bad Request (client error)
- `401` — Unauthorized
- `403` — Forbidden
- `404` — Not Found
- `409` — Conflict
- `422` — Validation Error
- `500` — Internal Server Error

## Pagination
```
GET /api/v1/users?page=2&per_page=20
```

## Best Practices
- Document all endpoints (OpenAPI/Swagger)
- Use consistent naming across all endpoints
- Include request/response examples
- Rate limit public endpoints
- Use HATEOAS links for discoverability
