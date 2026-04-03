---
name: committing-changes
description: "Pre-commit checks, security scanning, and conventional commit message formatting"
model: qwen2.5-coder:7b
---

# Committing Changes

## Overview
Guide the user through a complete pre-commit workflow including security scanning, linting, and conventional commit formatting.

## Pre-Commit Checklist
1. **Run linters** — ensure code passes all configured linters (see waddlepowers:linting-and-formatting)
2. **Security scan** — check for hardcoded secrets and vulnerabilities (see waddlepowers:security-scanning)
3. **Run tests** — verify all tests pass before committing (see waddlepowers:verification-before-completion)
4. **Review changes** — use `git diff --staged` to review what will be committed

## Conventional Commit Format
Use the format: `<type>(<scope>): <description>`

**Types:**
- `feat` — new feature
- `fix` — bug fix
- `docs` — documentation only
- `style` — formatting, missing semicolons, etc.
- `refactor` — code restructuring without behavior change
- `perf` — performance improvement
- `test` — adding or fixing tests
- `chore` — build process, tooling, dependencies
- `ci` — CI/CD pipeline changes
- `revert` — reverting a previous commit

## Workflow
1. Stage files: `git add <specific-files>` (avoid `git add .`)
2. Review staged: `git diff --staged`
3. Check for secrets: scan for API keys, passwords, tokens
4. Run lint: `make lint` or language-specific linter
5. Run tests: `make test` or `pytest`
6. Commit: `git commit -m "<type>(<scope>): <description>"`
7. Verify: `git log --oneline -1` to confirm

## Important Rules
- NEVER use `git add .` or `git add -A` — stage specific files
- NEVER commit .env, credentials, or secret files
- ALWAYS run security scan before commit
- Keep commits atomic — one logical change per commit
