---
name: onboarding-new-project
description: "Project setup, codebase understanding, and development environment configuration"
---

# Onboarding a New Project

## Overview
Guide through understanding a new codebase, setting up the development environment, and making a first contribution.

## Step 1: Understand the Project
1. Read `README.md` for project overview
2. Read `CLAUDE.md` for AI-specific context and rules
3. Check `docs/` for architecture and development guides
4. Review `docs/APP_STANDARDS.md` for app-specific standards
5. Look at recent commits: `git log --oneline -20`

## Step 2: Explore the Codebase
1. Understand directory structure: `ls -la`
2. Identify language/framework: check config files
3. Review entry points: `main.py`, `main.go`, `index.ts`
4. Understand service architecture: check `docker-compose.yml`
5. Review test structure: `ls tests/`

## Step 3: Set Up Development Environment
```bash
make setup              # Install dependencies
cp .env.example .env    # Configure environment
make dev                # Start development services
make test               # Verify tests pass
```

## Step 4: Make a First Contribution
1. Pick a small issue or improvement
2. Create a feature branch
3. Make the change
4. Run tests and linting
5. Submit a PR

## Key Files to Read
- `CLAUDE.md` — project rules and context
- `Makefile` — available commands
- `docker-compose.yml` — service architecture
- `config.yaml` — application configuration
- `.github/workflows/` — CI/CD pipelines
