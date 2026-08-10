# Phase 1 — Cilium Policy Reconciler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Work on branch `feature/cilium-policy-reconciler`, branched off `release/v0.2.X`. **Depends on `feature/aiproxy-migration`** (needs the `ProxyPipeline`, the token gate that owns per-key RPM, and the `shared/licensing/features.py` flag helper) — branch off the release line only after aiproxy-migration has merged. Merge back into `release/v0.2.X` without a PR when complete. This is the **4th** branch in the §14.1 dependency chain.

**Goal:** Management (Quart) becomes the control-plane authority for Cilium data-plane policy (spec §12, §5.3): a reconciler renders and upserts **CiliumEnvoyConfig** (Envoy `local_ratelimit`, per-org descriptors from DB RPM) and **CiliumNetworkPolicy** (default-deny + explicit topology flows) from DB settings via the kubernetes Python client, through an **RBAC-scoped ServiceAccount + ClusterRole** limited to `cilium.io` CRDs. On non-Cilium clusters the reconciler **detects the missing CRDs and no-ops with a clear status** instead of failing. Every CRD-emitting Helm template is capability-guarded and per-class values-toggled (**ON by default**). Management never enters the request path — it writes policy, Cilium enforces it.

**Rate-limit split (§5.3, Q#10):** **per-org limits at the Cilium/Envoy edge** (this branch's CEC descriptors, keyed on an org header); **per-key limits in the AIProxy token gate** (`shared/utils/token_limiter.py`, delivered by aiproxy-migration — *not* rendered here). This avoids CEC churn on key rotation. This plan touches only the per-org edge path.

**Architecture:** `services/management/app/services/cilium_policy.py` holds pure **render functions** (`render_envoy_config`, `render_network_policies`) plus a `CiliumPolicyReconciler` orchestrator. Render functions are total, deterministic, cluster-free (snapshot-tested). The orchestrator does runtime CRD **capability detection**, upserts via `CustomObjectsApi` (create-or-replace), and returns a `ReconcileStatus`; it **never raises into Management startup** — any `ApiException`/absent-CRD/flag-off path yields a degraded/skipped status. Topology (namespace + pod-selector labels for gateway/AIProxy/fleet/Management/Postgres/Valkey) is injected from a Helm-rendered ConfigMap so selectors are environment-configurable while rendering stays in Python. The reconciler is gated by the `waddleai.native_rate_limit` PostHog flag (default **OFF**, fail-safe OFF on any error) via `features.enabled(...)`.

**Static vs dynamic ownership (see judgment call):** the Helm chart ships a **capability-guarded bootstrap default-deny CNP + RBAC + topology ConfigMap** (day-0/GitOps protection, golden-per-toggle tested); the **Python reconciler is the runtime authority** for the per-org rate-limit CEC and the full explicit-flow CNP, rendered from DB/config and upserted live.

**Feature flag:** `waddleai.native_rate_limit` (§14.5) wraps the whole reconciler. Default OFF at launch; flipped on in Penguin Licensing after beta validation. A **flag-off proof** test (§14.2) shows zero CRD writes when off. This is a **community-tier** capability (core rate limiting) — flag only, no `check_feature()` entitlement gate.

**Tech Stack:** Python 3.13, Quart 0.19+, hypercorn, penguin-aaa, penguin-dal, SQLAlchemy 2 + Alembic, `kubernetes==35.0.0` (pinned; already vendored in `proxy/`), pytest + pytest-asyncio, Helm v3, Kustomize, Cilium `cilium.io/v2` (CiliumNetworkPolicy) + `cilium.io/v2` (CiliumEnvoyConfig), Debian bookworm images.

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `services/management/requirements.in` | Add `kubernetes>=35.0.0` (pin matches proxy) |
| Modify | `services/management/requirements.txt` | Recompiled via `uv pip compile --generate-hashes` |
| Modify | `services/management/app/models_sqlalchemy.py` | Add `Organization.rpm_limit` (nullable Integer — per-org edge RPM) |
| Create | `services/management/migrations/versions/*_add_org_rpm_limit.py` | Alembic migration: add `organizations.rpm_limit`; downgrade + round-trip |
| Create | `services/management/app/services/cilium_policy.py` | k8s client loaders, capability detection, render functions, `CiliumPolicyReconciler`, `ReconcileStatus` |
| Create | `services/management/app/api/v1/cilium.py` | `GET /api/v1/cilium/status`, `POST /api/v1/cilium/reconcile` (admin) |
| Modify | `services/management/app/__init__.py` | Register `cilium_bp`; `@app.before_serving` bootstrap reconcile; org-write reconcile hook |
| Modify | `services/management/app/api/v1/organizations.py` | Trigger reconcile after org RPM create/update (fire-and-forget, non-blocking) |
| Create | `services/management/tests/unit/test_cilium_render.py` | Snapshot tests for CEC + CNP render functions |
| Create | `services/management/tests/unit/test_cilium_reconciler.py` | Reconciler upsert/capability/flag/graceful tests (mocked k8s) |
| Create | `services/management/tests/unit/test_cilium_api.py` | Status + reconcile endpoint tests |
| Create | `services/management/tests/unit/snapshots/cilium_*.json` | Committed golden renders |
| Create | `k8s/helm/waddleai/templates/cilium-rbac.yaml` | ClusterRole (cilium.io CRDs + apiextensions read) + ClusterRoleBinding to Management SA |
| Create | `k8s/helm/waddleai/templates/cilium-network-policy.yaml` | Bootstrap default-deny + topology-flow CNP (capability + toggle guarded) |
| Create | `k8s/helm/waddleai/templates/cilium-configmap.yaml` | Topology ConfigMap consumed by the reconciler |
| Modify | `k8s/helm/waddleai/templates/_helpers.tpl` | `waddleai.cilium.topology` helper (namespace + selector labels) |
| Modify | `k8s/helm/waddleai/templates/management-deployment.yaml` | Mount topology ConfigMap env; `CILIUM_*` env |
| Modify | `k8s/helm/waddleai/values.yaml` | `cilium:` block — per-class toggles (ON by default) |
| Modify | `k8s/helm/waddleai/values-alpha.yaml` | Alpha: capability-honest defaults (reconciler on, CRDs may be absent) |
| Modify | `k8s/helm/waddleai/values-beta.yaml` | Beta host/topology overrides |

---

## Task Group A — Reconciler Render Core (pure, DB-driven, cluster-free)

### Task A1: Kubernetes client dep, `organizations.rpm_limit`, k8s loaders + CRD capability detection

**Files:** Modify `requirements.in`/`requirements.txt`, `models_sqlalchemy.py`; Create migration + `services/cilium_policy.py` (loaders + capability only) + `tests/unit/test_cilium_reconciler.py` (capability cases).

- [ ] **Step 1 (test first):** Create `services/management/tests/unit/test_cilium_reconciler.py` with capability-detection cases against a **mocked** `ApiextensionsV1Api`:
  - both CRDs present → `cilium_capabilities()` returns `{"network_policy": True, "envoy_config": True, "available": True}`.
  - only `ciliumnetworkpolicies.cilium.io` present → `envoy_config: False`, `available: True`.
  - neither present → all `False`, `available: False`.
  - client raises `ApiException(status=403)` / `ConfigException` → returns all-`False` **without raising** (graceful degradation), logs a warning.

```python
def test_capabilities_absent_when_crds_missing(monkeypatch):
    from services.management.app.services import cilium_policy as cp
    monkeypatch.setattr(cp, "get_k8s_apiext_client", lambda: _FakeApiext(crds=[]))
    caps = cp.cilium_capabilities()
    assert caps == {"network_policy": False, "envoy_config": False, "available": False}
```

- [ ] **Step 2:** In `requirements.in` add under a `# Kubernetes control-plane (Cilium reconciler)` heading:

```
kubernetes>=35.0.0
```

Recompile hashes:

```bash
cd ./services/management
uv pip compile requirements.in --generate-hashes -o requirements.txt
```

- [ ] **Step 3:** In `models_sqlalchemy.py`, add to `Organization` (after `token_quota_daily`):

```python
    rpm_limit = Column(Integer)  # Per-org requests/min enforced at the Cilium/Envoy edge (§5.3)
```

Create the Alembic migration `*_add_org_rpm_limit.py` (add column, nullable; `downgrade` drops it) with a seeded round-trip test. **Migration number: coordinate at merge** — this branch lands after 008 and before 009; either fold this column into the dependency branch's `007_drop_ailb_add_native_limits` (which already adds native limit columns) or assign the next free sequential number. Do not hard-collide with 009–014.

- [ ] **Step 4:** In `services/cilium_policy.py`, add k8s client loaders mirroring `llamacpp_manager.py` (in-cluster → kubeconfig fallback): `get_k8s_apiext_client()` (`ApiextensionsV1Api`), `get_k8s_custom_objects_client()` (`CustomObjectsApi`). Add `cilium_capabilities() -> dict` that lists CRDs and checks for `ciliumnetworkpolicies.cilium.io` and `ciliumenvoyconfigs.cilium.io`, catching every exception and returning the all-`False` dict. Constants: `CILIUM_GROUP = "cilium.io"`, `CILIUM_VERSION = "v2"`, `CNP_PLURAL = "ciliumnetworkpolicies"`, `CEC_PLURAL = "ciliumenvoyconfigs"`.

- [ ] **Step 5: Run tests + verify hashes**

```bash
cd .
python3 -m pytest services/management/tests/unit/test_cilium_reconciler.py -k capabilit -v --no-cov
grep -c "\--hash=sha256:" services/management/requirements.txt
```

Expected: capability tests green; hash count > 0.

- [ ] **Step 6: Commit**

```bash
git add services/management/requirements.in services/management/requirements.txt services/management/app/models_sqlalchemy.py services/management/migrations/versions/*_add_org_rpm_limit.py services/management/app/services/cilium_policy.py services/management/tests/unit/test_cilium_reconciler.py
git commit -m "feat(cilium): add k8s client, org rpm_limit column, and CRD capability detection

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task A2: `render_envoy_config()` — per-org edge rate-limit descriptors from DB RPM

**Files:** Modify `services/cilium_policy.py`; Create `tests/unit/test_cilium_render.py` + `snapshots/cilium_cec_*.json`.

- [ ] **Step 1 (test first):** In `test_cilium_render.py`, snapshot-test the pure function `render_envoy_config(orgs, topology)` where `orgs` is a list of `(org_id, name, rpm_limit, enabled)` tuples:
  - empty orgs → a valid CEC with an empty descriptor list (still well-formed; no crash).
  - single enabled org with `rpm_limit=600` → one `local_ratelimit` token-bucket descriptor keyed on header `x-waddleai-org-id: <id>`, `max_tokens=600`, `tokens_per_fill=600`, `fill_interval=60s`.
  - org with `rpm_limit=None` → **excluded** (no edge limit; unlimited at edge, still gated per-key downstream).
  - `enabled=False` org → excluded.
  - multiple orgs → deterministic ordering (sorted by org_id) so snapshots are stable.

```python
def test_cec_single_org(snapshot_cmp):
    from services.management.app.services.cilium_policy import render_envoy_config
    cec = render_envoy_config(
        [(1, "acme", 600, True)],
        topology={"namespace": "waddleai", "gateway_name": "shared"},
    )
    snapshot_cmp("cilium_cec_single_org", cec)
```

- [ ] **Step 2:** Implement `render_envoy_config(orgs, topology) -> dict`. Produce a `cilium.io/v2 CiliumEnvoyConfig` targeting the Gateway/HTTPRoute listener, with an `envoy.filters.http.local_ratelimit` typed config: a global default plus one per-org descriptor entry (`request_headers` match on `x-waddleai-org-id`) → token bucket derived from `rpm_limit`. Skip orgs with null `rpm_limit` or `enabled=False`. Name the object `waddleai-org-ratelimit`, namespaced from `topology["namespace"]`. Keep it a pure dict — **no k8s calls**.

- [ ] **Step 3: Record + verify**

```bash
cd .
CONTRACT_RECORD=1 python3 -m pytest services/management/tests/unit/test_cilium_render.py -k cec -v --no-cov
python3 -m pytest services/management/tests/unit/test_cilium_render.py -k cec -v --no-cov
```

Expected: recorded then green.

- [ ] **Step 4: Commit**

```bash
git add services/management/app/services/cilium_policy.py services/management/tests/unit/test_cilium_render.py services/management/tests/unit/snapshots/cilium_cec_*.json
git commit -m "feat(cilium): render per-org CiliumEnvoyConfig local_ratelimit from DB RPM

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task A3: `render_network_policies()` — default-deny + explicit topology flows

**Files:** Modify `services/cilium_policy.py`; Create `snapshots/cilium_cnp_*.json`; extend `test_cilium_render.py`.

- [ ] **Step 1 (test first):** Snapshot-test `render_network_policies(topology) -> list[dict]`. Assert one default-deny CNP per namespace plus explicit-allow CNPs for every §12.1 flow, each keyed on the injected selector labels:
  - **client → Gateway** (ingress to gateway pods from outside / gateway namespace).
  - **Gateway → AIProxy** (ingress to AIProxy from gateway only).
  - **AIProxy → fleet** (Ollama/llama.cpp), **AIProxy → Postgres**, **AIProxy → Valkey** (egress).
  - **Management → Postgres**, **Management → Valkey**, **Management → kube-apiserver** (egress; apiserver via `toEntities: [kube-apiserver]`).
  - **fleet ingress AIProxy-only** (§10.3): fleet pods admit ingress solely from AIProxy pods — assert no other source is allowed.
  - Negative assertion: default-deny CNP has empty `ingress`/`egress` and an `endpointSelector` matching all pods in the namespace.

- [ ] **Step 2:** Implement `render_network_policies(topology)`. Read selectors from `topology` (labels for `gateway`, `aiproxy`, `fleet`, `management`, `postgres`, `valkey`). Emit `cilium.io/v2 CiliumNetworkPolicy` dicts: a `default-deny` policy plus one policy per flow using `fromEndpoints`/`toEndpoints` matchLabels, `toEntities` for `kube-apiserver`, and `toPorts` for Postgres 5432 / Valkey 6379 / fleet 11434. Pure dicts, deterministic ordering.

- [ ] **Step 3: Record + verify + full render suite**

```bash
cd .
CONTRACT_RECORD=1 python3 -m pytest services/management/tests/unit/test_cilium_render.py -v --no-cov
python3 -m pytest services/management/tests/unit/test_cilium_render.py -v --no-cov
```

- [ ] **Step 4: Commit**

```bash
git add services/management/app/services/cilium_policy.py services/management/tests/unit/test_cilium_render.py services/management/tests/unit/snapshots/cilium_cnp_*.json
git commit -m "feat(cilium): render default-deny + explicit-flow CiliumNetworkPolicy from topology

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task Group B — Reconciler Orchestration (upsert, flag gate, graceful degradation)

### Task B1: `CiliumPolicyReconciler.reconcile()` — capability-gated, flag-gated, non-raising upsert

**Files:** Modify `services/cilium_policy.py`; extend `tests/unit/test_cilium_reconciler.py`.

- [ ] **Step 1 (test first):** With a **mocked** `CustomObjectsApi`, assert `reconcile()` behavior and the returned `ReconcileStatus` dataclass (`@dataclass(slots=True)`: `applied: list[str]`, `skipped: bool`, `reason: str`, `degraded: bool`):
  - **flag OFF** (`features.enabled("native_rate_limit", ...)` patched → `False`) → **zero** `CustomObjectsApi` calls; `status.skipped is True`, `reason="flag_off"`. *(This is the §14.2 flag-off proof.)*
  - **CRDs absent** (`cilium_capabilities().available is False`) → zero writes; `skipped is True`, `reason="crds_absent"`.
  - **create path**: object 404 on read → `create_namespaced_custom_object` called; `status.applied` lists the CEC + CNP names.
  - **replace path**: object exists → `replace_namespaced_custom_object` (or `patch`) with the prior `resourceVersion`; no duplicate-create.
  - **partial capability**: only CNP CRD present → CNPs applied, CEC skipped (per-capability), status reflects it.
  - **`ApiException` mid-upsert** → caught; `degraded is True`; **does not raise**; remaining objects still attempted.

- [ ] **Step 2:** Implement `CiliumPolicyReconciler(db)` with `reconcile() -> ReconcileStatus`:
  1. `if not features.enabled("native_rate_limit", distinct_id="_global")`: return skipped(`flag_off`). Import the helper defensively — if `shared.licensing.features` is unavailable, **fail-safe OFF**.
  2. `caps = cilium_capabilities()`; if `not caps["available"]`: return skipped(`crds_absent`).
  3. Load orgs (penguin-dal, via `asyncio.to_thread` at the call site) + topology; call the pure renderers.
  4. `_upsert(client, plural, obj)` = read-then-create-or-replace, each wrapped so one failure sets `degraded` but continues.
  5. Return the aggregate `ReconcileStatus`. Never let an exception escape.

- [ ] **Step 3: Run**

```bash
cd .
python3 -m pytest services/management/tests/unit/test_cilium_reconciler.py -v --no-cov
```

Expected: all green, including the flag-off zero-write and degraded-no-raise cases.

- [ ] **Step 4: Commit**

```bash
git add services/management/app/services/cilium_policy.py services/management/tests/unit/test_cilium_reconciler.py
git commit -m "feat(cilium): reconciler upsert with flag gate, capability gate, graceful degradation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task B2: Status/reconcile API + startup bootstrap + org-write trigger

**Files:** Create `app/api/v1/cilium.py` + `tests/unit/test_cilium_api.py`; Modify `app/__init__.py`, `app/api/v1/organizations.py`.

- [ ] **Step 1 (test first):** In `test_cilium_api.py` (Quart test client):
  - `GET /api/v1/cilium/status` (admin scope) → 200 with `{capabilities, last_reconcile, applied, degraded, flag_enabled}`; **401 unauth**, **403 non-admin** (assert scope enforcement per house auth).
  - `POST /api/v1/cilium/reconcile` (admin) → 202/200, returns the `ReconcileStatus`; reconciler invoked once (mocked).
  - When CRDs absent → status endpoint still 200 with `capabilities.available=False` and a clear human-readable `reason` (no 500).
  - Org RPM update calls the reconcile trigger exactly once (patch the reconciler, assert called).

- [ ] **Step 2:** Create `cilium_bp` with the two async routes, `@require_scope("admin")` (penguin-aaa). Register it in `register_blueprints` under `/api/v1`. Add an `@app.before_serving` hook that runs one bootstrap `reconcile()` in `asyncio.to_thread` (non-blocking, swallow-and-log — startup must not fail if Cilium is absent). In `organizations.py`, after a successful RPM create/update, fire a **non-blocking** reconcile (`asyncio.create_task` / `to_thread`) so key CRUD is unaffected and CEC churn stays org-scoped only.

- [ ] **Step 3: Run + contract gate** (existing `/api/v1/*` snapshots must stay green — the new blueprint is additive):

```bash
cd .
python3 -m pytest services/management/tests/unit/test_cilium_api.py -v --no-cov
make test-contract
```

- [ ] **Step 4: Commit**

```bash
git add services/management/app/api/v1/cilium.py services/management/app/__init__.py services/management/app/api/v1/organizations.py services/management/tests/unit/test_cilium_api.py
git commit -m "feat(cilium): status/reconcile API, startup bootstrap, org-write reconcile trigger

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task Group C — Helm RBAC, Toggles, Capability-Guarded Templates

### Task C1: `cilium:` values block + topology helper (ON by default, per-class switches)

**Files:** Modify `values.yaml`, `values-alpha.yaml`, `values-beta.yaml`, `_helpers.tpl`.

- [ ] **Step 1:** Add a `cilium:` block to `values.yaml` (**all ON by default**, per §12/Q#8):

```yaml
cilium:
  enabled: true            # master switch for all Cilium integration
  rbac:
    create: true           # ClusterRole/Binding for the reconciler SA
  networkPolicy:
    enabled: true          # bootstrap default-deny + topology-flow CNP
  rateLimit:
    enabled: true          # reconciler manages per-org CEC (also needs flag waddleai.native_rate_limit)
  topology:                # selector labels consumed by the Python reconciler
    gatewayName: shared
    gatewayNamespace: gateway
    selectors:
      gateway:    { "app.kubernetes.io/name": cilium-gateway }
      aiproxy:    { "app.kubernetes.io/name": waddleai, "app.kubernetes.io/component": proxy }
      fleet:      { "app.kubernetes.io/component": fleet }
      management: { "app.kubernetes.io/name": waddleai, "app.kubernetes.io/component": management }
      postgres:   { "app.kubernetes.io/component": postgres }
      valkey:     { "app.kubernetes.io/component": valkey }
```

Add `waddleai.cilium.topology` to `_helpers.tpl` emitting the namespace + selectors as JSON for the ConfigMap. In `values-alpha.yaml` keep `cilium.enabled: true` but document that local clusters without Cilium CRDs skip gracefully (reconciler + `.Capabilities` guard handle it). In `values-beta.yaml` set the real gateway/host topology.

- [ ] **Step 2: Lint**

```bash
cd .
helm lint k8s/helm/waddleai
helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/values-beta.yaml >/dev/null && echo OK
```

- [ ] **Step 3: Commit**

```bash
git add k8s/helm/waddleai/values.yaml k8s/helm/waddleai/values-alpha.yaml k8s/helm/waddleai/values-beta.yaml k8s/helm/waddleai/templates/_helpers.tpl
git commit -m "feat(helm): cilium values block with per-class toggles (on by default) + topology helper

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task C2: RBAC — least-privilege ClusterRole + Binding to the Management SA

**Files:** Create `templates/cilium-rbac.yaml`.

- [ ] **Step 1:** Create `cilium-rbac.yaml` guarded by `{{- if and .Values.cilium.enabled .Values.cilium.rbac.create -}}`. A `ClusterRole` scoped **only** to what the reconciler needs:
  - `cilium.io` / `ciliumnetworkpolicies`, `ciliumenvoyconfigs`: `get, list, watch, create, update, patch, delete`.
  - `apiextensions.k8s.io` / `customresourcedefinitions`: `get, list` (capability detection **only** — no write).

  Bind it with a `ClusterRoleBinding` to `{{ include "waddleai.serviceAccountName" . }}` in `{{ .Values.namespace }}`. No wildcard verbs, no `*` resources, no cluster-admin.

- [ ] **Step 2: Render + least-privilege assertion**

```bash
cd .
helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/values-beta.yaml | grep -A25 "kind: ClusterRole" > /tmp/cilium-rbac.yaml
grep -q "ciliumnetworkpolicies" /tmp/cilium-rbac.yaml && grep -q "ciliumenvoyconfigs" /tmp/cilium-rbac.yaml && echo OK
grep -E "resources:\s*\[?\"?\*" /tmp/cilium-rbac.yaml && echo "WILDCARD LEAK" || echo "least-privilege OK"
helm lint k8s/helm/waddleai
```

Expected: `OK`; `least-privilege OK` (no wildcard).

- [ ] **Step 3: Commit**

```bash
git add k8s/helm/waddleai/templates/cilium-rbac.yaml
git commit -m "feat(helm): least-privilege ClusterRole/Binding for the cilium reconciler SA

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task C3: Bootstrap CNP + topology ConfigMap (capability-guarded), Management env wiring

**Files:** Create `templates/cilium-network-policy.yaml`, `templates/cilium-configmap.yaml`; Modify `templates/management-deployment.yaml`.

- [ ] **Step 1:** Create `cilium-configmap.yaml` (guarded by `.Values.cilium.enabled`) carrying the `waddleai.cilium.topology` JSON; mount it into the Management deployment as `CILIUM_TOPOLOGY` env (+ `CILIUM_ENABLED`, `CILIUM_RATELIMIT_ENABLED`). Create `cilium-network-policy.yaml` — the **bootstrap default-deny + topology-flow CNP** — guarded by **both** `{{- if and .Values.cilium.enabled .Values.cilium.networkPolicy.enabled (.Capabilities.APIVersions.Has "cilium.io/v2") -}}` so it renders only when the cluster actually has the Cilium CRDs (the §12.3/§12.4 CRD-absent graceful path at template time). Contents mirror the reconciler's default-deny + explicit flows so day-0/GitOps clusters are protected before the reconciler's first run.

- [ ] **Step 2: Golden-per-toggle render (§12.4)** — verify each toggle combination:

```bash
cd .
# all on, CRDs present (kind/beta): CNP + RBAC + CEC-configmap render
helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/values-beta.yaml --api-versions cilium.io/v2 | grep -q "kind: CiliumNetworkPolicy" && echo "cnp-on OK"
# networkPolicy off: no CNP
helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/values-beta.yaml --api-versions cilium.io/v2 --set cilium.networkPolicy.enabled=false | grep -q "kind: CiliumNetworkPolicy" && echo "LEAK" || echo "cnp-off OK"
# CRDs absent (no --api-versions): CNP template skips, chart still renders
helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/values-beta.yaml | grep -q "kind: CiliumNetworkPolicy" && echo "CRD-ABSENT LEAK" || echo "crd-absent graceful OK"
# master switch off: no cilium objects at all
helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/values-beta.yaml --api-versions cilium.io/v2 --set cilium.enabled=false | grep -Eiq "cilium" && echo "MASTER LEAK" || echo "master-off OK"
helm lint k8s/helm/waddleai
```

Expected: `cnp-on OK`, `cnp-off OK`, `crd-absent graceful OK`, `master-off OK`; lint passes.

- [ ] **Step 3: Commit**

```bash
git add k8s/helm/waddleai/templates/cilium-network-policy.yaml k8s/helm/waddleai/templates/cilium-configmap.yaml k8s/helm/waddleai/templates/management-deployment.yaml
git commit -m "feat(helm): capability-guarded bootstrap CNP + topology ConfigMap + management env

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task Group D — Acceptance Gate (§12.4)

### Task D1: Full §12.4 acceptance + standing gates

**Files:** none (verification; a residue-fix commit only if needed).

- [ ] **Step 1: Unit + render suites green, coverage on changed modules (§14.2, 90%+)**

```bash
cd .
python3 -m pytest services/management/tests/unit/test_cilium_render.py services/management/tests/unit/test_cilium_reconciler.py services/management/tests/unit/test_cilium_api.py \
  --cov=services/management/app/services/cilium_policy --cov=services/management/app/api/v1/cilium --cov-fail-under=90 -v
```

- [ ] **Step 2: Contract snapshots green** (additive blueprint changed nothing observable on existing surfaces):

```bash
make test-contract
```

- [ ] **Step 3: `helm template` golden per toggle combo + CRD-absent path + RBAC least-privilege** (re-run the C2/C3 matrix as one gate):

```bash
cd .
for f in values-alpha values-beta; do
  helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/$f.yaml --api-versions cilium.io/v2 > /tmp/cilium-golden-$f.yaml && echo "$f renders (crds present)"
  helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/$f.yaml > /tmp/cilium-golden-$f-nocrd.yaml && echo "$f renders (crds absent)"
done
helm lint k8s/helm/waddleai
```

- [ ] **Step 4: Migration round-trip + downgrade** on a seeded snapshot (house rule, §13):

```bash
cd ./services/management
alembic upgrade head && alembic downgrade -1 && alembic upgrade head && echo "org rpm_limit round-trip OK"
```

- [ ] **Step 5: Flag-off proof (§14.2) explicit assertion** — confirm the reconciler makes zero CRD writes with the flag off:

```bash
cd .
python3 -m pytest services/management/tests/unit/test_cilium_reconciler.py -k "flag_off or flag off" -v --no-cov
```

- [ ] **Step 6: kind-cluster deploy (integration, §12.4)** — where a kind+Cilium cluster is available: deploy the chart, confirm proxy + management + fleet pods up, **CNP default-deny effective** (an unauthorized cross-namespace curl to a fleet pod is blocked), the **rate-limit CEC applied**, then remove the Cilium CRDs and confirm the CRD-absent path deploys and the reconciler reports `crds_absent` without crashing. *(If no kind cluster is provisioned in this environment, record that these live checks run in CI's kind tier per §14.4 and rely on the render/mocked-upsert coverage above.)*

- [ ] **Step 7: Security + container standing gates**

```bash
cd .
make test-security   # bandit/gosec/pip-audit/trivy/gitleaks + pip-licenses OSI gate
```

- [ ] **Step 8: Final commit (only if a residue fix was required)**

```bash
git add -A
git commit -m "chore(cilium): acceptance — render/reconcile green, RBAC least-privilege, CRD-absent graceful, flag-off proof

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review Against Spec §12 / §5.3

| Spec requirement | Task |
|---|---|
| §12.1 Reconciler renders + upserts CRDs from DB via k8s Python client | A1, A2, A3, B1 |
| §12.1 CiliumEnvoyConfig `local_ratelimit`, **per-org** descriptors | A2 |
| §5.3 / Q#10 per-org at edge, **per-key stays in AIProxy token gate** (not rendered here) | A2 (scope note), B2 |
| §12.1 CiliumNetworkPolicy default-deny per namespace | A3 |
| §12.1 Explicit flows: client→Gateway, Gateway→AIProxy, AIProxy→fleet/Postgres/Valkey, Management→Postgres/Valkey/apiserver | A3 |
| §12.1 / §10.3 fleet pods admit ingress **only from AIProxy** | A3 |
| §12.1 RBAC-scoped ServiceAccount + least-privilege ClusterRole (`cilium.io` CRDs) | C2 |
| §12.1 / §12.3 CRD capability detection; no-op + clear status when absent (Python runtime) | A1, B1, B2 |
| §12.3 CRD-absent path renders cleanly (Helm template `.Capabilities` guard) | C3 |
| §12.2 / Q#8 protections ON by default, per-class Helm values switches | C1 |
| §3.3 Management owns reconciliation, never in request path (control-plane only) | B1, B2 |
| §14.5 wrapped in `waddleai.native_rate_limit` flag, default OFF, fail-safe OFF | B1 |
| §14.2 flag-off proof (zero CRD writes when off) | B1, D1 |
| Graceful degradation — reconciler never raises into Management startup | A1, B1, B2 |
| §12.4 `helm template` golden per toggle combo | C3, D1 |
| §12.4 RBAC least-privilege check | C2, D1 |
| §12.4 kind deploy: default-deny effective + CEC applied + CRD-absent graceful | D1 (kind tier) |
| §13 migration ships downgrade + round-trip (org `rpm_limit`) | A1, D1 |
| §14.2 90%+ coverage on changed modules; security + pip-licenses gates | D1 |
| Every task is TDD (tests first) and ends in a commit with Co-Authored-By trailer | A1–D1 |
