---
name: branching-strategy
description: "Branch naming conventions, feature/hotfix/release flows"
---

# Branching Strategy

## Overview
Guide consistent branch naming and workflow for feature development, hotfixes, and releases.

## Branch Naming Convention
- `feature/<description>` — new features
- `fix/<description>` — bug fixes
- `hotfix/<description>` — urgent production fixes
- `release/<version>` — release preparation
- `docs/<description>` — documentation updates
- `refactor/<description>` — code restructuring
- `test/<description>` — test additions/fixes

## Feature Development Flow
1. Create branch from main: `git checkout -b feature/<name> main`
2. Develop and commit incrementally
3. Keep branch updated: `git rebase main`
4. Create PR when ready
5. Merge after review and CI passes

## Hotfix Flow
1. Create from main: `git checkout -b hotfix/<name> main`
2. Apply minimal fix
3. Test thoroughly
4. Create PR with "hotfix" label
5. Merge and tag release

## Best Practices
- Keep branches short-lived (< 1 week)
- Rebase over merge for cleaner history
- Delete branches after merging
- Use descriptive branch names (e.g., `feature/add-user-auth`)
