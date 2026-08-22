# WaddleAI CI/CD Workflows

This document describes all GitHub Actions workflows for WaddleAI and the services they manage.

## WaddleAI Services Overview

WaddleAI is an AI proxy system with the following core services:

| Service | Language | Purpose | Location | Port |
|---------|----------|---------|----------|------|
| **Proxy** | Python 3.13 (Quart/hypercorn) | OpenAI-compatible request routing, rate limiting, caching | `proxy/` | 8080 |
| **Management** | Python 3.13 (Quart/hypercorn) | Admin API, user management, monitoring | `services/management/` | 8001 |
| **WebUI** | React/Vite | Operator/admin frontend | `services/webui/` | — |

Runtime database access goes through `penguin-dal`; SQLAlchemy + Alembic
(`services/management/alembic/`) are schema/migration only. Cache/session store is Valkey,
not Redis (see `k8s/helm/waddleai/values.yaml`).

### Service Architecture

```
┌─────────────────────────────────────────────────┐
│           Client Applications                   │
└──────────────────────┬──────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────┐
│      Proxy Service (Port 8080)                  │
│  - OpenAI-compatible API endpoints              │
│  - Request routing and load balancing           │
│  - Rate limiting and caching                    │
│  - Authentication enforcement                   │
└──────────────────────┬──────────────────────────┘
                       │
        ┌──────────────┴──────────────┐
        │                             │
┌───────▼──────────┐      ┌──────────▼────────────┐
│  Management API  │      │  Backend Services    │
│  (Port 8001)     │      │  - PostgreSQL        │
│                  │      │  - Valkey Cache      │
│  - WebUI backend │      │  - External LLM APIs │
│  - Settings      │      │                      │
│  - User Mgmt     │      │                      │
└──────────────────┘      └──────────────────────┘
```

## Workflow Files

WaddleAI ships three workflows under `.github/workflows/`: `docker-build.yml`,
`codeql.yml`, and `version-release.yml`. There is no website deploy workflow — no
`deploy-cloudflare-pages.yml` exists and no `website/` directory exists in this repo.

### 1. **docker-build.yml**

**Trigger Events** (five-tier image mapping — see Image Tagging Logic below):
- Push to `release/v{Major}.{Minor}.X` branches → alpha tier (`alpha-<epoch64>`)
- Push to `main` → beta tier (`beta-<epoch64>`)
- GitHub Release published, flagged pre-release → gamma tier (`gamma-<epoch64>`)
- GitHub Release published, not pre-release (v1.x+ only) → prod tier
  (`v{Major}.{Minor}.{Patch}`)
- Push to `feature/`, `fix/`, `chore/`, `hotfix/`, `docs/`, `refactor/` branches, and
  pull requests targeting `main` **and** `release/**` (release-targeted PRs used to run
  no tests or builds at all; this was fixed so the auto-merge green gate has something
  to actually gate on) → pre-alpha: build/test only, image not pushed
- Path-based: changes to `proxy/**`, `services/**`, `shared/**`, `.version`, or the
  workflow file itself

**Jobs (in dependency order):**

#### test
- **Purpose**: Python unit tests and security scanning
- **Matrix**: Python 3.13
- **Steps**: checkout → epoch64 timestamp → `.version` change detection → set up Python →
  cache pip → install `requirements.txt` + `services/management/requirements.txt` +
  `pytest`/`bandit` → `pytest tests/unit/ -v --cov=shared --cov-report=xml --cov-report=html`
  → bandit (two passes: `-ll` non-blocking JSON report, `-lll` HIGH-severity gate over
  `proxy services/management shared`) → upload coverage to Codecov

#### test-webui
- **Purpose**: Lint, unit test, and build `services/webui` — added after the service went
  unnoticed for a while with 19 lint errors and 17 failing tests because it was only ever
  Docker-built, never linted/tested in CI
- **Steps**: checkout → set up Node.js 24 → `npm ci` → `npm run lint` → `npm test`
  (vitest with `--coverage`, enforces the 90% thresholds in `vitest.config.js`) → `npm run build`

#### build-platform
- **Purpose**: Build multi-architecture Docker images and push to registry
- **Needs**: `test`
- **Matrix**: `platform: [linux/amd64, linux/arm64]` × `service: [proxy, management]`, plus
  an explicit `webui` include
- **Per-service build context** (not uniform — `management` moved into `services/` during
  the Phase-1 consolidation and needs the repo root as build context to `COPY` `shared/`):
  - `proxy`: context `.`/`proxy` default, `./proxy/Dockerfile`
  - `management`: context `.` (repo root), `./services/management/Dockerfile`
  - `webui`: context `./services/webui`, `./services/webui/Dockerfile`
- **Steps**: checkout → epoch64 timestamp → `.version` change detection → QEMU → Buildx →
  registry login (skipped on PR) → compute per-arch tags → build and push (skipped on PR)

**Image Tagging Logic** (per architecture, then merged into a manifest — see
`merge-manifests` below):

