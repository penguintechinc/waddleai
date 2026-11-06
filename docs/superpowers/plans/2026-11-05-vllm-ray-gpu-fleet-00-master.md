# vLLM on Ray GPU Fleet — Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy and operate open-weight models on heterogeneous (NVIDIA/AMD/Intel, mixed-class) Kubernetes GPU nodes via vLLM on Ray, driven from a guided admin UI, routable through the proxy, metered against team/org token budgets.

**Architecture:** A `vllm_ray` fleet backend in `shared/fleet/` drives one KubeRay `RayCluster` (one worker group per GPU vendor+class) through the Ray Serve REST API; GPU inventory, a fit calculator and class-aware placement live in pure Python; the proxy gains an OpenAI-compatible `vllm` connector that reports real `usage` counts into the existing quota engine at a per-deployment virtual price; a React "GPU Fleet" section exposes it.

**Tech Stack:** Python 3.13 + Quart, SQLAlchemy/Alembic, httpx, Ray Serve LLM + vLLM (pinned), KubeRay Helm subchart, Cilium, React 18 + Vite + TS + TanStack Query, Playwright, Rust CLI (clap).

**Spec:** `docs/superpowers/specs/2026-11-05-vllm-ray-gpu-fleet-design.md`

## Sub-plans (execute in this order; A ∥ B)

| # | Plan file | Subsystem | Depends on |
|---|---|---|---|
| A | `...-A1-helm.md`, `...-A1-helm-part2.md`, `...-A2-storage-nvidia.md`, `...-A3-netsec-images.md` | Helm (KubeRay subchart, RayCluster, device-plugin class map, model store, PSA/hostPath), Cilium, Tetragon, 3 images + CI | — |
| B | `...-B-fleet-backend.md` (index) + `-part1..6.md` | enums, migration, ORM, inventory parsers, fit calculator, serve config, Ray client, `VllmRayBackend`, placement/caps changes, conformance | — |
| C | `...-C1-api.md`, `...-C1-api-part2.md`, `...-C1-api-part3.md`, `...-C2-proxy-chargeback.md`, `...-C2-proxy-chargeback-part2.md` | management API + OpenAPI, proxy `vllm` connector, local-provider consolidation, chargeback/quota, routing helper, metrics | B |
| D | `...-D1-webui.md`, `...-D1-webui-part2.md`, `...-D1-webui-part3.md`, `...-D2-e2e-cli-docs.md`, `...-D2-e2e-cli-docs-part2.md` | webui GPU Fleet section, Playwright e2e + mock seeder, CLI, admin docs, screenshots | C |
| F | `...-F-llamacpp-rpc.md`, `...-F3-llamacpp-rpc-manifests.md`, `...-F5-llamacpp-rpc-api.md`, `...-F2-llamacpp-rpc.md` | llama.cpp multi-node via RPC inside the existing llamacpp fleet backend: rpc-server worker set + head, per-deployment CNP, images, fit/TCO/chargeback reuse, wizard "Engine" choice; Ollama multi-node only if natively supported | B, C (shared modules) |
| E | §E below | Real-path validation on dal2beta (adds E10: llama.cpp RPC 2-node deployment serves a completion; E11: worker reachable only from its head) | A, B, C, F |

(All files share the prefix `2026-11-05-vllm-ray-gpu-fleet-`; split to respect the 25,000-char file cap.)

**Pre-condition before any sub-plan branches:** the release/v0.4.X working tree holds ~60 uncommitted wave-5 files (incl. alembic 027). Those must be committed/merged first; B2's migration 028 revises 027.

Each sub-plan produces working, tested software on its own branch
(`feature/vllm-ray-<letter>`) off `release/v0.4.X`, PR'd into the release
branch when green. Branches B→C→D stack; A is independent.

## Global Constraints (apply to every task in every sub-plan)

