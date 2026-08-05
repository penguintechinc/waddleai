# WaddleAI CI/CD Workflows

This document describes all GitHub Actions workflows for WaddleAI and the services they manage.

## WaddleAI Services Overview

WaddleAI is an AI proxy system with the following core services:

| Service | Language | Purpose | Location |
|---------|----------|---------|----------|
| **Proxy** | Python 3.13 | AI request routing, rate limiting, caching | `/proxy` |
| **Management** | Python 3.13 | Admin dashboard, user management, monitoring | `/management` |

### Service Architecture

```
┌─────────────────────────────────────────────────┐
│           Client Applications                   │
└──────────────────────┬──────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────┐
│      Proxy Service (Port 8000)                  │
│  - OpenAI-compatible API endpoints              │
│  - Request routing and load balancing           │
│  - Rate limiting and caching                    │
│  - Authentication enforcement                   │
└──────────────────────┬──────────────────────────┘
                       │
        ┌──────────────┴──────────────┐
        │                             │
┌───────▼──────────┐      ┌──────────▼────────────┐
│  Management UI   │      │  Backend Services    │
│  (Port 8001)     │      │  - Database          │
│                  │      │  - Redis Cache       │
│  - Dashboard     │      │  - External APIs     │
│  - Settings      │      │                      │
│  - User Mgmt     │      │                      │
└──────────────────┘      └──────────────────────┘
```

## Workflow Files

### 1. **docker-build.yml**

**Trigger Events:**
- Push to `main`, `v1.x` branches
- Push of version tags (`v*`)
- Pull requests to `main`
- Path-based: changes to `proxy/**`, `management/**`, `.version`, or workflow file itself

**Jobs:**

#### test
- **Purpose**: Unit tests and security scanning
- **Matrix**: Python 3.13
- **Steps**:
  1. Checkout with full history
  2. Generate epoch64 timestamp
  3. Detect version file changes
  4. Set up Python 3.13
  5. Cache pip dependencies
  6. Install dependencies (includes bandit)
  7. Run pytest with coverage reporting
  8. Run bandit security analysis
  9. Upload coverage to Codecov

**Security Features**:
- Bandit security scanning for all Python code
- Coverage reporting for test quality metrics
- Codecov integration for continuous monitoring

#### build-and-push
- **Purpose**: Build multi-architecture Docker images and push to registry
- **Needs**: `test` job
- **Matrix**: `proxy`, `management` services
- **Platforms**: linux/amd64, linux/arm64
- **Steps**:
  1. Checkout with full history
  2. Generate epoch64 timestamp
  3. Detect version file changes
  4. Set up QEMU for multi-arch builds
  5. Set up Docker Buildx
  6. Login to GHCR if not a PR
  7. Extract metadata with conditional tagging
  8. Build and push Docker images
  9. Run Trivy vulnerability scan
  10. Upload scan results to GitHub Security tab

**Image Tagging Logic**:

| Scenario | Main Branch | Other Branches |
|----------|------------|-----------------|
| Regular build (no `.version` change) | `beta-<epoch64>` | `alpha-<epoch64>` |
| Version release (`.version` changed) | `vX.X.X-beta` | `vX.X.X-alpha` |
| Tagged release | `vX.X.X` + `latest` | N/A |

**Artifact Handling**:
- Container images automatically tagged based on branch and version status
- GitHub Artifacts: Trivy vulnerability scan results (SARIF format)

#### security-scan
- **Purpose**: Run Trivy vulnerability scanner on built images
- **Needs**: `build-and-push` job
- **Matrix**: `proxy`, `management` services
- **Conditions**: Skipped on PRs
- **Steps**:
  1. Run Trivy vulnerability scanner on image
  2. Upload results to GitHub Security tab

#### integration-test
- **Purpose**: Test services in containerized environment
- **Needs**: `build-and-push` job
- **Conditions**: Skipped on PRs
- **Services**:
  - PostgreSQL 15-Alpine (test database)
