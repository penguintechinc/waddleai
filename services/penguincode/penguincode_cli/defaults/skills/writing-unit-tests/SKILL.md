---
name: writing-unit-tests
description: "Unit test best practices, mocking, and coverage strategies"
model: qwen2.5-coder:7b
---

# Writing Unit Tests

## Overview
Write effective unit tests that verify individual components in isolation with proper mocking and coverage.

## Test Structure (Arrange-Act-Assert)
```python
def test_user_creation():
    # Arrange — set up test data
    user_data = {"name": "Test User", "email": "test@example.com"}

    # Act — call the function under test
    user = create_user(**user_data)

    # Assert — verify the result
    assert user.name == "Test User"
    assert user.email == "test@example.com"
```

## Naming Convention
- `test_<function>_<scenario>_<expected_result>`
- Example: `test_login_invalid_password_returns_401`

## Mocking Best Practices
- Mock external dependencies, not internal logic
- Use `unittest.mock.patch` or `pytest-mock`
- Mock at the boundary (e.g., API calls, database queries)
- Verify mock calls: `mock.assert_called_once_with(...)`

## Coverage Strategy
- Aim for 80%+ coverage on critical paths
- Focus on business logic, not boilerplate
- Test edge cases: empty inputs, None, boundary values
- Test error paths: exceptions, invalid states

## Running Tests
```bash
# With coverage
pytest --cov=src --cov-report=term-missing

# Specific file
pytest tests/test_auth.py -v

# Specific test
pytest tests/test_auth.py::test_login_success -v
```
