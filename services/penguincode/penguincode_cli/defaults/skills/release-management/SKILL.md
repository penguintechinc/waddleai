---
name: release-management
description: "Version bumping, changelog generation, and release tagging"
model: qwen2.5-coder:7b
---

# Release Management

## Overview
Manage version bumps, changelog generation, and release tagging following semver.

## Version Format
`vMajor.Minor.Patch.build` where build is epoch64 timestamp.

## Release Workflow
1. **Pre-release checks** (see waddlepowers:committing-changes, waddlepowers:smoke-testing)
2. **Bump version**: `./scripts/version/update-version.sh <level>`
3. **Update changelog**: prepend release notes to RELEASE_NOTES.md
4. **Commit**: `git commit -m "chore: release vX.X.X"`
5. **Tag**: `git tag -a vX.X.X -m "Release vX.X.X"`
6. **Push**: `git push && git push --tags`
7. **Verify CI** (see waddlepowers:github-actions-workflows)

## Version Bump Levels
```bash
./scripts/version/update-version.sh patch    # Bug fixes
./scripts/version/update-version.sh minor    # New features
./scripts/version/update-version.sh major    # Breaking changes
```

## Changelog Format
```markdown
## vX.X.X (YYYY-MM-DD)

### Added
- New feature description

### Changed
- Modified behavior

### Fixed
- Bug fix description
```

## Build Tags
- `beta-<epoch64>` — builds from main
- `alpha-<epoch64>` — builds from feature branches
- `vX.X.X-beta` — version release candidates
- `vX.X.X` — production releases
