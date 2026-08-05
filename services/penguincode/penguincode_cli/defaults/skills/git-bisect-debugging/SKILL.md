---
name: git-bisect-debugging
description: "Using git bisect for systematic regression hunting"
model: qwen2.5-coder:7b
---

# Git Bisect Debugging

## Overview
Use git bisect to efficiently find the commit that introduced a bug through binary search.

## When to Use
- A feature that used to work is now broken
- You know a "good" commit and a "bad" commit
- Manual debugging hasn't identified the cause

## Manual Bisect
1. **Start**: `git bisect start`
2. **Mark bad**: `git bisect bad` (current commit is broken)
3. **Mark good**: `git bisect good <known-good-hash>`
4. **Test each checkout**: Git checks out a midpoint
   - If broken: `git bisect bad`
   - If working: `git bisect good`
5. **Repeat** until the culprit commit is found
6. **Finish**: `git bisect reset`

## Automated Bisect
```bash
git bisect start
git bisect bad HEAD
git bisect good <good-hash>
git bisect run <test-command>
```
Example: `git bisect run pytest tests/test_auth.py`

## Tips
- Write a small test script that exits 0 (good) or 1 (bad)
- Use `git bisect log` to review the bisect session
- Use `git bisect visualize` to see remaining commits
- Save the log: `git bisect log > bisect.log`
