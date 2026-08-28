# Kubernetes Deployment

WaddleAI ships one Helm chart (`k8s/helm/waddleai`) and deploys the same way in every
environment. Docker Compose is not a supported deployment path in any environment,
including local development — see [Docker Compose](docker-compose.md) for why.

!!! warning "Only alpha and beta have working values files today"
    The chart currently ships `values.yaml` (defaults) plus `values-alpha.yaml` and
    `values-beta.yaml` overlays. There is no `values-gamma.yaml` or
    `values-production.yaml` in the repo yet, and the `Makefile`'s `deploy-prod` target
    exits 1 with a pointer back to this page (`ERROR: deploy-prod is not implemented.
    See docs/docs-site/docs/deployment/kubernetes.md`) rather than doing anything.
    Until those land, treat the steps below as alpha/beta-verified; a production
    rollout needs a production values file authored first (start from `values.yaml` +
    `values-beta.yaml` as a base and layer the differences).

## Prerequisites

- Kubernetes 1.28+
- Helm 3
- `kubectl` configured with the right context (see [Cluster contexts](#cluster-contexts))
- Cilium as CNI — the chart's `cilium.enabled` toggle (on by default) renders
  `CiliumNetworkPolicy`/`CiliumClusterwideNetworkPolicy` and RBAC for the
  Management-service Cilium reconciler. Every Cilium-emitting template is also guarded
  by `.Capabilities.APIVersions.Has "cilium.io/v2"`, so a cluster without the Cilium
  CRDs (e.g. a bare MicroK8s/kind install) skips those resources cleanly instead of
  failing `helm install`
- A GPU-labeled node (`kubectl label node <name> gpu=true`) only if you plan to enable
  Ollama or llama.cpp (`ollama.enabled` / `llamacpp.enabled`, both `false` by default)

### Cluster contexts

| Context | Cluster | Values file | Namespace |
|---|---|---|---|
| `local-alpha` | Local MicroK8s / Docker Desktop — shared by the pre-alpha and alpha tiers today | `values-alpha.yaml` | `waddleai` |
| `dal2-beta` | dal2.penguintech.cloud (**temporarily offline as of 2026-08-21**, expected back within ~1 week) | `values-beta.yaml` | `waddleai` |
| TBD | DigitalOcean gamma cluster (context name not yet assigned) | *(no values-gamma.yaml yet)* | `waddleai` |
| TBD | DigitalOcean prod cluster — separate cluster from gamma, not built yet | *(no values-production.yaml yet)* | `waddleai` |

`namespace` defaults to `waddleai` in the base `values.yaml` and neither
`values-alpha.yaml` nor `values-beta.yaml` overrides it, and `scripts/deploy-beta.sh`
defaults its own `NAMESPACE` env var to `waddleai` too — every environment uses the
product-name-only namespace, no environment suffix.

See [Release pipeline](#release-pipeline) below for which branch/event produces which
tag and cluster.

## Release pipeline

WaddleAI images move through five tiers, each with its own source branch/event, tag,
and cluster:

| Tier | Source | Tag | Cluster / deploy |
|---|---|---|---|
| Pre-alpha | `feature/` `fix/` `chore/` `hotfix/` `docs/` `refactor/` branches | build-only in CI (compile test, not pushed); local builds → `localhost:32000` | Local K8s (MicroK8s / Docker Desktop), `./scripts/deploy-alpha.sh`, destroy + fresh |
| Alpha | `release/v{Major}.{Minor}.X` branches | `alpha-<epoch64>` | Local K8s today (`dal3` planned), destroy + fresh |
| Beta | Merge to `main` | `beta-<epoch64>` | `dal2-beta` context, GitHub Actions, destroy + fresh — `./scripts/deploy-beta.sh --tag beta-<epoch64>` |
| Gamma | GitHub release flagged pre-release | `gamma-<epoch64>` | DigitalOcean (context TBD), **upgrade in place** — the only upgrade-path test, mandatory before prod |
| Prod | GitHub release (non-pre-release), **v1.x+ only** | `v{Major}.{Minor}.{Patch}` | DigitalOcean, separate cluster (not built yet), upgrade in place |

Namespace is always `waddleai` in every context; registry is
`ghcr.io/penguintechinc/waddleai/{proxy,management,webui,ollama}`.

**Destroy-fresh vs. upgrade-in-place**: pre-alpha, alpha, and beta clusters are torn
down and rebuilt for every deploy — the [migration Job](#database-migrations) still
runs on `helm upgrade --install` there, but against an empty database every time, so it
never actually exercises an upgrade path. **Gamma is the first tier that keeps its
database across deploys** (upgrade in place) — it's the only environment where the
Alembic migration Job runs against real prior state, which is why a clean gamma pass is
mandatory before promoting to prod.

**Promotion chain**: `main` → beta (`beta-<epoch64>`, automated tests) → auto
pre-release (`version-release.yml` creates a GitHub pre-release on a `.version` change)
→ gamma (`gamma-<epoch64>`, upgrade-in-place validation) → **manual** promotion of the
pre-release to a full GitHub release → prod (`v{Major}.{Minor}.{Patch}`, v1.x+ only —
this repo is still pre-1.0 (`.version` currently `v0.2.x`), so the prod tier has no
deploy target yet).

!!! note "dal2-beta is temporarily offline (2026-08-21)"
    As of 2026-08-21 the `dal2-beta` cluster is offline; it's expected back within
    ~1 week. `./scripts/deploy-beta.sh` will fail to reach the `dal2-beta` context
    until then.

## Quick install (alpha / local MicroK8s)

```bash
# Build service images and import them into MicroK8s, then helm upgrade --install
./scripts/deploy-alpha.sh

# Or drive Helm directly:
helm upgrade --install waddleai k8s/helm/waddleai \
  --kube-context local-alpha \
  --namespace waddleai \
  --create-namespace \
  --values k8s/helm/waddleai/values-alpha.yaml
```

`values-alpha.yaml` sets `image.pullPolicy: Never` for management/proxy/webui — images
must already exist in MicroK8s' local image store (`scripts/deploy-alpha.sh` handles
the `docker build` + `microk8s ctr image import` step; use `--skip-build` to skip it
and reuse whatever is already imported).

## Configuration

Values are layered: `values.yaml` (defaults) → `values-alpha.yaml`/`values-beta.yaml`
(environment overrides) → `--set` flags. Keys that matter for a real deployment:

| Key | Default (`values.yaml`) | Notes |
|---|---|---|
| `namespace` | `waddleai` | Product name only — never environment-suffixed |
| `global.imageRegistry` | `registry-dal2.penguintech.io` | Beta overrides to `ghcr.io/penguintechinc/waddleai` |
| `management.image.repository` / `.tag` / `.digest` | `waddleai/management` / `latest` / unset | `.digest` is templated (`waddleai.management.image` helper, `templates/_helpers.tpl`) — set it for a SHA256-pinned prod image per house pinning rules |
| `proxy.image.repository` / `.tag` / `.digest` | `waddleai/proxy` / `latest` / unset | Same digest-or-tag pattern, `waddleai.proxy.image` helper |
| `webui.image.repository` / `.tag` / `.digest` | `waddleai/webui` / `latest` / unset | Same digest-or-tag pattern, `waddleai.webui.image` helper |
| `management.env.LICENSE_SERVER_URL` / `NER_SPACY_MODEL` / `WADDLEAI_NER_ALLOW_DOWNLOAD` | `https://license.penguintech.io` / `en_core_web_lg` / `false` | Plain (non-secret) env vars, also set on `proxy.env` with the same defaults |
| `WADDLEAI_PUBLIC_HOST` (management + proxy) | Derived, not a `values.yaml` key | Auto-populated from `httproute.host`/`ingress.hosts[0].host` (`waddleai.publicHost` helper, `templates/_helpers.tpl`); omitted entirely if neither is enabled. Feeds `penguin_licensing`'s domain-based licence bypass (`shared/licensing/domain_bypass.py`) — never set this by hand |
| `migrations.enabled` | `true` | Master switch for the Alembic migration Job (see [Database migrations](#database-migrations)) |
| `ollama.image.repository` / `.tag` | `ghcr.io/penguintechinc/waddleai/ollama` / `hardened` | `ollama.enabled` defaults `false`; DaemonSet (`mode: daemonset`) targets `gpu=true`-labeled nodes |
| `postgres.persistence.size` | `10Gi` (`5Gi` alpha, `20Gi` beta) | In-chart Postgres; there is no external-DB toggle today |
| `valkey.persistence.size` | `5Gi` (`2Gi` alpha, `10Gi` beta) | |
| `ingress.enabled` | `false` (`true` alpha, `false` beta) | Beta uses `httproute` (Cilium Gateway API) instead |
| `httproute.enabled` | `false` (`true` beta) | Routes `/v1/`, `/mem0/` → proxy:8080; `/api` → management:8001; `/` → webui:8080 |
| `cilium.enabled` / `.networkPolicy.enabled` / `.rateLimit.enabled` | `true` | Master switches for the bootstrap `CiliumNetworkPolicy` set and the per-org rate-limit reconciler (reconciler itself additionally gated by the `waddleai.native_rate_limit` PostHog flag) |
| `secrets.manage` | `true` | Set `false` to bring your own `waddleai-secrets` Secret instead of the chart-managed one |
| `fleet.external.enabled` | `false` | Opt-in mTLS/token auth for bare-metal Ollama/llama.cpp nodes outside the cluster (spec §10.3) |

## Required secrets

With `secrets.manage: true` (default), `templates/secret.yaml` renders the
`waddleai-secrets` Secret from `values.secrets.*` as a `pre-install,pre-upgrade` Helm
hook (weight `-20`, so it exists before anything that references it — including the
migration Job below). Its `hook-delete-policy` does **not** include `hook-succeeded`,
and a hook resource isn't part of the normal release manifest Helm tracks for
deletion — so `helm uninstall` leaves `waddleai-secrets` in place rather than deleting
it. That's deliberate: it protects `credential-encryption-key` (see the warning below)
from being wiped out by an uninstall/reinstall cycle.

| Secret key (in `waddleai-secrets`) | `values.yaml` source | Consumed by |
|---|---|---|
| `postgres-password` | `secrets.postgresPassword` (`changeme-*` placeholder) | postgres container, `database-url` below |
| `database-url` | derived: `postgresql://waddleai:<postgres-password>@<release>-postgres:5432/waddleai` | management + proxy (`DATABASE_URL`), and the migration Job |
| `jwt-secret` | `secrets.jwtSecret` (`changeme-*` placeholder) | management (`JWT_SECRET`) |
| `flask-secret-key` | `secrets.flaskSecretKey` (`changeme-*` placeholder) | management (`FLASK_SECRET_KEY`) |
| `webhook-secret` | `secrets.webhookSecret` (`changeme-*` placeholder) | management (`WEBHOOK_SECRET`) |
| `fleet-external-token` | `secrets.fleetExternalToken` (`changeme-*` placeholder) | fleet-external-token-proxy sidecar, only when `fleet.external.enabled` + `mode: token` |
| `proxy-grpc-auth-token` | `secrets.proxyGrpcAuthToken`, empty by default | proxy (`PROXY_GRPC_AUTH_TOKEN`) |
| `admin-initial-password` | `secrets.adminInitialPassword`, empty by default | management (`ADMIN_INITIAL_PASSWORD`) |
| `license-key` | `secrets.licenseKey`, empty by default | management + proxy (`LICENSE_KEY`) |
| `credential-encryption-key` | `secrets.credentialEncryptionKey`, empty by default | management (`CREDENTIAL_ENCRYPTION_KEY`) |

Every `changeme-*` placeholder above must be overridden before any beta/prod install
(`--set-file` or a private values overlay, never a committed plaintext value). The
last four keys are intentionally left empty in `values.yaml` and behave differently:

- **`proxy-grpc-auth-token`** — generate-once-and-keep-stable. If
  `secrets.proxyGrpcAuthToken` is unset, `waddleai.proxyGrpcAuthToken`
  (`templates/_helpers.tpl`) does a `lookup` against the existing
  `waddleai-secrets` Secret and reuses its value across `helm upgrade`; only a
  first install with no existing Secret generates a fresh 64-char random value.
  Set it explicitly to pin/rotate.
- **`credential-encryption-key`** — same generate-once/keep-stable `lookup` pattern
  (`waddleai.credentialEncryptionKey`), for a stronger reason: it Fernet-encrypts
  every stored provider API credential
  (`shared/security/credential_encryption.py`). **Never rotate it after go-live** —
  the decrypt path has no key-versioning, so replacing the key makes every
  already-encrypted `enc:...` credential in the database permanently unreadable. Set
  it explicitly only to pin/restore a known value (disaster recovery, or an External
  Secrets Operator supplying it in beta/prod).
- **`admin-initial-password`** — **not** auto-generated. Leaving it empty lets
  `services/management/app/extensions.py`'s `init_default_data()` run its own
  fail-closed path: it creates the `admin` user with a `secrets.token_urlsafe(16)`
  password that is never logged anywhere, so the account is unusable until you
  redeploy with a known password. Set it via
  `--set secrets.adminInitialPassword=<your-password>` before first install, or
  supply it through an externally managed Secret (`secrets.manage: false`).
- **`license-key`** — empty by default = community tier.
  `shared/licensing/python_client.py` logs a warning, not a startup failure, when
  it's unset, so license-gated features just stay off.

`LICENSE_SERVER_URL`, `NER_SPACY_MODEL`, and `WADDLEAI_NER_ALLOW_DOWNLOAD` are plain
(non-secret) env vars, not Secret-backed — they're set directly on
`management.env`/`proxy.env` with real defaults (see the [Configuration](#configuration)
table) and can be overridden per environment, e.g.
`--set-string management.env.WADDLEAI_NER_ALLOW_DOWNLOAD=true`. Without that flag set,
the NER security tier stays disabled rather than pulling the ~400MB spaCy model at
runtime — see [Troubleshooting](#troubleshooting).

## Ingress / TLS

Two mutually-exclusive paths, controlled per environment:

- **`ingress.enabled: true`** (alpha default) — classic `nginx` Ingress with
  `cert-manager.io/cluster-issuer` annotations (`letsencrypt-staging` alpha,
  `letsencrypt-prod` in the `values.yaml` default). TLS secret name:
  `waddleai-tls`.
- **`httproute.enabled: true`** (beta default) — Cilium Gateway API `HTTPRoute`
  against a shared `Gateway` (`httproute.gatewayName`/`httproute.gatewayNamespace`,
  default `shared`/`gateway`), with path rules for `/v1/`, `/mem0/`, `/api`, `/`
  (see `templates/httproute.yaml`).

Neither path is enabled by default in the base `values.yaml` for `httproute`
(`ingress.enabled: false` is the base default) — each environment overlay picks one.

## Database migrations

Schema handling is two mechanisms:

1. **Initial schema + default data** — `services/management/app/extensions.py`'s
   `init_extensions()` runs on every management pod startup and is idempotent. No
   action needed.
2. **Versioned Alembic migrations** (`services/management/alembic/versions/*.py`) —
   these run automatically as a **Helm hook Job**
   (`templates/migration-job.yaml`, `waddleai-migration`), gated by
   `migrations.enabled` (default `true`). It's a `pre-install,pre-upgrade` hook at
   weight `-10` (after the `waddleai-secrets` Secret hook at weight `-20`, so its
   `DATABASE_URL` secretKeyRef always resolves), and it runs `alembic upgrade head`
   from the management image's own `/opt/venv/bin/python3 -m alembic` — the image
   now bundles `alembic.ini` and `alembic/` alongside `app/`
   (`services/management/Dockerfile:60-61`), so this is no longer a manual,
   out-of-band step. The Job runs **before** the management Deployment rolls out new
   pods, so schema and code land together automatically on every
   `helm install`/`helm upgrade`.

   Watch it:

   ```bash
   kubectl --context <context> -n waddleai get jobs
   kubectl --context <context> -n waddleai logs job/waddleai-migration
   ```

   Disable it (e.g. to run migrations out-of-band yourself) with
   `--set migrations.enabled=false`.

   **On failure**: `backoffLimit: 3` and `activeDeadlineSeconds: 300` bound the Job,
   but a failing or stuck Job blocks the whole `helm install`/`helm upgrade` — Helm
   waits on pre-install/pre-upgrade hooks before touching any other resource, so the
   management/proxy/webui Deployments never roll out until the migration succeeds.
   Fix DB connectivity (check `kubectl -n waddleai logs job/waddleai-migration` and
   confirm `DATABASE_URL` resolves and Postgres is reachable), then re-run
   `helm upgrade` — `hook-delete-policy: before-hook-creation,hook-succeeded` cleans
   up the failed Job automatically so the retry doesn't hit "already exists".

## Licence activation

`LICENSE_SERVER_URL` is a plain env var on both management and proxy, defaulting to
`https://license.penguintech.io` in `values.yaml` — override with
`--set-string management.env.LICENSE_SERVER_URL=... --set-string proxy.env.LICENSE_SERVER_URL=...`
if you run your own license server.

`LICENSE_KEY` is Secret-backed (see [Required secrets](#required-secrets)) — set it
with `--set-string secrets.licenseKey=<your-license-key>` before install, or via an
externally managed Secret. `shared/licensing/python_client.py` logs a warning (not a
startup failure) if it's unset, so licence-gated features simply stay off rather than
blocking the deployment.

## First login and creating your first API key

1. Set `secrets.adminInitialPassword` before first install (see
   [Required secrets](#required-secrets)). If it's unset, `init_default_data()`
   (`services/management/app/extensions.py`) still creates the `admin` user, but with
   a `secrets.token_urlsafe(16)` password that is **never logged anywhere** — the
   account is fail-closed and unusable until you redeploy with
   `secrets.adminInitialPassword` set (there is no "check the logs" recovery path;
   the old print-to-stdout bootstrap was removed as CodeQL alert #2507).
2. Log in against the management API (or the WebUI login page, which calls the same
   endpoint) with `admin` / the password you set:

   ```bash
   curl -sX POST https://<your-host>/api/v1/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"username": "admin", "password": "'"$ADMIN_BOOTSTRAP_PASSWORD"'"}'
   ```

   The response includes `access_token` — use it as `Authorization: Bearer <token>`.
3. Create your own API key (`POST /api/v1/keys`, `services/management/app/api/v1/keys.py`):

   ```bash
   curl -sX POST https://<your-host>/api/v1/keys \
     -H "Authorization: Bearer $ACCESS_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"name": "my-first-key"}'
   ```

   The response's `api_key` field is the plaintext key (`wa-...`) and is **only ever
   shown in this response** — store it now (e.g. `$WADDLEAI_API_KEY`). Bootstrap also
   inserts a DB row named "Admin Master Key" with a randomly generated secret that is
   never surfaced anywhere; treat it as inert and create your own key instead of
   trying to recover that one.
4. Change the admin password immediately after first login
   (`POST /api/v1/auth/change-password`) — `secrets.adminInitialPassword` remains set
   on the release otherwise.

## Upgrades and rollback

```bash
# Upgrade (re-run any time values or the chart change)
helm upgrade --install waddleai k8s/helm/waddleai \
  --kube-context dal2-beta \
  --namespace waddleai \
  --values k8s/helm/waddleai/values-beta.yaml \
  --set proxy.image.tag=beta-<epoch64>

# Roll back to the previous release
helm rollback waddleai --kube-context dal2-beta -n waddleai

# Or via the wrapper script
./scripts/deploy-beta.sh --rollback
```

Remember the [migration Job](#database-migrations) runs on every `helm upgrade`, not
just on rollback — a rollback of the chart does not roll back schema; if the release
you're rolling back to predates a migration that already ran, you need a matching
Alembic downgrade (the Job only ever runs `upgrade head`, never a downgrade), run
manually with `migrations.enabled=false` set and `alembic downgrade <revision>` run
out-of-band against the cluster database.

## Verifying

```bash
kubectl --context <context> -n <namespace> get pods
kubectl --context <context> -n <namespace> rollout status deployment/waddleai-management
kubectl --context <context> -n <namespace> rollout status deployment/waddleai-proxy

# Health endpoints (real routes, real ports)
kubectl --context <context> -n <namespace> exec deploy/waddleai-management -- \
  curl -sf localhost:8001/healthz   # always 200 once the process is up — tolerant of DB issues
kubectl --context <context> -n <namespace> exec deploy/waddleai-management -- \
  curl -sf localhost:8001/readyz    # gates on DB + Redis — exists but is NOT the wired readinessProbe
kubectl --context <context> -n <namespace> exec deploy/waddleai-proxy -- \
  curl -sf localhost:8080/readyz    # gates on DB only — this IS the wired readinessProbe for proxy
```

Note the asymmetry: the chart's `management.readinessProbe` points at `/healthz`
(always 200 if the process is running, even with the DB down), while `/readyz`
(DB+Redis-gated) exists in code but isn't what Kubernetes checks. The `proxy`
service's `readinessProbe` does point at `/readyz` and correctly gates on the
database. Don't assume a `Ready` management pod means its DB connection is healthy —
check `/readyz` manually.

## Beta / gamma / prod notes

- **Never build beta/prod/gamma images locally** — CI builds and pushes to
  `ghcr.io/penguintechinc/waddleai/{proxy,management,webui,ollama}`. Tag/source
  conventions are the five-tier table in [Release pipeline](#release-pipeline) above —
  `alpha-<epoch64>` from `release/v{Major}.{Minor}.X` branches, `beta-<epoch64>` from
  merge to `main`, `gamma-<epoch64>` from a GitHub pre-release, `v{Major}.{Minor}.{Patch}`
  from a full GitHub release (v1.x+ only).
- `./scripts/deploy-beta.sh` drives `helm upgrade --install` against `dal2-beta` with
  `values-beta.yaml`, namespace `waddleai`; it also has `--rollback` and `--dry-run`
  modes. It **never builds or pushes images itself** — it only deploys an
  already-CI-built tag via Helm, and requires one:
  `./scripts/deploy-beta.sh --tag=beta-<epoch64>` (optionally `--service=management`
  to bump a single service's tag). It prints the exact `helm upgrade --install`
  command it runs before executing it. `dal2-beta` is temporarily offline as of
  2026-08-21 — see the note in Release pipeline above.
- There is no `deploy-gamma.sh` or `deploy-prod.sh` script and no gamma/production
  values file — see the warning at the top of this page. Gamma and prod also move to
  a new DigitalOcean cluster/context (TBD) instead of `dal2.penguintech.cloud`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Pod `Pending` / stuck `ContainerCreating`, or first requests to a security-scanning endpoint hang for a long time | `WADDLEAI_NER_ALLOW_DOWNLOAD=true` triggers an on-demand ~400MB spaCy model (`NER_SPACY_MODEL`, default `en_core_web_lg`) pull inside the pod, not at image build time (`shared/security/ner_filter.py`) | Bake the model into the image ahead of time, or budget for the download on first use; without the flag set, the NER tier just stays disabled instead of hanging |
| `management` pod shows `Ready` but the app 500s on every DB-backed request | `management.readinessProbe` checks `/healthz`, which returns 200 regardless of DB state (see [Verifying](#verifying)) | Check `/readyz` manually, or `kubectl -n <ns> logs deploy/waddleai-management` for connection errors |
| `proxy` pod never becomes `Ready` | `proxy.readinessProbe` hits `/readyz`, which returns 503 whenever the database check fails | `kubectl -n <ns> logs deploy/waddleai-proxy`; confirm `DATABASE_URL` (via the `waddleai-secrets` Secret) resolves and Postgres is `Ready` first — the init containers (`wait-for-postgres`, `wait-for-valkey`) block pod start on a raw TCP check, but that doesn't guarantee the DB is accepting the actual configured user/db |
| Admin login fails right after install | `secrets.adminInitialPassword` wasn't set, so the admin account got an unrecoverable random password | Redeploy with `--set secrets.adminInitialPassword=<your-password>` (see [Required secrets](#required-secrets)); there is no way to recover the auto-generated one |
| `helm install`/`helm upgrade` hangs, or fails with a hook error referencing `waddleai-migration` | The `waddleai-migration` Job (`migrations.enabled: true`) is `Pending` (can't schedule — e.g. no node capacity) or its `alembic upgrade head` run failed — a pre-install/pre-upgrade hook blocks all other resources until it succeeds | `kubectl -n <ns> get jobs`, `kubectl -n <ns> logs job/waddleai-migration`; fix DB connectivity/scheduling, then re-run `helm upgrade` — the failed Job is cleaned up automatically (`hook-delete-policy: before-hook-creation,hook-succeeded`) |
