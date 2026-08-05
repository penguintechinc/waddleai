---
name: cherry-picking
description: "Cherry-pick workflow with conflict handling and verification"
model: qwen2.5-coder:7b
---

# Cherry-Picking Commits

## Overview
Guide applying specific commits from one branch to another using git cherry-pick.

## When to Cherry-Pick
- Backporting a fix to a release branch
- Applying a specific commit without merging the whole branch
- Moving a commit that was made on the wrong branch

## Workflow
1. **Identify the commit**: `git log --oneline <source-branch>`
2. **Switch to target branch**: `git checkout <target-branch>`
3. **Cherry-pick**: `git cherry-pick <commit-hash>`
4. **Resolve conflicts** if any (see waddlepowers:resolving-merge-conflicts)
5. **Verify**: run tests and review changes
6. **Push**: `git push`

## Multiple Commits
```bash
git cherry-pick <hash1> <hash2> <hash3>
# Or a range:
git cherry-pick <start-hash>..<end-hash>
```

## Handling Issues
- **Abort**: `git cherry-pick --abort` to cancel
- **Skip**: `git cherry-pick --skip` to skip current commit
- **Continue**: `git cherry-pick --continue` after resolving conflicts

## Best Practices
- Cherry-pick individual commits, not ranges when possible
- Always verify tests pass after cherry-picking
- Note the original commit in the cherry-pick message