- **Steps**:
  1. Checkout code
  2. Create docker-compose override for testing
  3. Start services with docker-compose
  4. Test Proxy health endpoints
  5. Test Management health endpoints
  6. Test OpenAI-compatible API endpoint (401 expected without auth)
  7. Cleanup on exit

#### release
- **Purpose**: Create GitHub release on version tag
- **Needs**: `test`, `build-and-push`, `security-scan`, `integration-test`
- **Conditions**: Only on tags matching `refs/tags/v*`
- **Steps**:
  1. Checkout with full history
  2. Generate changelog from git log
  3. Create GitHub release with changelog

#### cleanup
- **Purpose**: Remove untagged images from registry
- **Needs**: `test`, `build-and-push`, `security-scan`, `integration-test`
- **Conditions**: Always runs (even on failures)
- **Steps**:
  1. Delete untagged images from GHCR to save storage

---

### 2. **version-release.yml**

**Trigger Events:**
- Push to `main` branch
- Path-based: changes to `.version` file only

**Purpose**: Automatically create GitHub pre-releases when `.version` file is updated

**Jobs:**

#### create-release
- **Purpose**: Parse version file and create pre-release
- **Permissions**: `contents: write`
- **Steps**:
  1. Checkout with depth 2
  2. Check if `.version` file exists
     - Creates with `0.0.0` if missing
     - Extracts semantic version (first 3 parts)
     - Parses full version string
  3. Check if release already exists
     - Skips if release with same version already exists
     - Prevents duplicate releases
  4. Generate release notes with version details
  5. Create pre-release via `gh release create`
  6. Report results (skip if default or exists)

**Version File Format**:
- Plain text file containing semantic version
- Example: `1.2.3`
- Whitespace is trimmed automatically

**Release Notes Include**:
- Semantic version (vX.X.X)
- Full version string
- Commit SHA reference
- Branch name
- Link to commit history since previous release

---

### 3. **deploy-cloudflare-pages.yml**

**Trigger Events:**
- Push to `main`, `v1.x` branches
- Pull requests to `main`
- Path-based: changes to `website/**`, `.version`, or workflow file

**Purpose**: Build and deploy website to Cloudflare Pages

**Jobs:**

#### build-and-deploy
- **Purpose**: Build and deploy website
- **Steps**:
  1. Checkout code
  2. Setup Node.js 18.17.0
  3. Install dependencies via `npm ci`
  4. Run npm audit security check (non-blocking)
  5. Build website with `npm run build`
  6. Deploy to Cloudflare Pages
  7. Post deployment preview URL to PR comments

**Security Features**:
- npm audit for dependency vulnerability scanning
- Audit level set to moderate (allows low severity issues)
- Non-blocking to allow deployment to proceed

**PR Integration**:
- Posts deployment preview URL as comment
- Provides real-time feedback on website changes

---

## Version Management

### .version File

**Location**: Repository root (`.version`)

**Format**: Semantic versioning
```
MAJOR.MINOR.PATCH
1.2.3
```

**Usage**:
- Edit `.version` file to trigger version-release workflow
- Commit with message like: `Release v1.2.3`
- Both workflows (docker-build and version-release) detect changes
- Images are automatically tagged with version info

**Detection Logic**:
```bash
# Check if .version was changed in latest commit
git diff --name-only HEAD^ HEAD | grep -q "^.version$"
```

### Epoch64 Timestamp

**Purpose**: Unique identifier for non-version builds

**Format**: Unix timestamp in seconds since epoch

**Usage**:
```
# Regular builds on non-main branches
alpha-<epoch64>        # Feature branches
beta-<epoch64>         # Main branch without version change

# Version release builds
v1.2.3-alpha          # Feature branches with version change
v1.2.3-beta           # Main branch with version change
```

**Example**: `beta-1702312159` = built at 2023-12-11 14:09:19 UTC

---

## Security Scanning

### bandit (Python)
- **Location**: `proxy/`, `management/`, `shared/` directories
- **Severity Level**: `-ll` (low and above)
- **Format**: JSON output
- **Purpose**: Detect security issues in Python code
- **Status**: Non-blocking (uses `|| true` to allow failures)

