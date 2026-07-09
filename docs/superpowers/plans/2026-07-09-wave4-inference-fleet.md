# Hardened Inference Fleet & Deployment Targets — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task ends in a real `git commit` (Co-Authored-By trailer + flag-off proof).

**Branch:** `feature/inference-fleet-v2` (off `release/v0.2.X`). **Depends on:** `feature/routing-engine` (§7 / migration 009 — the routing engine consults `endpoints_for(model)` for placement-aware dispatch; `model_registry` from migration 007 and `shared/licensing/features.py` from §5 must already be on the branch). Migrations 006–011 assumed landed; this branch adds **012**.

**Spec:** `docs/superpowers/specs/2026-07-09-waddleai-platform-spec.md` §10 (with §10.1 fleet backend interface, §10.2 hardened Ollama image, §10.3 access control, §10.4 placement/LB/caps, §10.5 acceptance), §2.4 (Free caps + Pro dual-metering), §2.2 (origin deny-list), §13.1 (migration 012), §14.5 (flag `waddleai.fleet_v2`), §14.6 (license entitlement/metering), Q#4/Q#7/Q#9 in the resolved ledger. Authoritative.

---

**Goal:** A pluggable `InferenceFleetBackend` interface behind which five backend types live — **Ollama** (primary, DaemonSet + pool mode), **llama.cpp** (DaemonSet/remote), **EXO** (external-only, GPLv3 network boundary), **Vertex AI** and **Bedrock** (Professional-gated cloud, with mandatory idle-teardown). The two existing managers are **restructured, not rewritten**, to implement it. A `fleet_backends` registry table carries `type` + `management_scope` (`register_and_route` | `full_lifecycle`). Placement is placement-aware: the §7 router consults `endpoints_for(model)` with session affinity, hot-model pinning, lazy pull, and origin deny-list; Free-tier caps (≤5 nodes / ≤3 models, utility models excluded) are enforced via the license client at `provision`/`place_model`/registry-registration. A hardened, digest-pinned, non-root, read-only-rootfs `ghcr.io/penguintechinc/waddleai/ollama` image (hardened + debug tags, Trivy-gated) replaces the upstream image in the DaemonSet and GPU subcharts. The AIProxy is the **only** authenticated path to inference: ClusterIP + CiliumNetworkPolicy in-cluster (cross-ref Cilium branch §12); mTLS (cert-manager) in beta/prod and shared-token sidecar in alpha for external nodes.

**Architecture:** `shared/fleet/base.py` defines the ABC (`provision` / `deprovision` / `health` / `list_nodes` / `place_model` / `endpoints_for`) plus `@dataclass(slots=True)` value types (`ProvisionSpec`, `NodeInfo`, `ModelPlacement`, `FleetHealth`, `Endpoint`). A `shared/fleet/registry.py` factory maps `type` → backend class and resolves `config`/`credentials_ref` (provider-credential pattern, Fernet at rest). Caps, node-metering, idle-teardown, and origin-deny are cross-cutting helpers (`caps.py`, `idle_teardown.py`, `placement.py`) shared by every backend so no backend hand-rolls them. Cloud endpoints count as managed nodes for Pro metering (`keepalive({"nodes": M})`, §14.6). Everything new sits behind PostHog flag `waddleai.fleet_v2` (default OFF), evaluated via `shared/licensing/features.py::features.enabled("fleet_v2", distinct_id=str(org_id))` with fail-safe OFF; `vertex_ai`/`bedrock` creation additionally requires `LicenseClient.check_feature("hybrid_targets")`. Flag OFF ⇒ the legacy single-backend Ollama/llama.cpp deployment API stays byte-compatible; the multi-backend registry, cloud backends, caps, and placement path no-op.

