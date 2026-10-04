# Configuration

WaddleAI deploys via **Kubernetes + Helm only** — Docker Compose is deprecated
platform-wide and this repo ships no `docker-compose.yml`. This page covers
the minimum you set for a first deploy; see [Full Reference](#full-reference)
below for every env var and feature flag.

Chart: [`k8s/helm/waddleai`](https://github.com/penguintechinc/waddleai/tree/main/k8s/helm/waddleai)
(+ [`services/penguincode/k8s/helm/penguincode`](https://github.com/penguintechinc/waddleai/tree/main/services/penguincode/k8s/helm/penguincode)
for the penguincode service). Per-environment values files:
`values-alpha.yaml` / `values-beta.yaml` / `values-gamma.yaml` /
`values-production.yaml`, layered on the chart's `values.yaml` defaults.

## Services & Ports

| Service | Port | Purpose |
|---|---|---|
| `management` | 8001 | Control plane API + webui backend |
| `proxy` | 8080 | OpenAI-compatible data plane (`/v1/chat/completions`, `/v1/messages`, `/v1/models`, `/mem0/*`) |
| `proxy` | 50051 | Internal gRPC |
| `webui` | 8080 (in-container) | Static React shell |

`/metrics` (Prometheus) is scraped on each service's own port above — there
is no separate metrics port.

## Core Environment Variables

Set via `values.yaml` `env:`/`secretEnv:` blocks — not plain env files.

| Variable | Service | Purpose |
|---|---|---|
| `DATABASE_URL` | management, proxy | PostgreSQL (+ `pgvector`) connection string — from `waddleai-secrets` |
| `CACHE_HOST` / `CACHE_PORT` | management | Valkey (Redis-protocol) cache — `REDIS_URL` still works but is deprecated |
| `OLLAMA_HOST` / `OLLAMA_MANAGEMENT_MODE` | management | Local model serving, when enabled |
| `JWT_SECRET` | management | Token signing secret — from `waddleai-secrets` |
| `CREDENTIAL_ENCRYPTION_KEY` | management | Generate once, never rotate — see chart `_helpers.tpl` |
| `LICENSE_SERVER_URL` / `LICENSE_KEY` | management, proxy | Defaults to `https://license.penguintech.io`; empty key = community tier |
| `LOG_LEVEL` | all | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | all | OTLP collector URL — unset disables tracing/metrics export, never crashes the app |

Feature gating (Ollama provider, Gemini, Bedrock, Azure OpenAI, Cohere, etc.)
is PostHog-flag-controlled per `critical-rules.md` Feature Flags & License
Tiers — see the chart's `env:` block for the current `ENABLE_*` toggles.

## Full Reference

- [`docs/deployment/CONFIGURATION.md`](../deployment/CONFIGURATION.md) — every
  env var, secret, and feature flag across `management`/`proxy`/`webui`
- [`docs/penguincode/CONFIGURATION.md`](https://github.com/penguintechinc/waddleai/blob/main/docs/penguincode/CONFIGURATION.md) —
  penguincode service configuration

## Next Steps

- [Installation Guide](installation.md)
- [Kubernetes Deployment](../deployment/kubernetes.md)
- [Troubleshooting](../troubleshooting/common-issues.md)