- Python 3.13, Quart, `@dataclass(slots=True)`, penguin logging only (no `print`/`logging.basicConfig`); OTel logs+metrics+traces for every new code path.
- Every new feature path gated on BOTH PostHog flags: existing `waddleai.fleet_v2` and new `waddleai.fleet-vllm-ray` (default OFF; unseen → OFF). Flag check helper: `shared.utils.feature_flags.is_feature_enabled(flag, distinct_id=str(org_id), default=False)`.
- API scope: `fleet:admin` (`Permission.FLEET_ADMIN`, `shared/auth/permissions.py:97`) via `require_scope`.
- Entity identity: numeric BIGINT PK internal, `public_id` UUID external; int enums for vendor/quantization/status/state; no string refs.
- All deps/images immutably pinned (exact version + SHA256 / digest). No PRC-origin deps or models (placement deny-list already applies).
- Rootless containers, PSA `restricted`; the one `hostPath` exception is opt-in and tagged `# STATEFUL EXCEPTION (approved)`; PSA restricted; the hot-cache opt-in sets the namespace to enforce=baseline (or a Kyverno PolicyException), spec §6.4.
- Tokens/secrets from env/Secrets only; never in args/logs; HF tokens via org `credentials_ref`.
- Response DTOs via `quart-schema` `@validate_response`; `openapi/v1.yaml` updated; API tests cover filters, empty results, invalid filters, pagination.
- Coverage ≥90% on new code; every gate must be able to fail; `set -euo pipefail` in scripts.
- Commit message prefix per branch type; never commit to `release/v0.4.X` directly; work in `.worktrees/`.
- Units: VRAM in **MB** on `NodeInfo`/inventory, **GB** in `model_registry.min_vram` (existing contract, `shared/fleet/caps.py:76`).
- Copy for admins: every refusal states what to change in plain language; no bare "TP/PP" without the explanation string defined in Plan B's fit module.

## Cross-plan Interfaces (locked here; sub-plans must use these names exactly)

