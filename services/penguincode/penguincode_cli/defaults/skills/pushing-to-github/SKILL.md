---
name: pushing-to-github
description: "Push workflow with branch verification, PR creation using gh CLI"
model: qwen2.5-coder:7b
---

# Pushing to GitHub

## Overview
Guide safe pushing to remote repositories and creating pull requests.

## Prerequisites
- All commits pass pre-commit checks (see waddlepowers:committing-changes)
- Code has been reviewed (see waddlepowers:code-review)
- Branch is up to date with target branch

## Push Workflow
1. **Verify branch**: `git branch --show-current`
2. **Check remote status**: `git fetch origin && git status`
3. **Rebase if needed**: `git rebase origin/<target-branch>`
4. **Push**: `git push -u origin <branch-name>`

## Creating Pull Requests
Use the `gh` CLI for PR creation:
```bash
gh pr create --title "<title>" --body "<description>"
```

### PR Description Template
- Summary of changes (1-3 bullet points)
- Test plan with verification steps
- Link to related issues

## Safety Rules
- NEVER force push to main/master
- NEVER push directly to protected branches
- ALWAYS create a PR for code changes
- Verify CI checks pass after pushing
