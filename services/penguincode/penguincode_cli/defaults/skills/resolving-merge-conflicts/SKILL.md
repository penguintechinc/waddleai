---
name: resolving-merge-conflicts
description: "Systematic merge conflict resolution workflow"
model: qwen2.5-coder:7b
---

# Resolving Merge Conflicts

## Overview
Guide systematic resolution of git merge conflicts while preserving both sides' intent.

## Conflict Detection
1. **Identify conflicts**: `git status` shows conflicted files
2. **Understand the conflict**: read both versions before editing
3. **Check context**: `git log --merge` to see diverging commits

## Resolution Process
1. Open conflicted file
2. Find conflict markers: `<<<<<<<`, `=======`, `>>>>>>>`
3. Understand BOTH sides:
   - Above `=======` — your changes (HEAD)
   - Below `=======` — their changes (incoming)
4. Decide: keep one, keep both, or write a new resolution
5. Remove ALL conflict markers
6. Test the resolved code
7. Stage: `git add <resolved-file>`
8. Continue: `git rebase --continue` or `git merge --continue`

## Common Patterns
- **Both added to same location**: combine additions in logical order
- **Both modified same line**: understand intent, pick or merge
- **One deleted, one modified**: check if modification is still needed
- **Package lock conflicts**: regenerate with `npm install` or equivalent

## Verification
- Run tests after resolving
- Review the full diff: `git diff`
- Ensure no conflict markers remain: `grep -rn "<<<<<<" .`
