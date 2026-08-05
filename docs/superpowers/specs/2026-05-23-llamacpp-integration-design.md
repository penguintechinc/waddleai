# llama.cpp Integration Design

**Date:** 2026-05-23
**Branch:** v0.1.x
**Author:** Justin Bowen
**Status:** Approved — pending implementation

---

## Overview

Add native llama.cpp support to WaddleAI, covering both a `LlamaCppConnector` for inference and a `LlamaCppManager` for lifecycle management of llama-server instances. Managed deployments target Kubernetes DaemonSets on GPU-labelled nodes (one DaemonSet per model); remote-connect mode registers an external llama-server endpoint without any lifecycle management.

This is a v0.1.0 feature shipping on the `v0.1.x` branch.

---

## Why Not Ollama?

Ollama wraps llama.cpp and adds a management daemon layer. That layer introduces latency and reduces GPU utilisation. Running llama-server directly eliminates the intermediary, giving lower inference latency and better GPU throughput — important for production deployments where cost-per-token matters.

---

## Architecture

### New files

| File | Purpose |
|------|---------|
| `services/management/app/services/llamacpp_manager.py` | K8s DaemonSet lifecycle + remote-connect registration |
| `services/management/app/api/v1/llamacpp.py` | REST routes for deployment CRUD + lifecycle |
| `tests/unit/management/test_llamacpp_routes.py` | Route unit tests |
| `tests/unit/management/test_llamacpp_manager.py` | Manager unit tests |
| `tests/integration/test_llamacpp_integration.py` | Integration tests (skipped without live server) |

### Modified files

| File | Change |
|------|--------|
| `shared/utils/llm_connectors.py` | Add `LlamaCppConnector`; update `_load_connectors()` |
| `services/management/app/services/providers/__init__.py` | Add `LLAMACPP` to `ProviderType`; add `LlamaCppConfig` and default models |
| `services/management/app/models_sqlalchemy.py` | Add `llamacpp_deployments` table |
| `services/management/app/extensions.py` | Add pricing config stubs for llama.cpp models |
| `services/management/app/api/v1/__init__.py` | Register llamacpp blueprint |
| `docs/TESTING_SETUP.md` | llama.cpp local testing section |
| `docs/APP_STANDARDS.md` | llama.cpp provider section, K8s node labelling conventions |

---

## Data Model

### `llamacpp_deployments` table

```
id                  INT PK
name                VARCHAR UNIQUE NOT NULL      -- human label
deployment_type     ENUM('kubernetes','remote')  -- lifecycle mode
status              ENUM('pending','deploying','running','stopped','error')
status_message      TEXT                         -- error detail

# Model
model_name          VARCHAR NOT NULL             -- e.g. "llama-3.2-3b-instruct"
model_url           VARCHAR                      -- GGUF download URL (k8s mode)
model_filename      VARCHAR                      -- filename within volume

# Inference params
n_ctx               INT DEFAULT 4096
n_gpu_layers        INT DEFAULT -1               -- -1 = all layers on GPU
gpu_count           INT DEFAULT 1

# Connection
endpoint_url        VARCHAR                      -- resolved after deploy or set directly for remote

# Kubernetes
k8s_namespace       VARCHAR DEFAULT 'waddleai'
k8s_daemonset_name  VARCHAR
node_selector       TEXT (JSON)                  -- e.g. {"gpu-tier": "a100"}
node_affinity       TEXT (JSON)                  -- optional, advanced scheduling

# Metadata
created_on          DATETIME
modified_on         DATETIME
```

### Provider registration

On `status → running`, the manager inserts/updates a row in `ai_providers` (name=deployment.name, provider="llamacpp") and calls `LLMConnectionManager.reload()` (the existing `reload_connectors()` method) so the new connector is available immediately without a service restart. On removal the row is deleted and `reload()` is called again.

---

## `LlamaCppConnector`

Location: `shared/utils/llm_connectors.py`

```
class LlamaCppConnector(LLMConnector):
    endpoint_url: str       # e.g. http://llamacpp-llama3-svc.waddleai:8080
    model_name: str         # the GGUF model loaded on this server
    session: aiohttp.ClientSession

    chat_completion()   →  POST /v1/chat/completions
    count_tokens()      →  POST /tokenize  (exact); fallback to tiktoken on failure
    list_models()       →  GET  /v1/models (returns single loaded model)
    health_check()      →  GET  /health
```

