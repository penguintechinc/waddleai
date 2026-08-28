# WaddleAI Documentation

WaddleAI is the control plane for AI: one place to manage which models your teams can reach,
apply security controls that hold whether a request lands on a commercial API or a
self-hosted engine, and cut the waste in AI workloads without changing how the calling
tools behave. It sits in front of your models — it does not replace them.

## What WaddleAI Is For

1. **Cross-cloud management of models — and of who may reach them.** One endpoint in front
   of OpenAI, Anthropic, Google Gemini, xAI, AWS Bedrock, Ollama, and self-hosted llama.cpp.
   Virtual keys, per-user/per-team quotas, org scoping, and usage attribution are managed
   centrally across every provider at once.
2. **Security controls that apply to commercial and open engines alike.** PII detection and
   redaction, prompt-injection scanning, output guardrails, and audit logging run in the
   proxy itself, so they behave identically no matter which provider serves the request —
   including outbound: what leaves your building gets scanned on the way out too.
3. **Efficiency, without degrading the calling harness.** Response/semantic caching,
   just-in-time RAG, and lazy-loaded MCP tools cut repeated work and route cheap questions to
   cheap models — but Claude Code, Cursor, Antigravity, and any other caller must see exactly
   the same API shape, streaming, and tool calls they'd get talking to the provider directly.

WaddleAI defaults to open models and open embeddings (Ollama, llama.cpp, Llama, Mistral,
Nomic) running on your own Kubernetes — that's a default, not a limit. Routing to Anthropic,
OpenAI, Gemini, xAI, or Bedrock is a first-class path, and the same security and efficiency
controls apply either way.

## Quick Start

Kubernetes via Helm is the only supported deployment path. See
[Installation](getting-started/installation.md) for prerequisites and a full walkthrough, and
[Kubernetes Deployment](deployment/kubernetes.md) for chart structure and the alpha/beta/prod
values files. This section covers the fastest path from a clean cluster to a working admin
login and your first API key.

### 1. Install the chart

The chart manages its own `waddleai-secrets` Secret by default (`secrets.manage: true`) — do
not pre-create one with the same name, or `helm install` will fail with "already exists".
Set the admin bootstrap password explicitly, or the management service generates a random one
that is never logged and leaves the `admin` account unusable until reset:

```bash
ADMIN_PASSWORD=$(openssl rand -hex 16)

kubectl create namespace waddleai
helm install waddleai k8s/helm/waddleai \
  --namespace waddleai \
  --values k8s/helm/waddleai/values-beta.yaml \
  --set secrets.postgresPassword="$(openssl rand -hex 16)" \
  --set secrets.jwtSecret="$(openssl rand -hex 32)" \
  --set management.env.ADMIN_INITIAL_PASSWORD="$ADMIN_PASSWORD"

echo "Admin password (save this now, it is not shown again): $ADMIN_PASSWORD"
```

> **Known chart gap**: `management.secretEnv` requires a `proxy-grpc-auth-token` key on
> `waddleai-secrets`, but `templates/secret.yaml` doesn't generate one — the management pod
> fails to start (`CreateContainerConfigError`) on a fresh install. Work around it:
> ```bash
> kubectl patch secret waddleai-secrets -n waddleai --type merge \
>   -p "{\"stringData\":{\"proxy-grpc-auth-token\":\"$(openssl rand -hex 32)\"}}"
> kubectl rollout restart deployment/waddleai-management -n waddleai
> ```

### 2. Verify the rollout

```bash
kubectl -n waddleai get pods
kubectl -n waddleai rollout status deployment/waddleai-management
kubectl -n waddleai port-forward svc/waddleai-management 8001:8001 &
curl http://localhost:8001/healthz
```

### 3. Log in to the WebUI

Reach the WebUI via the ingress host configured in your values file (`values-beta.yaml` uses
`waddleai.penguintech.cloud`; `values-alpha.yaml` uses `waddleai.localhost.local`), or
port-forward directly:

```bash
kubectl -n waddleai port-forward svc/waddleai-webui 8080:8080
```

Open `http://localhost:8080` and log in as `admin` with the `ADMIN_PASSWORD` you set above.
There is no default password — an install without `ADMIN_INITIAL_PASSWORD` set bootstraps a
random, unlogged one, and the account needs a reset before first use.

### 4. Create your first API key

The bootstrap process also seeds an internal admin key (used by the proxy's own auth check),
but its value is never logged or displayed anywhere — it is **not retrievable**. Create your
own key from the WebUI (Virtual Keys) or the API:

```bash
curl -X POST http://localhost:8001/api/v1/keys \
  -H "Authorization: Bearer <your-admin-jwt>" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-first-key"}'
```

The response includes `api_key` in plaintext exactly once — save it immediately, it cannot be
retrieved again:

```bash
export WADDLEAI_API_KEY="<your-waddleai-key>"
```

### 5. Call the proxy

```bash
kubectl -n waddleai port-forward svc/waddleai-proxy 8080:8080 &

curl http://localhost:8080/v1/models \
  -H "Authorization: Bearer $WADDLEAI_API_KEY"
```

## Architecture Overview

WaddleAI consists of two main components:

### Proxy Server (Stateless)
- OpenAI-compatible (`/v1/chat/completions`) and Anthropic-compatible (`/v1/messages`)
  endpoints
- Request routing and load balancing across providers
- Security scanning, PII redaction, and prompt injection detection
- Token counting and quota enforcement
- Prometheus metrics and health checks

### Management Server (Stateful)
- Web-based administration portal
- User and organization management
- API key management with RBAC
- Usage analytics and reporting
- LLM provider configuration

## Integration Guide

For detailed integration instructions with various tools and platforms, see the
[Claude Integration](claude.md) guide, which provides comprehensive examples for:

- Python applications with OpenAI SDK
- Node.js applications
- cURL/HTTP requests
- VS Code extension integration
- Management API usage
- Role-based access control

## Navigation

- **[Getting Started](getting-started/installation.md)** — Installation and setup
- **[Kubernetes Deployment](deployment/kubernetes.md)** — Helm chart and alpha/beta/prod values
- **[API Reference](api/openai-compatible.md)** — Complete API documentation
- **[Administration](administration/user-management.md)** — System management
- **[Integrations](integrations/ollama-setup.md)** — Third-party integrations
- **[Troubleshooting](troubleshooting/common-issues.md)** — Common issues and solutions

---

**Ready to get started?** Follow the [installation guide](getting-started/installation.md) or
the Helm quick start above.
