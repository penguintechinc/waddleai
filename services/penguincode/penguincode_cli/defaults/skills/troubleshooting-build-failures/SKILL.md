---
name: troubleshooting-build-failures
description: "Build debugging, dependency resolution, and compilation error fixing"
model: qwen2.5-coder:7b
---

# Troubleshooting Build Failures

## Overview
Systematically diagnose and fix build failures across different languages and build systems.

## Debugging Process
1. **Read the error message** — often the fix is in the error itself
2. **Identify the failing step** — compile, link, test, package?
3. **Check recent changes**: `git diff HEAD~1` — what changed?
4. **Reproduce locally** — ensure you can see the failure
5. **Fix and verify** — apply fix, confirm build passes

## Common Issues

### Dependency Resolution
```bash
# Python
pip install -r requirements.txt  # Missing deps
pip install --upgrade pip         # Outdated pip

# Node.js
rm -rf node_modules package-lock.json && npm install

# Go
go mod tidy
go mod download
```

### Compilation Errors
- **Type errors**: check function signatures and return types
- **Import errors**: verify module paths and installed packages
- **Syntax errors**: check language version compatibility

### Docker Build Failures
```bash
# Build with no cache to ensure clean state
docker build --no-cache -t app .

# Check specific stage
docker build --target builder -t app-builder .
```

### CI Failures
- Check if failure reproduces locally
- Review environment differences (OS, versions)
- Check for flaky tests: re-run the pipeline
- Verify secrets and environment variables are set

## Quick Fixes
- **"Module not found"**: install the missing dependency
- **"Permission denied"**: check file permissions, user context
- **"Port already in use"**: `lsof -i :<port>` to find the conflict
- **"Out of memory"**: increase Docker/CI memory limits