```python
# shared/fleet/enums.py  (Plan B)
class GpuVendor(IntEnum): NVIDIA=1; AMD=2; INTEL=3
class Quantization(IntEnum): NONE=0; AWQ=1; GPTQ=2; FP8=3; BNB=4
class DeploymentStatus(IntEnum): PENDING=1; PULLING=2; STARTING=3; WARMING=4; SERVING=5; DEGRADED=6; STOPPED=7; FAILED=8
class InventoryState(IntEnum): WARM=1; COLD=2; UNCLASSIFIED=3; MIXED_UNSUPPORTED=4
class LayoutStrategy(IntEnum): STRICT_PACK=1; STRICT_SPREAD=2

# shared/fleet/base.py additions (Plan B)
NodeInfo(..., gpu_vendor: int | None = None, gpu_class: str | None = None)  # appended, defaulted
BackendType.VLLM_RAY = "vllm_ray"

# shared/fleet/vllm_ray/inventory.py (Plan B)
@dataclass(slots=True) class GpuRecord: node_uid:str; node_name:str; gpu_index:int; vendor:int; gpu_class:str|None; product_name:str; vram_total_mb:int; vram_used_mb:int; pci_bus_id:str; schedulable:bool; state:int
def parse_nvidia_smi(csv_text:str, class_map:dict[str,str]) -> list[GpuRecord]
def parse_rocm_smi(json_text:str) -> list[GpuRecord]
def parse_xpu_smi(json_text:str) -> list[GpuRecord]
def normalize_class(vendor:int, product_name:str, class_map:dict[str,str]) -> str|None   # e.g. "nvidia-rtx4090"

# shared/fleet/vllm_ray/fit.py (Plan B)
@dataclass(slots=True) class Pool: vendor:int; gpu_class:str; nodes:list[tuple[str,int,int]]  # (node_uid, gpu_count, vram_free_mb per gpu)
@dataclass(slots=True) class Layout: vendor:int; gpu_class:str; replicas:int; tensor_parallel:int; pipeline_parallel:int; strategy:int; nodes_used:int; vram_needed_mb:int; vram_available_mb:int; recommended:bool; explanation:str
@dataclass(slots=True) class FitResult: fits:bool; layouts:list[Layout]; refusal:str|None
def estimate_weight_bytes(params_b:float, quantization:int, dtype:str) -> int
def fit_check(params_b:float, quantization:int, dtype:str, max_model_len:int, pools:list[Pool], allow_mixed_class:bool, kv_headroom_pct:int=20) -> FitResult
QUANT_MATRIX: dict[int, set[int]]  # vendor -> allowed Quantization values (NVIDIA: NONE,AWQ,GPTQ,BNB,FP8[ada+]; AMD: NONE,AWQ,GPTQ,FP8[mi300]; INTEL: NONE)

# shared/fleet/vllm_ray/serve_config.py (Plan B)
def build_serve_app(dep: "VllmDeployment", image_model_path:str, nccl_env:dict[str,str]) -> dict   # Ray Serve multi-app entry; route_prefix=f"/d/{dep.slug}"
def placement_group_spec(tp:int, pp:int, gpu_class:str, strategy:int) -> dict  # {"bundles":[...], "strategy":"STRICT_PACK"|"STRICT_SPREAD"}

# shared/fleet/vllm_ray/ray_client.py (Plan B)
class RayServeClient(Protocol):
    async def put_applications(self, config:dict) -> None
    async def get_application(self, name:str) -> dict   # {"status": "RUNNING"|"DEPLOYING"|"DEPLOY_FAILED"|..., "message": str}
    async def delete_application(self, name:str) -> None
    async def run_inventory(self) -> list[dict]          # fan-out task results, raw per-worker payloads
class HttpRayServeClient(RayServeClient): def __init__(self, head_url:str, token:str|None, timeout_s:float=10.0)

# shared/fleet/vllm_ray/backend.py (Plan B)
@register(BackendType.VLLM_RAY)
class VllmRayBackend(InferenceFleetBackend):
    def __init__(self, db, config:dict, credentials:dict|None, client:RayServeClient|None=None)
    # six ABC methods + :
    async def deploy(self, dep:"VllmDeployment") -> None
    async def scale(self, dep, replicas_min:int, replicas_max:int) -> None
    async def stop(self, dep) -> None
    async def delete(self, dep) -> None
    async def reconcile(self, dep) -> int   # returns new DeploymentStatus, appends vllm_deployment_events row on change
    async def collect_inventory(self) -> list[GpuRecord]   # upserts gpu_inventory

# ORM (Plan B, services/management/app/models_sqlalchemy.py)
class GpuInventory(Base): __tablename__="gpu_inventory"
class VllmDeployment(Base): __tablename__="vllm_deployments"   # fields per spec §6.1 + slug (String(64), unique per org, derived from name)
class VllmDeploymentEvent(Base): __tablename__="vllm_deployment_events"
TokenUsage += local_deployment_kind (INT NULL, LocalDeploymentKind), local_deployment_id (BIGINT NULL, no FK; kind-discriminated; AM10 replaces fleet_deployment_id), unit_price_input, unit_price_output (NUMERIC(12,6), nullable)

# Plan C API blueprint: services/management/app/api/v1/fleet_vllm.py, url_prefix "/api/v1/fleet"
#   additional endpoints beyond spec §5.2 (needed by the UI, Plan D):
#   GET  /vllm/flags            -> {"fleet_v2": bool, "fleet_vllm_ray": bool}  (webui has no PostHog client; server resolves flags)
#   GET  /vllm/models?q=&limit= -> model_registry rows for the org (public_id, name, params_b|null, origin, min_vram_gb) — no registry list endpoint exists today
#   POST /vllm/fit-check response also carries allowed_quantizations: list[int] for the chosen vendor (from QUANT_MATRIX)
#   POST /vllm/deployments body also accepts expose_via_proxy: bool = true, prefer_local: {fallback_model: str, match: dict} | null
# Plan D webui facts: services/webui is JSX (no tsconfig) → JSDoc typedefs; @tanstack/react-query installed but no QueryClientProvider yet (D1 adds one at app root); no existing fleet page; CLI binary is `waddleai`; `make seed-mock-data` is a stub (Makefile:190) → D6 implements it
# Amendments AM1–AM27 (authoritative over conflicting sub-plan text): see 2026-11-05-vllm-ray-gpu-fleet-01-amendments.md (AM1–AM13) and 2026-11-05-vllm-ray-gpu-fleet-01b-amendments.md (AM14–AM27)
# Docs cap: every markdown/code file ≤ 25,000 chars (docs/CLAUDE.md:161) → sub-plans split into -1/-2 files when larger
# Plan C connector: shared/utils/llm_connectors.py  class VllmConnector(OpenAIConnector)  provider key "vllm"
# Plan C providers constant: shared/routing/providers.py  LOCAL_PROVIDERS: frozenset = {"ollama","llamacpp","vllm"}
# Plan C routing helper: shared/routing/prefer_local.py  def create_prefer_local_rule(db, org_id:int, deployment:VllmDeployment, fallback_model:str, match:dict) -> int
# Plan C metrics names: waddleai_gpu_vram_total_mb, waddleai_gpu_vram_used_mb {vendor,gpu_class,node}; waddleai_vllm_deployments {status}; waddleai_vllm_placement_decisions_total {outcome}; waddleai_vllm_model_load_seconds (histogram)
# Helm values root (Plan A): fleet.vllmRay.* exactly per spec §6.2
```