| Tier | Source | Tag |
|------|--------|-----|
| Pre-alpha | `feature/`/`fix/`/`chore/`/`hotfix/`/`docs/`/`refactor/` branches, PRs | build-only, not pushed (local builds → `localhost:32000`) |
| Alpha | `release/v{Major}.{Minor}.X` branches | `alpha-<epoch64>-<arch>` |
| Beta | `main` | `beta-<epoch64>-<arch>` |
| Gamma | GitHub release flagged pre-release | `gamma-<epoch64>-<arch>` |
| Prod | GitHub release, not pre-release (v1.x+ only) | `v{Major}.{Minor}.{Patch}-<arch>` |

Every pushed (non-pre-alpha) build additionally gets a `ci-<arch>-<sha>` (long SHA) tag
for traceability.

#### merge-manifests
- **Purpose**: Combine the per-arch images from `build-platform` into a single
  multi-arch manifest per tag
- **Needs**: `build-platform`

#### security-scan
- **Purpose**: Trivy vulnerability scan on the built images
- **Needs**: `merge-manifests`
- **Matrix**: `service: [proxy, management, webui]`
- **Conditions**: Trivy run + SARIF upload both skipped on PRs
- Trivy pinned to `v0.69.3`

#### integration-test
- **Purpose**: Smoke-check the built images against a real Postgres + Valkey via an
  ephemeral, CI-generated `docker-compose.test.yml` (not a committed file, and not the
  local-dev workflow — see `docs/PRE_COMMIT.md`)
- **Needs**: `merge-manifests`
- **Conditions**: Skipped on PRs; `continue-on-error: true`
- **Steps**: log in to GHCR → write `docker-compose.test.yml` → `docker compose up -d` →
  curl proxy/management health endpoints → curl the chat-completions endpoint expecting
  `401` without auth → `docker compose down -v` (always)

#### release
- **Purpose**: Create a GitHub release on a version tag
- **Needs**: `test`, `security-scan`, `integration-test`
- **Conditions**: Only on tags matching `refs/tags/v*`
- **Steps**: checkout with full history → generate changelog from `git log` → create
  GitHub release with the changelog

#### cleanup
- **Purpose**: Remove untagged images from GHCR
- **Needs**: `test`, `security-scan`, `integration-test`
- **Conditions**: Always runs (even on failures)

---

### 2. **version-release.yml** (`name: Create Release on Version Change`)

**Trigger Events:**
- Push to `main` branch
- Path-based: changes to `.version` file only

**Purpose**: Automatically create a GitHub pre-release when `.version` changes.

**Job: create-release**
- Reads `.version`, extracts the semver (`Major.Minor.Patch`, stripping the trailing
  epoch64 build component)
- Skips if the version is the default `0.0.0`, or if a release with that tag already
  exists (checked via `gh release view`)
- Generates release notes (semver, full version string, commit SHA, branch) and creates
  the pre-release via `gh release create ... --prerelease`

**Version File Format**: plain text, `vMajor.Minor.Patch.epoch64build` (e.g.
`v0.2.0.1787265396`); whitespace trimmed automatically. The build field is the
epoch at build time, so the live value changes constantly -- read `.version`
rather than relying on any figure quoted here.

---

### 3. **codeql.yml**

**Trigger Events:**
- Push to `main` or `release/**`
- Pull requests targeting `main` or `release/**`
- Weekly schedule (`0 8 * * 1`) so advisories published after a merge still surface

**Purpose**: Supplies the code-scanning results the org ruleset's `require_code_scanning`
rule expects. `docker-build.yml`'s own Trivy SARIF upload lives in `security-scan`, which
is gated on `github.event_name != 'pull_request'` and therefore never runs on a PR — without
this workflow, PRs into `main`/`release/**` sat at "Waiting for Code Scanning results"
forever.

**Matrix**: `python` (build-mode: none), `javascript-typescript` (build-mode: none),
`actions` (build-mode: none). Go is intentionally excluded for now — it's a compiled
language needing autobuild verification across two independent Go modules
(`shared/go_libs`, `services/penguincode/shared/go_libs`), and Go is being phased out in
favor of Rust per the language standard, so the existing `.go` files are library code
rather than a shipped service.

---

## Version Management

### .version File

**Location**: Repository root (`.version`)

**Format**: `vMajor.Minor.Patch.epoch64build` — e.g. `v0.2.0.1787265396`

**Usage**:
- Edit `.version` on `main` to trigger `version-release.yml`'s auto pre-release, which
  in turn triggers the gamma-tier build (`gamma-<epoch64>`); promoting that pre-release
  to a full GitHub release triggers the prod-tier build (`v{Major}.{Minor}.{Patch}`,
  v1.x+ only) — see Image Tagging Logic above
- Only increment Major/Minor/Patch once the current version has a published tag/release —
  otherwise update the build/epoch component only (see the `versioning` skill)

**Detection Logic** (used by both workflows):
```bash
git diff --name-only HEAD^ HEAD | grep -q "^.version$"
```

---

## Security Scanning

### bandit (Python)
- **Location**: `proxy/`, `services/management/`, `shared/`
- **Two passes in CI** (`test` job): `-ll` (low+) writes a non-blocking JSON report;
  `-lll` (HIGH only) gates the build
