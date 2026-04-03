# GitHub Workflows & Version Management

## Overview

Penguin Code uses GitHub Actions for automated building, testing, and releasing. The version management system follows semantic versioning with epoch64 build timestamps.

## Version Format

**Format**: `vMajor.Minor.Patch.epoch64`

Example: `v0.1.0.1735329600`

- **Major**: Breaking changes, API changes, removed features
- **Minor**: New features and functionality
- **Patch**: Bug fixes, security patches, minor updates
- **Build**: Epoch64 timestamp of build time

## Workflows

### 1. CI Workflow (`ci.yml`)

**Trigger**: Pull requests and pushes to main

**Jobs**:
- **Lint & Test Extension**: ESLint, TypeScript compilation
- **Test CLI**: Multi-OS (Ubuntu, macOS, Windows) × Python (3.12, 3.13)
  - Ruff linting
  - MyPy type checking
  - Pytest with coverage

**Status Badges**:
```markdown
![CI](https://github.com/penguintechinc/penguin-code/workflows/CI/badge.svg)
```

### 2. Build Pre-release Workflow (`build-prerelease.yml`)

**Trigger**: Push to main branch with changes to:
- `vsix-extension/**`
- `penguincode/**`
- `.version`
- `pyproject.toml`

**Jobs**:
- **Build Extension**: Package VSIX with beta tag
- **Test CLI**: Run tests on Python 3.12 and 3.13

**Output**:
- Pre-release with tag: `v0.1.0.1735329600-beta`
- VSIX artifact: `penguin-code-v0.1.0.1735329600-beta.vsix`

**When**: Automatically on every merge to main

### 3. Release Workflow (`release.yml`)

**Trigger**: Git tag matching `v*.*.*`

**Jobs**:
- **Create Release**: Generate release notes, update .version
- **Build Extension**: Package and upload VSIX

**Output**:
- Full release with tag: `v0.1.0`
- VSIX artifact: `penguin-code-v0.1.0.vsix`
- Release notes with changelog

**When**: Manual tag creation for official releases

## Version Management Commands

### Update Version Script

Located at: `scripts/version/update-version.sh`

**Usage**:
```bash
# Increment build timestamp only (default)
./scripts/version/update-version.sh

# Increment patch version (0.1.0 → 0.1.1)
./scripts/version/update-version.sh patch

# Increment minor version (0.1.0 → 0.2.0)
./scripts/version/update-version.sh minor

# Increment major version (0.1.0 → 1.0.0)
./scripts/version/update-version.sh major
```

**What it does**:
1. Reads current version from `.version`
2. Increments specified component
3. Updates build timestamp to current epoch
4. Writes new version to `.version`

## Release Process

### Beta Pre-release (Automatic)

**Triggered on every main branch push**:

1. Make changes and commit
2. Push to main branch
3. Workflow automatically builds and creates beta pre-release
4. Download VSIX from GitHub Releases

```bash
git add .
git commit -m "Add new feature"
git push origin main
# Wait for workflow to complete
# Pre-release created: v0.1.0.1735329600-beta
```

### Official Release (Manual)

**For stable releases**:

1. Update version (optional):
   ```bash
   ./scripts/version/update-version.sh minor
   ```

2. Commit version change:
   ```bash
   git add .version
   git commit -m "Bump version to $(cat .version)"
   git push origin main
   ```

3. Create and push tag:
   ```bash
   VERSION=$(cat .version | cut -d. -f1-3)
   git tag $VERSION
   git push origin $VERSION
   ```

4. Workflow runs and creates official release

### Quick Release Command

```bash
# Release current version
VERSION=$(cat .version | cut -d. -f1-3) && \
git tag $VERSION && \
git push origin $VERSION
```

## Build Tags

| Tag Format | Type | Trigger | Example |
|------------|------|---------|---------|
| `vX.X.X.epoch-beta` | Beta Pre-release | Push to main | `v0.1.0.1735329600-beta` |
| `vX.X.X` | Official Release | Git tag | `v0.1.0` |

## Workflow Files

```
.github/workflows/
├── ci.yml                    # Continuous integration
├── build-prerelease.yml      # Beta builds on main
└── release.yml               # Official releases on tags

scripts/version/
└── update-version.sh         # Version management script

.version                      # Version tracking file
```

## GitHub Secrets

**Required**:
- `GITHUB_TOKEN` - Automatically provided by GitHub Actions

**Optional** (for marketplace publishing):
- `VSCE_PAT` - VS Code Marketplace Personal Access Token

## Status Badges

Add to README.md:

```markdown
![CI](https://github.com/penguintechinc/penguin-code/workflows/CI/badge.svg)
![Release](https://github.com/penguintechinc/penguin-code/workflows/Release/badge.svg)
![Version](https://img.shields.io/github/v/release/penguintechinc/penguin-code?include_prereleases)
```

## Troubleshooting

### Workflow Not Triggering

**Pre-release not building**:
- Check if changes are in trigger paths
- Verify `.version` file exists
- Check workflow file syntax

**Release not building**:
- Ensure tag format is `vX.X.X`
- Verify tag was pushed: `git push --tags`

### Build Failures

**Extension build fails**:
- Check Node.js version in workflow (should be 20.x)
- Verify `package-lock.json` is committed
- Check ESLint errors

**CLI test fails**:
- Check Python version (3.12+ required)
- Verify dependencies in `pyproject.toml`
- Check test file imports

### Version Issues

**Version file not found**:
```bash
echo "v0.1.0.$(date +%s)" > .version
```

**Version script not executable**:
```bash
chmod +x scripts/version/update-version.sh
```

---

**Last Updated**: 2025-12-27
**Version System**: Semantic Versioning with Epoch64 Build Timestamps
