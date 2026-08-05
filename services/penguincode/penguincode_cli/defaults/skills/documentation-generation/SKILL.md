---
name: documentation-generation
description: "Docstrings, API documentation, and README generation"
---

# Documentation Generation

## Overview
Generate and maintain documentation including docstrings, API docs, and README files.

## Docstring Standards

### Python (Google style)
```python
def create_user(name: str, email: str) -> User:
    """Create a new user account.

    Args:
        name: Full name of the user.
        email: Email address (must be unique).

    Returns:
        The created User object.

    Raises:
        ValueError: If email is already registered.
    """
```

### Go (godoc)
```go
// CreateUser creates a new user account with the given name and email.
// It returns an error if the email is already registered.
func CreateUser(name, email string) (*User, error) {
```

## API Documentation
- Use OpenAPI/Swagger for REST APIs
- Document all endpoints, request/response schemas
- Include example requests and responses
- Keep docs in sync with implementation

## README Structure
1. Project name and description
2. Quick start / installation
3. Usage examples
4. Configuration
5. API reference (or link to docs)
6. Contributing guidelines
7. License

## Best Practices
- Write docs as you code, not after
- Keep docstrings close to the code
- Use consistent formatting across the project
- Include build status badges in README