**Token counting detail:** llama-server's `/tokenize` accepts `{"content": "<text>"}` and returns `{"tokens": [...]}`. `count_tokens()` returns `len(tokens)`. If the endpoint returns non-200 or times out, logs a warning and falls back to the tiktoken `cl100k_base` estimate — inference is never blocked.

**No API key required** by default. Optional `api_key` field in config for deployments fronted by an auth proxy.

---

## `LlamaCppManager`

Location: `services/management/app/services/llamacpp_manager.py`

Uses the `kubernetes` Python client (already in `requirements-dev.txt`).

### Kubernetes mode

**`deploy_daemonset(deployment)`**
1. Builds DaemonSet manifest (see K8s Manifest section below)
2. Builds ClusterIP Service manifest (`<name>-svc`, port 8080)
3. `AppsV1Api.create_namespaced_daemon_set()`
4. `CoreV1Api.create_namespaced_service()`
5. Sets `endpoint_url = http://<name>-svc.<namespace>:8080`
6. Sets `status = deploying`
7. Spawns background poll — when `ready_replicas >= 1`: `status = running`, registers `ai_providers` row

**`remove_daemonset(deployment, force=False)`**
- If `status == running` and not `force`: raises `ConflictError`
- Deletes DaemonSet + Service
- Deletes `ai_providers` row
- Sets `status = stopped`

**`get_daemonset_status(deployment)`**
- Reads DaemonSet `.status.ready_replicas` and pod events
- Surfaces `CrashLoopBackOff` pod events as `status_message`

**`export_k8s_manifest(deployment) → str`**
- Returns YAML string (DaemonSet + Service) — for teams managing K8s themselves

### Remote mode

**`register_remote(deployment)`**
1. Calls `LlamaCppConnector.health_check()` on `endpoint_url`
2. If healthy: inserts `ai_providers` row, sets `status = running`
3. If unhealthy: returns 400, no DB write

---

## K8s DaemonSet Manifest

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: <k8s_daemonset_name>
  namespace: <k8s_namespace>
spec:
  selector:
    matchLabels:
      app: <k8s_daemonset_name>
  template:
    metadata:
      labels:
        app: <k8s_daemonset_name>
    spec:
      nodeSelector: <node_selector>          # from deployment config
      initContainers:
        - name: download-model
          image: curlimages/curl:latest
          command: ["curl", "-L", "-o", "/models/<model_filename>", "<model_url>"]
          volumeMounts:
            - name: model-storage
              mountPath: /models
      containers:
        - name: llama-server
          image: ghcr.io/ggerganov/llama.cpp:server
          args:
            - --model /models/<model_filename>
            - --n-gpu-layers <n_gpu_layers>
            - --ctx-size <n_ctx>
            - --port 8080
            - --host 0.0.0.0
          ports:
            - containerPort: 8080
          resources:
            limits:
              nvidia.com/gpu: "<gpu_count>"
          volumeMounts:
            - name: model-storage
              mountPath: /models
      volumes:
        - name: model-storage
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: <k8s_daemonset_name>-svc
  namespace: <k8s_namespace>
spec:
  selector:
    app: <k8s_daemonset_name>
  ports:
    - port: 8080
      targetPort: 8080
```

**Node labelling convention** (documented in `docs/APP_STANDARDS.md`):
```
kubectl label node <node> waddleai/gpu-tier=a100
kubectl label node <node> waddleai/gpu-tier=h100
```
`node_selector` example: `{"waddleai/gpu-tier": "a100"}`

---

## Management API Routes

Base: `/api/v1/llamacpp` — all routes require `ADMIN` role.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/deployments` | List all deployments |
| POST | `/deployments` | Create deployment (status=pending) |
| GET | `/deployments/{id}` | Get single deployment |
| PATCH | `/deployments/{id}` | Update config (stopped deployments only) |
| DELETE | `/deployments/{id}` | Delete record (must be stopped unless `?force=true`) |
| POST | `/deployments/{id}/deploy` | Deploy to K8s or register remote |
| POST | `/deployments/{id}/remove` | Remove from K8s / unregister remote |
| GET | `/deployments/{id}/health` | Live health check against llama-server |
| GET | `/deployments/{id}/export/k8s` | Return DaemonSet+Service YAML |

