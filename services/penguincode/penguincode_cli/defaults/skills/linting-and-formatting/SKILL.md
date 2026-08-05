---
name: linting-and-formatting
description: "Language-specific linters, auto-formatting, and code style enforcement"
model: qwen2.5-coder:7b
---

# Linting and Formatting

## Overview
Run language-specific linters and formatters to enforce consistent code style.

## Python
```bash
# Linting
flake8 src/ --max-line-length=120
ruff check src/

# Formatting
black src/
isort src/

# Type checking
mypy src/
```

## JavaScript/TypeScript
```bash
# Linting
eslint src/ --ext .js,.ts,.tsx

# Formatting
prettier --write "src/**/*.{js,ts,tsx,css,json}"
```

## Go
```bash
# Formatting (auto-applied)
gofmt -w .
goimports -w .

# Linting
golangci-lint run ./...
```

## Pre-Commit Integration
Run linters before every commit:
```bash
make lint
```

## Auto-Fix
Most linters support auto-fix mode:
- `ruff check --fix`
- `eslint --fix`
- `black` (always auto-formats)
- `prettier --write`

## CI Integration
- Run linters in CI as a required check
- Block merges on lint failures
- Use the same lint configuration locally and in CI