### gosec (Go)
- **Note**: Currently no Go services in WaddleAI
- **When to add**: If Go-based services are introduced
- **Configuration**: Severity high, confidence medium
- **Location**: Service-specific workflow

### npm audit
- **Location**: `website/` directory
- **Level**: Moderate
- **Purpose**: Detect dependency vulnerabilities
- **Status**: Non-blocking (allows deployment to proceed)

### Trivy (Container Scanning)
- **Purpose**: Scan built Docker images for vulnerabilities
- **Format**: SARIF (GitHub Security compatible)
- **Triggers**: `build-and-push` job
- **Upload**: Automatic to GitHub Security tab
- **Conditions**: Skipped on PRs

---

## Best Practices

### Updating Workflows

1. **Path Filters**: Always include `.version` and `.github/workflows/*.yml`
2. **Checkout Depth**: Use `fetch-depth: 0` for version detection
3. **Epoch64**: Generate in every build job (stateless timestamp)
4. **Version Detection**: Check for `.version` changes in every main build
5. **Conditional Tags**: Use enable conditions for branch-specific tags
6. **Security Scanning**: Always include language-specific security tools

### Creating New Workflows

1. **Use matrix strategy** for multi-service builds
2. **Add path filters** to reduce unnecessary runs
3. **Include epoch64 timestamp** generation
4. **Add version file detection**
5. **Use conditional tags** with semver and epoch64
6. **Add security scanning** appropriate to language
7. **Document in this file** with service details

### Version Release Process

1. Update `.version` file with new version number
2. Commit: `git add .version && git commit -m "Release vX.X.X"`
3. Push to main: `git push`
4. docker-build workflow tags images automatically
5. version-release workflow creates GitHub pre-release
6. Optionally create GitHub release manually for final release

---

## Environment Configuration

### Required Secrets

- `GITHUB_TOKEN`: Provided by GitHub Actions (permissions: contents: write for releases)

### Optional Secrets

- `CODECOV_TOKEN`: For Codecov integration (if using Codecov)
- `CLOUDFLARE_API_TOKEN`: For Cloudflare Pages deployment
- `CLOUDFLARE_ACCOUNT_ID`: For Cloudflare Pages deployment

### Container Registry

- **Registry**: GitHub Container Registry (ghcr.io)
- **Image Prefix**: `ghcr.io/penguintechinc/waddleai`
- **Images**:
  - `ghcr.io/penguintechinc/waddleai/proxy`
  - `ghcr.io/penguintechinc/waddleai/management`

---

## Troubleshooting

### Workflow Not Triggering

1. **Check path filters**: Ensure modified files match `paths:` configuration
2. **Check branches**: Ensure pushing to configured branches
3. **Verify permissions**: Token must have `contents: write`
4. **Check workflow file**: Syntax errors prevent execution

### Build Failures

1. **Python errors**: Check bandit output for security issues
2. **Docker build errors**: Check Dockerfile syntax and dependencies
3. **Integration test failures**: Check docker-compose configuration
4. **Registry login errors**: Verify GITHUB_TOKEN permissions

### Version Not Detected

1. **Fetch depth**: Ensure `fetch-depth: 0` on checkout
2. **File format**: `.version` should contain only version number (no newlines)
3. **Git history**: First commit after creating `.version` won't detect change

### Images Not Tagged Correctly

1. **Epoch64 generation**: Verify timestamp step completed
2. **Version detection**: Check version file parsing output
3. **Conditional logic**: Review `enable=` conditions for branch/change detection
4. **Registry login**: Verify login step succeeded before push

---

## Related Documentation

- [WaddleAI Architecture](NETWORK-ARCHITECTURE.md)
- [Development Standards](STANDARDS.md)
- [License Integration](licensing/license-server-integration.md)
- [Project Template CI/CD](../project-template/.github/workflows/)

---

**Last Updated**: 2025-12-11
**WaddleAI Version**: 0.0.0-beta
**Services**: Proxy (Python), Management (Python)