**Tech Stack:** Python 3.13, Quart + hypercorn, penguin-dal (runtime) / SQLAlchemy + Alembic (schema), penguin-aaa (auth), penguin-licensing (`LicenseClient`), Valkey 8 (session affinity + node/model cap counters), `google-cloud-aiplatform` (Apache-2.0, Vertex), `boto3` (Apache-2.0, Bedrock), `kubernetes` client, aiohttp (EXO/health), pytest + pytest-asyncio, fakeredis + `unittest.mock` (unit), kind (Ollama/llama.cpp real, cloud/EXO mocked). Hardened image via moby-expert; Helm/CNP via k8s-manifest-builder.

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `shared/fleet/__init__.py` | Package exports |
| Create | `shared/fleet/base.py` | `InferenceFleetBackend` ABC + `ProvisionSpec`/`NodeInfo`/`ModelPlacement`/`FleetHealth`/`Endpoint` dataclasses + `BackendType`/`ManagementScope` enums |
| Create | `tests/unit/fleet/test_fleet_base.py` | ABC contract + dataclass tests |
| Create | `shared/fleet/registry.py` | `type`→class factory; `config`/`credentials_ref` resolution; `management_scope` wiring |
| Create | `tests/unit/fleet/test_fleet_registry.py` | Factory + credential-resolution tests |
| Create | `services/management/alembic/versions/012_fleet.py` | Migration 012 |
| Create | `tests/unit/management/test_migration_012.py` | Round-trip + downgrade on seeded snapshot |
| Modify | `services/management/app/models_sqlalchemy.py` | `FleetBackend` model; `management_scope` + interface cols on `ollama_deployments`/`llamacpp_deployments` |
| Modify | `services/management/app/services/ollama_manager.py` | `OllamaDeploymentManager(InferenceFleetBackend)` — restructure |
| Create | `tests/conformance/test_fleet_conformance.py` | Parametrized 5-backend interface conformance suite |
| Modify | `services/management/app/services/llamacpp_manager.py` | `LlamaCppManager(InferenceFleetBackend)` — restructure |
| Create | `shared/fleet/caps.py` | Free-tier node/model cap enforcement (license client) + managed-node metering aggregation |
| Create | `tests/unit/fleet/test_caps.py` | Cap enforcement + utility-exclusion + keepalive-nodes tests |
| Create | `shared/fleet/placement.py` | `endpoints_for` aggregation, session affinity (Valkey), hot-model pinning, lazy pull, origin deny-list |
| Create | `tests/unit/fleet/test_placement.py` | Affinity/pinning/lazy-pull/deny-list tests |
| Modify | `shared/utils/request_router.py` | Router consults `placement.endpoints_for(model)` for local-fleet dispatch (§7.5) |
| Create | `shared/fleet/exo.py` | `ExoFleetBackend` — external API-only plugin (GPLv3 network boundary, no EXO code in-repo) |
| Create | `tests/unit/fleet/test_exo.py` | EXO plugin tests (mocked HTTP) |
| Create | `shared/fleet/idle_teardown.py` | Idle-window→teardown→redeploy-on-demand controller (full_lifecycle, audit-logged) |
| Create | `tests/unit/fleet/test_idle_teardown.py` | Idle-teardown lifecycle + audit tests |
| Create | `shared/fleet/vertex_ai.py` | `VertexAIFleetBackend` (`google-cloud-aiplatform`) |
| Create | `tests/unit/fleet/test_vertex_ai.py` | Vertex backend tests (mocked SDK) |
| Create | `shared/fleet/bedrock.py` | `BedrockFleetBackend` (`boto3`) |
| Create | `tests/unit/fleet/test_bedrock.py` | Bedrock backend tests (mocked boto3) |
| Create | `services/management/app/api/v1/fleet.py` | `fleet_backends` CRUD; Pro gating on `vertex_ai`/`bedrock`; `management_scope` selection |
| Modify | `services/management/app/api/v1/__init__.py` | Register `fleet` blueprint |
| Create | `tests/unit/management/test_fleet_api.py` | Fleet-backend CRUD + Pro-gating + flag-off tests |
| Create | `images/ollama/Dockerfile` | Hardened Ollama image (hardened + debug tags) |
| Create | `images/ollama/README.md` | Build/tag/digest procedure |
| Modify | `.github/workflows/build-images.yml` | Build + Trivy-gate the two tags; publish digest |
| Modify | `k8s/helm/waddleai/templates/ollama-daemonset.yaml` | Consume hardened image; readOnlyRootFS + writable model/tmp mounts; pool (Deployment+nodeSelector) mode |
| Create | `k8s/helm/waddleai/templates/ollama-networkpolicy.yaml` | CiliumNetworkPolicy — ClusterIP fleet admits ingress only from AIProxy (§10.3, cross-ref §12) |
| Create | `k8s/helm/waddleai/templates/fleet-external-mtls.yaml` | cert-manager Certificate + shared-token sidecar (external nodes, Q#4) |
| Modify | `k8s/helm/waddleai/values.yaml` + `values-alpha/beta/prod.yaml` | Hardened image digest; `ollama.mode`; external-node auth (token alpha / mTLS beta+prod) |
| Modify | `k8s/helm/waddleai/charts/amd-rocm-plugin/*`, `charts/intel-gpu-plugin/*` (+ nvidia gpu-operator values) | Consume hardened image |
| Create | `tests/integration/test_fleet_acceptance.py` | §10.5 acceptance (kind CNP, hardened-image assertions, mTLS handshake, idle lifecycle, caps, Pro gating) |

---

### Task 1: `InferenceFleetBackend` ABC + value types (`shared/fleet/base.py`)

The pluggable interface all five backends implement (§10.1). Abstract methods `provision(spec)`, `deprovision(node_or_backend)`, `health()`, `list_nodes()`, `place_model(model, constraints)`, `endpoints_for(model)`. Value types are `@dataclass(slots=True)` per house rule. No behavior yet — pure contract. Flag `waddleai.fleet_v2` is read by callers, not the ABC.

**Files:** Create `shared/fleet/__init__.py`, `shared/fleet/base.py`, `tests/unit/fleet/test_fleet_base.py`.

- [ ] **Step 1: Write failing tests** — `tests/unit/fleet/test_fleet_base.py`: (a) `InferenceFleetBackend` is `abc.ABC` and cannot be instantiated; (b) a minimal in-test subclass that implements all six methods instantiates; (c) omitting any one method raises `TypeError` at instantiation; (d) `BackendType` enum has exactly `ollama|llamacpp|exo|vertex_ai|bedrock`; `ManagementScope` has `register_and_route|full_lifecycle`; (e) dataclasses `ProvisionSpec`/`NodeInfo`/`ModelPlacement`/`FleetHealth`/`Endpoint` are `slots=True` (assert `__slots__`, no `__dict__`) and round-trip their fields.

- [ ] **Step 2: Run tests, verify they fail** — `python3 -m pytest tests/unit/fleet/test_fleet_base.py -v --no-cov` → `ModuleNotFoundError: shared.fleet.base`.

- [ ] **Step 3: Implement** —
  ```python
  class BackendType(str, Enum): OLLAMA="ollama"; LLAMACPP="llamacpp"; EXO="exo"; VERTEX_AI="vertex_ai"; BEDROCK="bedrock"
  class ManagementScope(str, Enum): REGISTER_AND_ROUTE="register_and_route"; FULL_LIFECYCLE="full_lifecycle"

  @dataclass(slots=True)
  class Endpoint: url: str; node_id: str; loaded_models: list[str]; healthy: bool
  @dataclass(slots=True)
  class NodeInfo: node_id: str; node_uid: Optional[str]; kind: str  # k8s|external|cloud
      loaded_models: list[str]; vram_total_mb: int; vram_free_mb: int; healthy: bool
  @dataclass(slots=True)
  class ModelPlacement: model: str; node_id: str; status: str  # placed|pulling|denied
  @dataclass(slots=True)
  class ProvisionSpec: name: str; models: list[str]; mode: str; constraints: dict
  @dataclass(slots=True)
  class FleetHealth: backend_id: int; healthy: bool; node_count: int; detail: dict

  class InferenceFleetBackend(abc.ABC):
      type: "BackendType"
      management_scope: "ManagementScope"
      @abstractmethod
      async def provision(self, spec: ProvisionSpec) -> list[NodeInfo]: ...
      @abstractmethod
      async def deprovision(self, node_id: str) -> None: ...
      @abstractmethod
      async def health(self) -> FleetHealth: ...
      @abstractmethod
      async def list_nodes(self) -> list[NodeInfo]: ...
      @abstractmethod
      async def place_model(self, model: str, constraints: dict) -> ModelPlacement: ...
      @abstractmethod
      async def endpoints_for(self, model: str) -> list[Endpoint]: ...
  ```

- [ ] **Step 4: Run tests, verify pass** — `python3 -m pytest tests/unit/fleet/test_fleet_base.py -v --no-cov` → green.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/fleet/__init__.py shared/fleet/base.py tests/unit/fleet/test_fleet_base.py
  git commit -m "feat(fleet): InferenceFleetBackend ABC + slots value types (flag: waddleai.fleet_v2)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 2: Migration 012 — `fleet_backends` + interface columns

Down-revision `011_knowledge` (§13.1). Adds `fleet_backends(id, org_id, type enum, mode, management_scope enum, config jsonb, credentials_ref, status)`; extends `ollama_deployments` + `llamacpp_deployments` with a nullable `fleet_backend_id` FK, `management_scope` (default `full_lifecycle`), and any interface-needed columns (`node_uid`, `pool_mode`). Round-trip + downgrade on a seeded snapshot (house rule).

**Files:** Create `services/management/alembic/versions/012_fleet.py`, `tests/unit/management/test_migration_012.py`. Modify `models_sqlalchemy.py` (`FleetBackend` class; new cols on both deployment models).

- [ ] **Step 1: Write failing round-trip test** — on a seeded SQLite snapshot with sample `ollama_deployments`/`llamacpp_deployments` rows: `upgrade` → `fleet_backends` exists with the two enums (Postgres native enum, SQLite check-constraint fallback); both deployment tables gain `fleet_backend_id`/`management_scope`; existing rows preserved with `management_scope='full_lifecycle'`; `downgrade` → schema returns to the 011 shape. Run → fails (no 012).

- [ ] **Step 2: Implement migration 012** — `op.create_table("fleet_backends", ...)`; guarded `op.add_column` on both deployment tables; enum creation portable across Postgres/SQLite. Complete `downgrade()`.

- [ ] **Step 3: Add ORM models** — `FleetBackend(Base)` with the columns above; add `fleet_backend_id`/`management_scope` to `OllamaDeployment` and `LlamaCppDeployment`.

- [ ] **Step 4: Run tests, verify pass** — `python3 -m pytest tests/unit/management/test_migration_012.py -v --no-cov`; `alembic -c services/management/alembic.ini heads` shows single head `012_...`.

- [ ] **Step 5: Commit**
  ```bash
  git add services/management/alembic/versions/012_fleet.py tests/unit/management/test_migration_012.py services/management/app/models_sqlalchemy.py
  git commit -m "feat(db): migration 012 — fleet_backends registry + management_scope on deployments" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 3: Backend registry/factory (`shared/fleet/registry.py`)

Maps `BackendType` → concrete class and constructs a live backend from a `fleet_backends` row: resolves `config` (jsonb) and `credentials_ref` via the provider-credential pattern (Fernet-decrypt, `shared.security.credential_encryption`), and injects the row's `management_scope`. Single construction chokepoint so callers never `if type == ...`.

**Files:** Create `shared/fleet/registry.py`, `tests/unit/fleet/test_fleet_registry.py`.

- [ ] **Step 1: Write failing tests** — (a) `build_backend(db, fleet_backend_row)` returns the right class per type (ollama/llamacpp real classes; exo/vertex/bedrock registered lazily so absence of cloud SDK doesn't break import); (b) `credentials_ref` is decrypted and passed, never logged (assert masked); (c) unknown type raises `ValueError`; (d) `management_scope` from the row lands on the instance.

- [ ] **Step 2: Run tests, verify they fail** — `ModuleNotFoundError: shared.fleet.registry`.

- [ ] **Step 3: Implement** — `_REGISTRY: dict[BackendType, Callable]`; `register(type)` decorator used by backend modules; `build_backend` looks up, resolves creds, constructs. Lazy-import cloud modules inside the factory to keep `google-cloud-aiplatform`/`boto3` optional at import time.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/fleet/registry.py tests/unit/fleet/test_fleet_registry.py
  git commit -m "feat(fleet): backend registry/factory with credential resolution" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 4: Refactor `OllamaDeploymentManager` → `InferenceFleetBackend`

**Restructure, not rewrite** (§10.1). The manager already has docker/kubernetes/kubernetes-daemonset/external modes and DaemonSet manifest generation — re-expose that behavior through the six ABC methods, keeping all existing manifest/health/pull logic and the legacy method names as thin internal helpers so the current `api/v1/ollama.py` routes stay byte-compatible.

**Files:** Modify `services/management/app/services/ollama_manager.py`. Create `tests/conformance/test_fleet_conformance.py` (parametrized skeleton; Ollama the first backend wired in — real in kind, unit path stubs the Docker/K8s client).

- [ ] **Step 1: Write failing conformance tests** — `tests/conformance/test_fleet_conformance.py` parametrized over a `BACKENDS` fixture list; for Ollama: `provision(spec)` returns `NodeInfo` list; `list_nodes()` reflects provisioned nodes with `node_uid` (K8s node UID) + `loaded_models` + `vram_free_mb`; `endpoints_for(model)` returns only nodes with that model loaded; `place_model` returns `ModelPlacement`; `health()` returns `FleetHealth`; `deprovision` removes the node. DaemonSet + pool (Deployment+nodeSelector) modes both covered.

- [ ] **Step 2: Run tests, verify they fail** — `AttributeError`/`TypeError` (manager not yet an `InferenceFleetBackend`).

- [ ] **Step 3: Implement** — declare `class OllamaDeploymentManager(InferenceFleetBackend)`; set `type = BackendType.OLLAMA`; add async ABC methods that delegate to existing `create_deployment`/`generate_k8s_manifest`/health/pull code; add node introspection (K8s node UID via the K8s client, VRAM headroom via the Ollama `/api/ps` + node labels); keep sync legacy methods intact. Add the pool (Deployment-with-nodeSelector) mode alongside DaemonSet for mixed-GPU clusters.

- [ ] **Step 4: Run tests, verify pass** — conformance (Ollama) green; `python3 -m pytest tests/unit -k ollama --no-cov --tb=short 2>&1 | tail -5` (no regression in existing manager tests).

- [ ] **Step 5: Commit**
  ```bash
  git add services/management/app/services/ollama_manager.py tests/conformance/test_fleet_conformance.py
  git commit -m "refactor(fleet): OllamaDeploymentManager implements InferenceFleetBackend (+pool mode)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 5: Refactor `LlamaCppManager` → `InferenceFleetBackend`

Same restructure for llama.cpp (kubernetes DaemonSet + remote modes already present). Exact token counts via the llama-server `/tokenize` endpoint surface through `NodeInfo`/placement metadata.

**Files:** Modify `services/management/app/services/llamacpp_manager.py`. Modify `tests/conformance/test_fleet_conformance.py` (add llama.cpp to `BACKENDS`).

- [ ] **Step 1: Write failing conformance tests** — parametrize llama.cpp through the same suite; assert `endpoints_for` maps a deployment's `model_name`→endpoint; `remote` mode registers an existing endpoint as an external `NodeInfo` (counted by registered endpoint per Q#7); `health()` uses the existing reachability check.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** — `class LlamaCppManager(InferenceFleetBackend)`, `type = BackendType.LLAMACPP`; async ABC methods delegating to `deploy_daemonset`/`remove_daemonset`/`register_remote`/health; expose `/tokenize`-backed token counts in placement metadata. Keep existing methods.

- [ ] **Step 4: Run tests, verify pass** — conformance (Ollama + llama.cpp) green; `-k llamacpp` no regression.

- [ ] **Step 5: Commit**
  ```bash
  git add services/management/app/services/llamacpp_manager.py tests/conformance/test_fleet_conformance.py
  git commit -m "refactor(fleet): LlamaCppManager implements InferenceFleetBackend" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 6: Free-tier caps + managed-node metering (`shared/fleet/caps.py`)

Enforce §2.4 Free/`community` caps at the two enforcement points (Q#7 semantics): **≤5 physical nodes** (distinct K8s node UID with ≥1 fleet pod; external nodes by registered endpoint) and **≤3 registered models** (registry entry with an active placement) — **utility models excluded** (`model_registry.is_utility=True`). Over-cap fails with the tier limit named. Also aggregate the managed-node count (K8s + external + cloud endpoints) for the §14.6 `keepalive({"nodes": M})` checkin.

**Files:** Create `shared/fleet/caps.py`, `tests/unit/fleet/test_caps.py`.

- [ ] **Step 1: Write failing tests** — (a) `enforce_node_cap(count)` raises `CapExceeded("Free tier limited to 5 inference nodes")` at the 6th distinct node under `community`; (b) `enforce_model_cap(model)` raises at the 4th non-utility model; (c) utility models (guard/routing/embedding, `is_utility=True`) never count toward the model cap; (d) `professional`/`enterprise` tiers are uncapped; (e) caps evaluated via `LicenseClient.validate()` cached tier, fail-safe to `community` on error; (f) `count_managed_nodes(backends)` sums distinct node UIDs across all backends incl. cloud endpoints; (g) flag `fleet_v2` OFF ⇒ enforcement is a no-op (legacy single-backend path unchanged).

- [ ] **Step 2: Run tests, verify they fail** — `ModuleNotFoundError: shared.fleet.caps`.

- [ ] **Step 3: Implement** — `CapEnforcer(db, license_client, features)`; distinct-node counting keyed on `NodeInfo.node_uid` (K8s) / registered endpoint (external/cloud); model counting joins `model_registry` and filters `is_utility`. `count_managed_nodes` for metering. All checks short-circuit-allow when `features.enabled("fleet_v2", ...)` is False.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/fleet/caps.py tests/unit/fleet/test_caps.py
  git commit -m "feat(fleet): Free-tier node/model caps (utility excluded) + managed-node metering" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 7: Placement, session affinity & origin deny-list (`shared/fleet/placement.py`) + router wiring

The §7 routing engine consults `endpoints_for(model)` and balances local-fleet targets with **session affinity** (KV-cache reuse, §6.3), hot-model pinning per node class, and lazy-pull of cold models. `place_model` validates the §2.2 origin deny-list and applies the Task 6 caps. Wires the router (§7.5) to this without changing commercial-provider dispatch.

**Files:** Create `shared/fleet/placement.py`, `tests/unit/fleet/test_placement.py`. Modify `shared/utils/request_router.py`.

- [ ] **Step 1: Write failing tests** — (a) `select_endpoint(model, session_id, endpoints)` returns the same node for a repeated `session_id` while that node is healthy (Valkey affinity, `waddleai:affinity:{session_id}` TTL); (b) affinity falls through to load-balanced choice when the pinned node is unhealthy; (c) hot models pinned to a node class are preferred; cold models trigger a `place_model(lazy=True)` pull; (d) `place_model` on a deny-listed origin (e.g. a Chinese-origin model) returns `ModelPlacement(status="denied")` and never dispatches; (e) the router, given a local-fleet route, calls `endpoints_for` and honors affinity; commercial routes are byte-identical to pre-change.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** — `PlacementEngine(valkey, registry, model_registry)` with `endpoints_for`, `select_endpoint` (affinity → hot-pin → load-balanced), and `place_model` (origin-deny check against `model_registry.origin` + cap enforcement + lazy pull). Add a local-fleet branch to `request_router._select_provider` that defers to `PlacementEngine`; leave the commercial path untouched.

- [ ] **Step 4: Run tests, verify pass**; `python3 -m pytest tests/ -k "router or placement" --no-cov --tb=short 2>&1 | tail -5`.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/fleet/placement.py tests/unit/fleet/test_placement.py shared/utils/request_router.py
  git commit -m "feat(fleet): placement engine — session affinity, hot-pin, lazy pull, origin deny-list; router wiring" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 8: EXO external-only backend (`shared/fleet/exo.py`)

`type: exo, mode: external` API-only plugin (§10.1). **GPLv3 boundary — no EXO code in-repo, network calls only** (aiohttp against a registered EXO cluster endpoint). `management_scope` is effectively `register_and_route` (WaddleAI does not lifecycle EXO). `provision` is a no-op that registers/validates the endpoint; `deprovision` deregisters.

**Files:** Create `shared/fleet/exo.py`, `tests/unit/fleet/test_exo.py`. Register in the Task 3 factory.

- [ ] **Step 1: Write failing tests** — mocked aiohttp: `list_nodes`/`endpoints_for`/`health` derive from the EXO cluster's HTTP API; `provision` on an unreachable endpoint raises; `place_model` maps to an EXO model-availability call; assert **no `import exo` / no vendored EXO source** anywhere (grep guard in-test).

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement `ExoFleetBackend(InferenceFleetBackend)`** — `type = BackendType.EXO`, forced `register_and_route`; pure HTTP client; `@register(BackendType.EXO)`.

- [ ] **Step 4: Run tests, verify pass** — add EXO (mocked) to the conformance `BACKENDS` list.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/fleet/exo.py tests/unit/fleet/test_exo.py tests/conformance/test_fleet_conformance.py
  git commit -m "feat(fleet): EXO external-only API backend (GPLv3 network boundary, no in-repo code)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 9: Idle-teardown controller (`shared/fleet/idle_teardown.py`)

**Mandatory cost control for `full_lifecycle` cloud backends** (§10.1, Q#9): configurable idle window → automatic endpoint teardown → redeploy-on-demand at next request; **every action audit-logged**. Cross-cutting, so it lives once and is shared by Vertex/Bedrock (Tasks 10–11) rather than duplicated.

**Files:** Create `shared/fleet/idle_teardown.py`, `tests/unit/fleet/test_idle_teardown.py`.

- [ ] **Step 1: Write failing tests** — (a) a `full_lifecycle` backend idle past its window is torn down (`deprovision` called) and an audit row written (`action="idle_teardown"`, backend_id, node_id, ts); (b) a request for a torn-down model triggers `redeploy_on_demand` (`provision` called) + audit row; (c) `register_and_route` backends are never torn down; (d) idle timer resets on activity; (e) teardown failure is audit-logged and retried, never silently swallowed.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** — `IdleTeardownController(db, registry, audit_log)`; last-activity tracked in Valkey (`waddleai:fleet:activity:{backend_id}:{node_id}`); a scheduled sweep (supercronic job entry) tears down idle full_lifecycle endpoints; `ensure_deployed(backend, model)` redeploys on demand. All actions write to the audit log.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/fleet/idle_teardown.py tests/unit/fleet/test_idle_teardown.py
  git commit -m "feat(fleet): idle-teardown controller for full_lifecycle backends (audit-logged)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 10: `VertexAIFleetBackend` (`shared/fleet/vertex_ai.py`)

Professional-gated cloud backend via `google-cloud-aiplatform` (Apache-2.0). Per-backend `management_scope`: `register_and_route` (route/health/meter existing endpoints) or `full_lifecycle` (deploy/scale/undeploy + Task 9 idle-teardown). Cloud endpoints count as managed nodes for Pro metering (§2.4). Credentials per-org via the provider-credential pattern; blocking SDK calls off the event loop (`asyncio.to_thread`).

**Files:** Create `shared/fleet/vertex_ai.py`, `tests/unit/fleet/test_vertex_ai.py`. Register in factory.

- [ ] **Step 1: Write failing tests** — mocked `aiplatform`: `register_and_route` mode's `list_nodes`/`endpoints_for`/`health` reflect existing Vertex endpoints; `full_lifecycle` `provision` calls `Model.deploy`, `deprovision` calls `undeploy`, and wires the idle-teardown controller; each cloud endpoint surfaces as a `NodeInfo(kind="cloud")` counted for metering; SDK calls run via `asyncio.to_thread` (never on the loop); credentials pulled from the decrypted `credentials_ref`.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement `VertexAIFleetBackend(InferenceFleetBackend)`** — `type = BackendType.VERTEX_AI`; scope-aware method bodies; `@register(...)`; idle-teardown wired for `full_lifecycle`.

- [ ] **Step 4: Run tests, verify pass** — add Vertex (mocked) to conformance `BACKENDS`.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/fleet/vertex_ai.py tests/unit/fleet/test_vertex_ai.py tests/conformance/test_fleet_conformance.py
  git commit -m "feat(fleet): VertexAIFleetBackend (Pro-gated, per-backend scope, idle-teardown)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 11: `BedrockFleetBackend` (`shared/fleet/bedrock.py`)

Professional-gated cloud backend via `boto3` (Apache-2.0), same scope/metering/idle-teardown contract as Vertex; blocking boto calls wrapped in `asyncio.to_thread`.

**Files:** Create `shared/fleet/bedrock.py`, `tests/unit/fleet/test_bedrock.py`. Register in factory.

- [ ] **Step 1: Write failing tests** — mocked boto3 `bedrock`/`bedrock-runtime`: `register_and_route` reflects existing provisioned-throughput endpoints; `full_lifecycle` `provision`/`deprovision` map to `create_provisioned_model_throughput`/`delete_...` + idle-teardown; endpoints are `NodeInfo(kind="cloud")` metered; boto calls off the event loop.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement `BedrockFleetBackend`** — `type = BackendType.BEDROCK`; `@register(...)`.

- [ ] **Step 4: Run tests, verify pass** — add Bedrock (mocked) to conformance `BACKENDS` (all five now present).

- [ ] **Step 5: Commit**
  ```bash
  git add shared/fleet/bedrock.py tests/unit/fleet/test_bedrock.py tests/conformance/test_fleet_conformance.py
  git commit -m "feat(fleet): BedrockFleetBackend (Pro-gated, boto3 off event loop, idle-teardown)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 12: Fleet-backend API + Pro gating (`api/v1/fleet.py`)

`fleet_backends` CRUD (`/api/v1/fleet/backends`), `management_scope` selection at creation, and **Pro gating on `vertex_ai`/`bedrock` creation** — `features.enabled("fleet_v2")` **and** `LicenseClient.check_feature("hybrid_targets")` (two-layer gate, §14.6). Status/health surfaced via the interface. Existing `ollama.py`/`llamacpp.py` routes stay compatible (they resolve through the registry when a `fleet_backend_id` is present, else legacy path).

**Files:** Create `services/management/app/api/v1/fleet.py`. Modify `services/management/app/api/v1/__init__.py`. Create `tests/unit/management/test_fleet_api.py`.

- [ ] **Step 1: Write failing tests** — (a) CRUD create/list/get/delete of ollama/llamacpp/exo backends works under any tier when flag ON; (b) creating a `vertex_ai`/`bedrock` backend under `community` → 403 naming the tier; under `professional` with flag ON → 201; (c) `check_feature` false but flag on → still blocked (two-layer); (d) flag `fleet_v2` OFF → the new `/fleet/backends` routes return feature-disabled, and legacy `/ollama/deployments` behaves exactly as before (regression guard); (e) `management_scope` persisted and echoed.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** — Quart blueprint, async routes, `require_scope` auth, `validate_request`; Pro gating via the unified `features.enabled` + `check_feature`; register blueprint in `__init__.py`.

- [ ] **Step 4: Run tests, verify pass**; `python3 -m pytest tests/unit/management -k "fleet or app_init" --no-cov --tb=short 2>&1 | tail -5`.

- [ ] **Step 5: Commit**
  ```bash
  git add services/management/app/api/v1/fleet.py services/management/app/api/v1/__init__.py tests/unit/management/test_fleet_api.py
  git commit -m "feat(fleet): fleet_backends API + two-layer Pro gating on vertex_ai/bedrock" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 13: Hardened Ollama image (`images/ollama/Dockerfile`) + Trivy gate

`ghcr.io/penguintechinc/waddleai/ollama` (§10.2): digest-pinned upstream base, non-root UID, all caps dropped, seccomp `RuntimeDefault`; writable paths limited to the model-store PVC + a tmp emptyDir (image itself has no world-writable dirs). Two tags — **`hardened`** (minimal, no shell) and **`debug`** (adds a shell for `kubectl exec`). Model pulls happen in an initContainer running the same binary. Trivy scan gate in CI. **Delegate to `moby-expert`.**

**Files:** Create `images/ollama/Dockerfile`, `images/ollama/README.md`. Modify `.github/workflows/build-images.yml`.

- [ ] **Step 1: Write failing container-structure test** — `images/ollama/structure-test.yaml` (container-structure-test): asserts `hardened` runs as non-root UID, has no `/bin/sh`, and exposes the ollama binary; `debug` has a shell. Run → fails (no Dockerfile).

- [ ] **Step 2: Author the Dockerfile** — multi-stage; pin upstream base by `@sha256`; `hardened` final stage from `debian:bookworm-slim@sha256:...` with only the ollama binary + non-root user; `debug` stage adds `busybox`/shell. Both tags from one Dockerfile via build target.

- [ ] **Step 3: CI build + Trivy gate** — `.github/workflows/build-images.yml`: build both tags multi-arch (amd64+arm64), run `trivy image --exit-code 1 --severity HIGH,CRITICAL`, publish digest; block on Trivy findings.

- [ ] **Step 4: Verify** — `container-structure-test test --image ...:hardened --config images/ollama/structure-test.yaml`; `trivy image` clean locally.

- [ ] **Step 5: Commit**
  ```bash
  git add images/ollama/ .github/workflows/build-images.yml
  git commit -m "feat(fleet): hardened+debug Ollama image (non-root, digest-pinned, Trivy-gated)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 14: Helm — consume hardened image, readOnlyRootFS, pool mode, GPU subcharts

Update `ollama-daemonset.yaml` + the GPU plugin subcharts to consume the hardened image; enforce `readOnlyRootFilesystem: true` with writable mounts only for the model-store PVC + tmp emptyDir; add the pool (Deployment+nodeSelector) mode toggle; initContainer pulls models via the same hardened binary. **Delegate to `k8s-manifest-builder`.**

**Files:** Modify `k8s/helm/waddleai/templates/ollama-daemonset.yaml`, `values.yaml` + `values-alpha/beta/prod.yaml`, `charts/amd-rocm-plugin/*`, `charts/intel-gpu-plugin/*` (+ nvidia gpu-operator values).

- [ ] **Step 1: Write failing render assertions** — `tests/helm/test_ollama_render.py` (or `helm template | ...`): asserts DaemonSet + initContainer use `ghcr.io/penguintechinc/waddleai/ollama@sha256:...`; every fleet container has `readOnlyRootFilesystem: true`, `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities.drop:[ALL]`, seccomp `RuntimeDefault`; only model/tmp mounts are writable; `ollama.mode=pool` renders a Deployment instead of a DaemonSet. Run → fails.

- [ ] **Step 2: Update templates + values** — hardened image digest in values; readOnlyRootFS + writable mounts; `ollama.mode` conditional (daemonset|pool); GPU subcharts reference the same image.

- [ ] **Step 3: Verify** — `helm template k8s/helm/waddleai --set ollama.mode=daemonset` and `=pool` render cleanly; `helm lint`; render assertions green.

- [ ] **Step 4: Commit**
  ```bash
  git add k8s/helm/waddleai/templates/ollama-daemonset.yaml k8s/helm/waddleai/values*.yaml k8s/helm/waddleai/charts/ tests/helm/test_ollama_render.py
  git commit -m "feat(fleet): Helm consumes hardened Ollama image; readOnlyRootFS + pool mode + GPU subcharts" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 15: Access control — ClusterIP + CNP + external-node auth

The AIProxy is the **only** authenticated path to inference (§10.3): fleet Services are ClusterIP-only; a CiliumNetworkPolicy admits ingress **only from AIProxy pods** (cross-ref the Cilium branch §12 — this task ships the fleet-side CNP + values switch, not the whole §12 policy set). External/bare-metal nodes (Q#4): **mTLS via cert-manager in beta/prod; shared-token sidecar in alpha/dev**. **Delegate to `k8s-manifest-builder`.**

**Files:** Create `k8s/helm/waddleai/templates/ollama-networkpolicy.yaml`, `k8s/helm/waddleai/templates/fleet-external-mtls.yaml`. Modify `k8s/helm/waddleai/templates/ollama-service.yaml` (ClusterIP), `values*.yaml`.

- [ ] **Step 1: Write failing render + policy assertions** — assert fleet Service `type: ClusterIP`; CNP `ingress.fromEndpoints` selects only the AIProxy label; external-node auth renders a cert-manager `Certificate` under beta/prod values and a shared-token sidecar under alpha values (mutually exclusive, env-driven per Q#4). Run → fails.

- [ ] **Step 2: Author templates** — CiliumNetworkPolicy (guarded by a Cilium-CRD capability check so non-Cilium clusters skip); ClusterIP service; external-node mTLS Certificate (beta/prod) vs token sidecar (alpha) toggled by values.

- [ ] **Step 3: Verify** — `helm template` per env renders the right auth mode; `helm lint`; assertions green.

- [ ] **Step 4: Commit**
  ```bash
  git add k8s/helm/waddleai/templates/ollama-networkpolicy.yaml k8s/helm/waddleai/templates/fleet-external-mtls.yaml k8s/helm/waddleai/templates/ollama-service.yaml k8s/helm/waddleai/values*.yaml
  git commit -m "feat(fleet): ClusterIP + CNP AIProxy-only ingress; external-node mTLS (beta/prod) / token sidecar (alpha)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 16: §10.5 acceptance — conformance across 5 backends + kind/mTLS/idle/caps/gating

Turn every §10.5 acceptance item into an explicit verify step: the interface conformance suite across all five backends (Ollama + llama.cpp **real in kind**; EXO/Vertex/Bedrock mocked); CNP verification in kind (unauthorized pod cannot reach the Ollama service); hardened-image Trivy gate + runs-as-nonroot/readonly-rootfs Helm assertions + initContainer pull under the `hardened` tag; mTLS handshake against an external-node simulator; idle-teardown lifecycle (idle→teardown→redeploy, all audit-logged); Free-tier cap tests at both enforcement points; Pro gating for `vertex_ai`/`bedrock` creation.

**Files:** Create `tests/integration/test_fleet_acceptance.py`.

- [ ] **Step 1: Conformance green across 5 backends** — `python3 -m pytest tests/conformance/test_fleet_conformance.py -v --no-cov` (kind for Ollama/llama.cpp, mocks for the rest).
- [ ] **Step 2: CNP negative test in kind** — deploy fleet + a non-AIProxy pod; assert the pod cannot reach the Ollama ClusterIP; AIProxy pod can.
- [ ] **Step 3: Hardened image** — Trivy gate passes; Helm assertions confirm non-root + readOnlyRootFS; initContainer pull works under the `hardened` (no-shell) tag.
- [ ] **Step 4: mTLS handshake** — external-node simulator; assert a request without a valid cert-manager cert is refused, a valid one succeeds (beta/prod path); alpha token-sidecar path succeeds with the shared token.
- [ ] **Step 5: Idle-teardown lifecycle** — full_lifecycle mock cloud backend: idle window elapses → teardown + audit row; next request → redeploy + audit row.
- [ ] **Step 6: Free-tier caps at both points** — `place_model`/`provision` reject the 6th node and 4th non-utility model under `community` with a tier-named error; utility models excluded.
- [ ] **Step 7: Pro gating** — `vertex_ai`/`bedrock` creation blocked under `community`, allowed under `professional` (flag ON).
- [ ] **Step 8: Flag-off proof** — `waddleai.fleet_v2` OFF: `/fleet/backends` feature-disabled, caps/placement no-op, legacy `/ollama/deployments` + `/llamacpp/deployments` byte-compatible.
- [ ] **Step 9: Coverage gate** — `python3 -m pytest tests/ --cov=shared/fleet --cov-fail-under=90 2>&1 | tail -15` (§14.2).
- [ ] **Step 10: Commit**
  ```bash
  git add tests/integration/test_fleet_acceptance.py
  git commit -m "test(fleet): §10.5 acceptance — 5-backend conformance, CNP, mTLS, idle-teardown, caps, Pro gating" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

## Self-Review Against Spec §10

| Spec §10 requirement | Task |
|---|---|
| §10.1 `InferenceFleetBackend` ABC (6 methods) + value types | 1 |
| §10.1 `fleet_backends` registry table + `management_scope` (migration 012) | 2 |
| §10.1 backend registry/factory + credential resolution | 3 |
| §10.1 OllamaDeploymentManager refactor → interface (restructure, +pool mode) | 4 |
| §10.1 LlamaCppManager refactor → interface (`/tokenize` counts) | 5 |
| §10.1 EXO external-only plugin (GPLv3 boundary, no in-repo code) | 8 |
| §10.1 VertexAIFleetBackend (Pro-gated, per-backend scope) | 10 |
| §10.1 BedrockFleetBackend (Pro-gated, boto3 off loop) | 11 |
| §10.1 full_lifecycle mandatory idle-teardown (audit-logged, redeploy-on-demand) | 9, 10, 11 |
| §10.1/§2.4 cloud endpoints count as managed nodes (Pro metering) | 6, 10, 11 |
| §10.2 hardened Ollama image (digest-pinned, non-root, hardened+debug tags, Trivy gate, initContainer pull) | 13 |
| §10.2 DaemonSet + GPU subcharts consume hardened image; readOnlyRootFS | 14 |
| §10.3 ClusterIP + CNP AIProxy-only ingress (cross-ref §12) | 15 |
| §10.3 external-node mTLS (beta/prod) / shared-token sidecar (alpha) — Q#4 | 15 |
| §10.4 `endpoints_for` + session affinity, hot-model pinning, lazy pull | 7 |
| §10.4 origin deny-list at `place_model` (§2.2) | 7 |
| §10.4 Free-tier caps ≤5 nodes/≤3 models, utility excluded (Q#7) | 6 |
| §10.4 Pro gating on `vertex_ai`/`bedrock` creation | 12 |
| §10.1/§7.5 routing engine consults `endpoints_for` (placement-aware dispatch) | 7 |
| §10.5 interface conformance across 5 backends (real Ollama/llama.cpp in kind, others mocked) | 4, 5, 8, 10, 11, 16 |
| §10.5 acceptance (CNP, mTLS, idle lifecycle, caps, Pro gating, hardened image) | 16 |
| §14.5 flag `waddleai.fleet_v2`, fail-safe OFF, flag-off proof per task | 1–16 |
| §14.6 two-layer gate (flag AND `check_feature("hybrid_targets")`); keepalive `nodes` metering | 6, 12 |
| §13.1 migration 012 round-trip + downgrade | 2 |