## Review Focus (spec-implied inputs no single task's tests would otherwise exercise; each pinned to a task in the named sub-plan)

1. A NVIDIA GPU whose product name matches no `nvidiaClassMap` pattern → inventory `UNCLASSIFIED`, `schedulable=false`, fit calculator excludes it, UI shows it. (B: inventory + fit tests)
2. Ray Serve reports `DEPLOY_FAILED` after `SERVING` (e.g. OOM on a long prompt) → status `DEGRADED` with the Serve message, `connection_links` row kept but health marks the endpoint unhealthy so routing falls back to commercial. (B: reconcile test; C: offers annotation test)
3. Streaming response with no final `usage` chunk (older vLLM or client-set `stream_options`) → connector records the request as `estimated=false` is NOT allowed; it must fail loud: usage row written with `estimated=true` AND a `waddleai_vllm_usage_missing_total` counter increments, never silently zero. (C: connector test)
4. Admin deletes a deployment while requests are in flight → Serve app removed, `connection_links` row deleted, in-flight requests get the proxy's existing upstream-unavailable error, no orphaned offers. (B: delete test; C: offers test)
5. Two deployments with the same name in different orgs, and the same `served_model_name` as a commercial model → unique per org only; proxy offers distinguish by destination, `location=local` wins only via routing rule, never implicitly. (C: API uniqueness test + offers test)

## §E Real-path validation (dal2beta; after A+B+C merge, before release)

- [ ] E1 Deploy chart with `fleet.vllmRay.enabled=true` on dal2beta (3 AI nodes, 2 storage; head nodeSelector override to a storage node until compute nodes land ~2026-11-13). Record `kubectl get raycluster -n waddleai` READY and worker-group pod counts per class.
- [ ] E2 Inventory: `GET /api/v1/fleet/gpu-inventory` lists every physical GPU across the 3 AI nodes with vendor, class, VRAM; count equals `nvidia-smi -L | wc -l` summed over nodes (report both numbers).
- [ ] E3 Single-node TP: deploy a 12B-class model (e.g. Devstral-Small AWQ) `tp=2`; reach `SERVING`; one completion through the proxy with an API key; `token_usage` row has `local_deployment_kind` = vLLM and `local_deployment_id` set, `estimated=false`, non-zero input/output.
- [ ] E4 Multi-node PP: deploy a 4-bit ~120B-class checkpoint `pp=2` (or `pp=3`) `STRICT_SPREAD`; confirm each stage's actor on a distinct node (`ray list actors`), completion succeeds; record tokens/s.
- [ ] E5 Mixed-class refusal: on the node with two different NVIDIA cards, request a `tp=2` layout spanning them with `allow_mixed_class=false` → fit-check refusal text; with `allow_mixed_class=true` and `pp=2` → accepted.
- [ ] E6 Quota: set a team budget below the cost of a long completion at the deployment's virtual price → request blocked with the existing quota error; raise budget → succeeds; ledger shows the debit at the virtual price. Repeat with an Ollama deployment; its quota debit appears in the ledger too.
- [ ] E7 Failover: scale deployment to 0 → the prefer-local rule's requests land on the commercial fallback (verify provider in usage log).
- [ ] E8 Telemetry: OTel sink receives ≥1 log, ≥1 metric point for `waddleai_gpu_vram_used_mb`, ≥1 histogram point for `waddleai_vllm_model_load_seconds`, ≥1 span across management→Ray REST; print counts.
- [ ] E9 Screenshots for `docs/screenshots/` captured on dal2beta with the real inventory.
- [ ] E10 llama.cpp RPC: 2-node RPC deployment (Plan F) reaches `SERVING`; one completion succeeds through the proxy.
- [ ] E11 RPC isolation: a connection to an rpc-server port from any pod other than its head is refused.
- [ ] E12 Auth: unauthenticated request to the vLLM and llama-server endpoints → 401.

All counts go into the release PR description; zero examined = FAIL.