---

## Provider Config

### `ProviderType` addition
```python
LLAMACPP = "llamacpp"
```

### `LlamaCppConfig`
```python
@dataclass
class LlamaCppConfig(ProviderConfig):
    deployment_id: Optional[int] = None   # links to llamacpp_deployments
    model_name: str = ""

    def __post_init__(self):
        self.provider_type = ProviderType.LLAMACPP
```

### Default models (popular GGUFs)
```python
ProviderType.LLAMACPP: [
    "llama-3.2-3b-instruct",
    "llama-3.1-8b-instruct",
    "llama-3.1-70b-instruct",
    "mistral-7b-instruct",
    "mixtral-8x7b-instruct",
    "codellama-13b-instruct",
    "phi-3.5-mini-instruct",
    "qwen2.5-7b-instruct",
]
```

---

## Error Handling

| Scenario | Behaviour |
|----------|-----------|
| llama-server unreachable | `health_check()` returns `unhealthy`; `chat_completion()` raises; router skips to next connector |
| `/tokenize` non-200 or timeout | Log warning; fall back to tiktoken estimate; request continues |
| K8s API unreachable during deploy | `deploy_daemonset()` raises; route returns 503; status stays `pending` |
| DaemonSet no ready replicas after 10 min | Status set to `error`; `status_message` contains last pod event |
| initContainer GGUF download failure | Pods enter `CrashLoopBackOff`; surfaced via pod event polling |
| Remote health check fails on register | Return 400; no DB write |
| DELETE on running deployment | Return 409 unless `?force=true` |
| PATCH on running deployment | Return 409 — must stop first |

---

## Testing

### Unit tests

**`tests/unit/test_llm_connectors.py`** — `TestLlamaCppConnector`:
- `test_chat_completion_success`
- `test_chat_completion_server_error`
- `test_count_tokens_exact_via_tokenize`
- `test_count_tokens_fallback_to_tiktoken_on_failure`
- `test_list_models_returns_loaded_model`
- `test_health_check_healthy`
- `test_health_check_unhealthy`

**`tests/unit/management/test_llamacpp_routes.py`**:
- CRUD happy paths and 404s
- Deploy/remove lifecycle transitions
- Admin-only auth guards
- Force-delete guard (409 without `?force=true`)
- K8s manifest export returns valid YAML

**`tests/unit/management/test_llamacpp_manager.py`**:
- DaemonSet manifest generation (nodeSelector, GPU limits, initContainer args)
- Service manifest generation
- Remote registration — healthy endpoint accepted
- Remote registration — unhealthy endpoint rejected
- K8s API error surfaces as 503
- Status polling surfaces CrashLoopBackOff

### Integration tests

**`tests/integration/test_llamacpp_integration.py`** (skipped without `LLAMACPP_ENDPOINT` env var):
- `test_llamacpp_connector_importable` — always runs
- `test_llamacpp_health_check` — skipped
- `test_llamacpp_chat_completion` — skipped
- `test_llamacpp_tokenize_endpoint` — skipped
- `test_llamacpp_list_models` — skipped

---

## Docs Updates

- **`docs/APP_STANDARDS.md`** — llama.cpp provider section: deployment model, K8s node labelling conventions (`waddleai/gpu-tier`), GGUF sourcing guidance (HuggingFace URLs)
- **`docs/TESTING_SETUP.md`** — how to run a local llama-server for integration tests:
  ```bash
  docker run -p 8080:8080 ghcr.io/ggerganov/llama.cpp:server \
    -m /path/to/model.gguf --port 8080 --host 0.0.0.0
  export LLAMACPP_ENDPOINT=http://localhost:8080
  pytest tests/integration/test_llamacpp_integration.py
  ```
- **`shared/utils/llm_connectors.py`** module docstring — add llama.cpp to provider list

---

## Out of Scope

- Streaming responses (can be added in a follow-on; llama-server supports SSE)
- Multi-model-per-server (not supported by llama-server; use multiple deployments)
- Automatic GGUF quantisation selection
- GPU node auto-provisioning