- Locally: `make test-security` runs `bandit -r . -x ./tests,./venv,./.git --quiet`
  (non-blocking, repo-wide) — narrower and stricter than CI; match CI's exact invocation
  when you need to reproduce a CI failure locally

### npm audit
- Run per `package.json` found in the repo (`make test-security`), non-blocking
- `services/webui` additionally runs `npm run lint` and `npm test` (coverage-gated) in
  the dedicated `test-webui` CI job, not as a security scan

### Trivy (Container Scanning)
- **Purpose**: Scan the merged multi-arch images for vulnerabilities
- **Format**: SARIF, uploaded to the GitHub Security tab
- **Version pinned**: `v0.69.3`
- **Conditions**: Skipped on PRs (see `codeql.yml` above for why PRs still get scanning
  results)

### CodeQL
- Runs on every push/PR to `main`/`release/**` plus a weekly schedule
- Languages: python, javascript-typescript, actions (see `codeql.yml` section above)

---

## Best Practices

### Updating Workflows

1. **Path Filters**: Always include `.version` and `.github/workflows/*.yml`
2. **Checkout Depth**: Use `fetch-depth: 0` where version/tag history detection is needed
3. **Epoch64**: Generate in every build job (stateless timestamp)
4. **Version Detection**: Check for `.version` changes in every relevant job
5. **Conditional Tags**: Use `enable=` conditions for branch/version-specific tags
6. **Security Scanning**: Always include language-specific security tools
7. **PR triggers**: Remember `release/**` branches need the same triggers as `main` —
   that gap already caused release-targeted PRs to merge with a green gate that had
   nothing behind it

### Version Release Process

1. Update `.version` file with the new version number
2. Commit: `git add .version && git commit -m "Release vX.X.X"`
3. Push to `main` (only via an approved release → main PR, never directly — see
   `.claude/rules` `devops.md` Branch & Release Strategy) → CI builds and pushes the
   beta tier (`beta-<epoch64>`)
4. `version-release.yml` detects the `.version` change and auto-creates a GitHub
   pre-release → CI builds and pushes the gamma tier (`gamma-<epoch64>`); validate
   gamma (upgrade-in-place — the only tier that exercises a real migration upgrade,
   mandatory before prod)
5. Promote the pre-release to a final GitHub release manually once gamma is validated
   → CI builds and pushes the prod tier (`v{Major}.{Minor}.{Patch}`, v1.x+ only)

---

## Environment Configuration

### Required Secrets

- `GITHUB_TOKEN`: Provided by GitHub Actions (`contents: write` for releases,
  `packages: write` for image push/cleanup, `security-events: write` for SARIF upload)

### Optional Secrets

- `CODECOV_TOKEN`: For Codecov integration, if enabled for this repo

### Container Registry

- **Registry**: GitHub Container Registry (`ghcr.io`)
- **Image Prefix**: `ghcr.io/penguintechinc/waddleai`
- **Images**:
  - `ghcr.io/penguintechinc/waddleai/proxy`
  - `ghcr.io/penguintechinc/waddleai/management`
  - `ghcr.io/penguintechinc/waddleai/webui`

---

## Troubleshooting

### Workflow Not Triggering

1. **Check path filters**: Ensure modified files match `paths:` configuration
2. **Check branches/events**: Ensure pushing to/PR-targeting a configured branch
   (`main`, `release/v{Major}.{Minor}.X`, or `release/**` depending on the workflow),
   or — for gamma/prod — that the GitHub Release event fired with the right
   pre-release flag
3. **Verify permissions**: Token must have the permissions the job declares
4. **Check workflow file**: Syntax errors prevent execution

### Build Failures

1. **Python errors**: Check the `test` job's bandit/pytest output
2. **Web UI errors**: Check the `test-webui` job's ESLint/vitest output — this job exists
   specifically because these failures used to go unnoticed
3. **Docker build errors**: Check the Dockerfile and the per-service build context/
   dockerfile pairing in `build-platform`'s matrix `include` (management's context is the
   repo root, not `services/management/`)
4. **Integration test failures**: Check the generated `docker-compose.test.yml` step's
   output in the `integration-test` job log — the file itself is not committed
5. **Registry login errors**: Verify `GITHUB_TOKEN` permissions

### Version Not Detected

1. **Fetch depth**: Ensure `fetch-depth: 0` (or `2`) on checkout, as required by the job
2. **File format**: `.version` should contain only the version string
3. **Git history**: The first commit after creating `.version` won't detect a change
   (there's no prior commit to diff against)

---

## Related Documentation

- [WaddleAI Network Architecture](../NETWORK-ARCHITECTURE.md)
- [Development Standards](STANDARDS.md)
- [Testing Guide](TESTING.md)
- [Pre-Commit Checklist](PRE_COMMIT.md)

---

**Last Updated**: 2026-08-10
**WaddleAI Version**: v0.1.0 (see `.version` for the full build string)
**Services**: Proxy, Management (Python/Quart), WebUI (React)
