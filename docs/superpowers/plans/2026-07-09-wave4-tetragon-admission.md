# Wave 4 — Optional Tetragon Runtime + Admission Policies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Work on branch `chore/tetragon-admission-policies`, branched off `release/v0.2.X` (house rule: never branch off `main`). Merge back into `release/v0.2.X` without a PR when complete. This branch **depends on `feature/cilium-policy-reconciler`** (§14.1) — it reuses that branch's CRD-capability-detection pattern (`.Capabilities.APIVersions.Has`-gated CRD templates + graceful no-op) and layers the *shipped-with-chart* protections (§12.2) and *CRD/agent bootstrap* (§12.3) on top. It does **not** touch the rate-limit CiliumEnvoyConfig or the CiliumNetworkPolicy reconciler — those are the prior branch (§12.1); duplicating them here is a bug.

**Goal:** Ship the optional, values-gated Cilium runtime-security layer (spec §12.2/§12.3): Tetragon `TracingPolicy` CRDs that block/observe exec into AIProxy + fleet pods, observe/flag unexpected egress and file/network activity from the AIProxy, and admission policies (`ValidatingAdmissionPolicy` core, Kyverno optional) that enforce rootless / non-root UID / `readOnlyRootFilesystem` / digest-pinned images / dropped capabilities on WaddleAI workloads. Every protection class ships **ON by default** behind its own Helm values switch, and every CRD-emitting template is guarded by an API-capability check so non-Cilium / non-Tetragon clusters render and deploy cleanly (protections no-op with a clear status). The installer **detects a missing Tetragon agent/CRDs and offers to install it** (opt-in Helm dependency / documented step).

**Architecture:** All work lives in `k8s/helm/waddleai/` — new `templates/tetragon-*.yaml` and `templates/admission-*.yaml`, a `templates/_capabilities.tpl` helper (mirroring the cilium-reconciler branch's gate), `tetragon:` and `admissionPolicies:` blocks in `values*.yaml`, an optional Chart.yaml dependency, and `NOTES.txt` bootstrap/status messaging. Protections target the WaddleAI workloads deployed by this chart — AIProxy (`app.kubernetes.io/component: proxy`, added by `feature/aiproxy-migration`), fleet pods (`component: ollama`/`llamacpp`), management, webui — selected by the chart's own labels so nothing outside the release is touched. No application (Python/Go) code changes; no runtime service depends on these CRDs existing.

**Flag posture:** §12.2 protections are **Helm-values-gated (per class, ON by default)**, not PostHog-flagged — like the §4 consolidation, this is infrastructure hardening, not a user-facing product feature, so the §14.2 "flag-off proof" gate does not apply. **The merge gate is `helm template` golden files across every toggle × capability combination** plus a kind deploy proving the policies are effective and the CRD-absent path is graceful.

**Tech Stack:** Helm v3, Kustomize, Tetragon `TracingPolicy`/`TracingPolicyNamespaced` (`cilium.io/v1alpha1`), Kubernetes `ValidatingAdmissionPolicy` + `ValidatingAdmissionPolicyBinding` (`admissionregistration.k8s.io/v1`, CEL), optional Kyverno `ClusterPolicy` (`kyverno.io/v1`), kind (Cilium+Tetragon-enabled) for e2e, `helm template --api-versions` for capability simulation, yamllint/`helm lint`.

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `k8s/helm/waddleai/templates/_capabilities.tpl` | Capability-gate helpers: `waddleai.tetragon.render`, `waddleai.vap.render`, `waddleai.kyverno.render` (reuse cilium-reconciler `.Capabilities.APIVersions.Has` pattern) |
| Create | `k8s/helm/waddleai/templates/tetragon-exec-policy.yaml` | `TracingPolicyNamespaced` — block/observe exec into AIProxy + fleet pods |
| Create | `k8s/helm/waddleai/templates/tetragon-egress-policy.yaml` | `TracingPolicyNamespaced` — observe/flag AIProxy egress; optional CIDR block; unexpected file/network activity |
| Create | `k8s/helm/waddleai/templates/admission-securitycontext.yaml` | `ValidatingAdmissionPolicy` + binding — non-root/UID/RORFS/drop-caps/no-privesc on WaddleAI workloads |
| Create | `k8s/helm/waddleai/templates/admission-image-digest.yaml` | `ValidatingAdmissionPolicy` + binding — digest-pinned images; optional Kyverno `ClusterPolicy` alternative |
| Create | `k8s/helm/waddleai/templates/NOTES.txt` | Post-install status: which protections active, CRD-absent no-op notice, Tetragon bootstrap offer |
| Modify | `k8s/helm/waddleai/values.yaml` | Add `tetragon:` + `admissionPolicies:` blocks (per-class toggles ON by default; provider egress allow-list; workload selector) |
| Modify | `k8s/helm/waddleai/values-alpha.yaml` | Alpha overrides — observe mode, admission `warn`/`audit` (local dev friendliness) |
| Modify | `k8s/helm/waddleai/values-beta.yaml` | Beta overrides — enforce mode, full provider egress allow-list |
| Modify | `k8s/helm/waddleai/Chart.yaml` | Optional Tetragon sub-chart dependency (condition-gated, disabled by default) |
| Modify | `k8s/helm/waddleai/templates/_helpers.tpl` | `waddleai.workloadSelectorLabels` (shared selector for policies) |
| Create | `tests/helm/golden/*.yaml` | Committed golden renders per toggle × capability combination (the merge gate) |
| Create | `tests/helm/render-golden.sh` | Record/compare `helm template` golden harness across the combination matrix |
| Create | `tests/helm/test_tetragon_admission_kind.sh` | kind e2e: exec blocked, egress observed, root/hostPath pod rejected, CRD-absent graceful |
| Modify | `Makefile` | `test-helm-golden` + `test-tetragon-kind` targets |

---

## Task Group A — Foundation: capability gates, values schema, golden harness

### Task A1: Capability helper, values blocks, and the golden-render harness (write the gate first)

**Files:** Create `templates/_capabilities.tpl`, `tests/helm/render-golden.sh`; Modify `values.yaml`, `values-alpha.yaml`, `values-beta.yaml`, `templates/_helpers.tpl`, `Makefile`.

- [ ] **Step 1: Add the capability-gate helper** (reuse of the cilium-reconciler pattern). Create `templates/_capabilities.tpl`. A CRD template renders only when (a) its class toggle is on **and** (b) either the target CRD is registered on the cluster (`.Capabilities.APIVersions.Has`) **or** the operator forced rendering (`forceRender`, for golden files / offline `helm template`):

```gotemplate
{{/* True when Tetragon TracingPolicy CRDs are installable/present */}}
{{- define "waddleai.tetragon.render" -}}
{{- and .Values.tetragon.enabled (or .Values.tetragon.forceRender (.Capabilities.APIVersions.Has "cilium.io/v1alpha1/TracingPolicyNamespaced")) -}}
{{- end -}}

{{/* True when core ValidatingAdmissionPolicy API is available */}}
{{- define "waddleai.vap.render" -}}
{{- and .Values.admissionPolicies.enabled .Values.admissionPolicies.validatingAdmissionPolicy.enabled (or .Values.admissionPolicies.forceRender (.Capabilities.APIVersions.Has "admissionregistration.k8s.io/v1/ValidatingAdmissionPolicy")) -}}
{{- end -}}

{{/* True only when Kyverno explicitly opted-in AND its CRDs are present */}}
{{- define "waddleai.kyverno.render" -}}
{{- and .Values.admissionPolicies.enabled .Values.admissionPolicies.kyverno.enabled (or .Values.admissionPolicies.forceRender (.Capabilities.APIVersions.Has "kyverno.io/v1/ClusterPolicy")) -}}
{{- end -}}
```

- [ ] **Step 2: Add the shared workload selector** to `_helpers.tpl` so every policy targets exactly this release's WaddleAI pods (never foreign workloads):

```gotemplate
{{- define "waddleai.workloadSelectorLabels" -}}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
```

- [ ] **Step 3: Add the values blocks** to `values.yaml` (per-class toggles **ON by default** per Q#8; `forceRender: false`; provider egress allow-list seeded with the big-5 provider egress descriptors; per-class mode):

```yaml
# Cilium runtime + admission protections (spec §12.2/§12.3)
# All ON by default; each class independently switchable; every CRD template is
# API-capability-gated so non-Cilium / non-Tetragon clusters render clean and no-op.
tetragon:
  enabled: true
  forceRender: false          # true only for offline `helm template` golden renders
  install:
    offer: true               # NOTES.txt offers agent install when CRDs absent
    dependency: false         # opt-in: pull Tetragon as a Helm sub-chart (Chart.yaml)
  exec:
    enabled: true
    mode: enforce             # enforce | observe  (block vs log exec into pods)
    targets: [proxy, ollama, llamacpp]   # chart component labels to protect
    allowedExecs: []          # optional binaries permitted (e.g. health-probe path)
  egress:
    enabled: true
    mode: observe             # observe | enforce  (flag vs kill unexpected egress)
    # FQDN allow-listing is CiliumNetworkPolicy's job (cilium-reconciler, §12.1);
    # Tetragon here observes egress + optionally blocks by CIDR outside the allow-list.
    allowedCIDRs: []          # e.g. provider egress ranges; empty = observe-only
    flagUnexpectedFileWrites: true
admissionPolicies:
  enabled: true
  forceRender: false
  failurePolicy: Fail         # Fail = closed; Ignore for soft-launch environments
  validationActions: [Deny]   # Deny | Warn | Audit (VAP binding actions)
  validatingAdmissionPolicy:
    enabled: true
    enforceNonRoot: true
    enforceReadOnlyRootFs: true
    enforceDropAllCaps: true
    enforceNoPrivilegeEscalation: true
    enforceDigestPinnedImages: true
  kyverno:
    enabled: false            # optional alternative engine; only renders if Kyverno CRDs present
```

- [ ] **Step 4: Environment overrides.** `values-alpha.yaml` — dev-friendly: `tetragon.exec.mode: observe`, `admissionPolicies.validationActions: [Warn]`, `admissionPolicies.failurePolicy: Ignore` (local kind lacks the CRDs by default). `values-beta.yaml` — hardened: `tetragon.exec.mode: enforce`, `tetragon.egress.mode: enforce` with `allowedCIDRs` populated from the provider egress ranges, `admissionPolicies.validationActions: [Deny]`, `failurePolicy: Fail`.

- [ ] **Step 5: Write the golden-render harness** `tests/helm/render-golden.sh`. It renders `helm template` for the full combination matrix (class toggles × simulated capability presence via `--api-versions`) into `tests/helm/golden/`, recording when `GOLDEN_RECORD=1`, else diffing. The `--api-versions` flags are how a non-Cilium cluster is simulated offline:

```bash
#!/usr/bin/env bash
# Renders golden files across the toggle × capability matrix (spec §12.4).
set -euo pipefail
CHART=k8s/helm/waddleai
OUT=tests/helm/golden
CILIUM_API="--api-versions cilium.io/v1alpha1/TracingPolicyNamespaced"
VAP_API="--api-versions admissionregistration.k8s.io/v1/ValidatingAdmissionPolicy"
KYV_API="--api-versions kyverno.io/v1/ClusterPolicy"
mkdir -p "$OUT"

render() {  # <name> <extra helm args...>
  local name="$1"; shift
  local f="$OUT/$name.yaml"
  helm template waddleai "$CHART" "$@" > "/tmp/$name.yaml"
  if [ "${GOLDEN_RECORD:-0}" = "1" ] || [ ! -f "$f" ]; then cp "/tmp/$name.yaml" "$f"
  else diff -u "$f" "/tmp/$name.yaml" || { echo "GOLDEN DRIFT: $name"; exit 1; }; fi
}

# 1. Non-Cilium cluster, all defaults ON -> zero policy CRDs emitted (graceful no-op)
render nocrd-defaults -f "$CHART/values.yaml"
# 2. Cilium+Tetragon present, defaults ON -> tetragon + VAP rendered
render full-present -f "$CHART/values.yaml" $CILIUM_API $VAP_API
# 3. Tetragon on, admission off
render tetragon-only -f "$CHART/values.yaml" --set admissionPolicies.enabled=false $CILIUM_API $VAP_API
# 4. Admission on, tetragon off
render admission-only -f "$CHART/values.yaml" --set tetragon.enabled=false $CILIUM_API $VAP_API
# 5. Kyverno opted-in and present
render kyverno-present -f "$CHART/values.yaml" --set admissionPolicies.kyverno.enabled=true $CILIUM_API $VAP_API $KYV_API
# 6. Kyverno opted-in but CRDs absent -> no Kyverno objects
render kyverno-absent -f "$CHART/values.yaml" --set admissionPolicies.kyverno.enabled=true $CILIUM_API $VAP_API
# 7. Everything disabled -> empty
render all-off -f "$CHART/values.yaml" --set tetragon.enabled=false --set admissionPolicies.enabled=false
# 8. Beta enforce profile
render beta-enforce -f "$CHART/values.yaml" -f "$CHART/values-beta.yaml" $CILIUM_API $VAP_API
```

- [ ] **Step 6: Add make targets** to `Makefile` (`.PHONY` + targets):

```makefile
test-helm-golden:
	@echo "Rendering Helm golden combination matrix..."
	bash tests/helm/render-golden.sh

test-tetragon-kind:
	@echo "kind e2e: Tetragon + admission policies..."
	bash tests/helm/test_tetragon_admission_kind.sh
```

- [ ] **Step 7: Record + verify the no-op baseline.** With every class ON but no CRDs present, the chart must emit zero policy objects and still lint:

```bash
cd /home/penguin/code/waddleai
GOLDEN_RECORD=1 bash tests/helm/render-golden.sh
grep -c "kind: TracingPolicyNamespaced\|kind: ValidatingAdmissionPolicy" tests/helm/golden/nocrd-defaults.yaml || echo "0 policies on non-Cilium (correct)"
helm lint k8s/helm/waddleai
bash tests/helm/render-golden.sh   # re-run, must be clean (no drift)
```

Expected: `nocrd-defaults.yaml` contains **zero** policy CRDs; lint passes; second run reports no drift.

- [ ] **Step 8: Commit**

```bash
git add k8s/helm/waddleai/templates/_capabilities.tpl k8s/helm/waddleai/templates/_helpers.tpl k8s/helm/waddleai/values.yaml k8s/helm/waddleai/values-alpha.yaml k8s/helm/waddleai/values-beta.yaml tests/helm/render-golden.sh Makefile tests/helm/golden/nocrd-defaults.yaml tests/helm/golden/all-off.yaml
git commit -m "feat(helm): capability-gated protection scaffolding + golden-render harness

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task Group B — Tetragon TracingPolicies (§12.2)

### Task B2: exec block/observe TracingPolicy for AIProxy + fleet pods

**Files:** Create `templates/tetragon-exec-policy.yaml`; regenerate affected golden files.

- [ ] **Step 1:** Create `templates/tetragon-exec-policy.yaml`, guarded by `{{- if eq (include "waddleai.tetragon.render" .) "true" }}`. Emit a namespaced `TracingPolicy` hooking process exec on pods matching the chart's workload label + the configured component labels; `mode: enforce` attaches a `Sigkill`/override action, `mode: observe` logs only. Honor `tetragon.exec.allowedExecs` (e.g. the native health-probe binary) as matchArgs exceptions:

```gotemplate
{{- if eq (include "waddleai.tetragon.render" .) "true" }}
{{- if .Values.tetragon.exec.enabled }}
apiVersion: cilium.io/v1alpha1
kind: TracingPolicyNamespaced
metadata:
  name: {{ include "waddleai.fullname" . }}-exec-guard
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "waddleai.labels" . | nindent 4 }}
spec:
  podSelector:
    matchLabels:
      {{- include "waddleai.workloadSelectorLabels" . | nindent 6 }}
    matchExpressions:
      - key: app.kubernetes.io/component
        operator: In
        values: {{ .Values.tetragon.exec.targets | toJson }}
  kprobes:
    - call: "security_bprm_creds_from_file"     # exec entrypoint
      syscall: false
      args:
        - index: 0
          type: "file"
      selectors:
        - matchActions:
            {{- if eq .Values.tetragon.exec.mode "enforce" }}
            - action: Sigkill
            {{- else }}
            - action: Post
            {{- end }}
{{- end }}
{{- end }}
```

- [ ] **Step 2: Record + verify** the exec policy appears only when Tetragon is present, and respects mode:

```bash
cd /home/penguin/code/waddleai
GOLDEN_RECORD=1 bash tests/helm/render-golden.sh
grep -q "kind: TracingPolicyNamespaced" tests/helm/golden/full-present.yaml && echo "exec policy rendered"
grep -q "action: Sigkill" tests/helm/golden/beta-enforce.yaml && echo "beta enforces"
grep -A40 "exec-guard" tests/helm/golden/nocrd-defaults.yaml && echo "LEAKED" || echo "absent on non-Cilium (correct)"
helm lint k8s/helm/waddleai
```

Expected: rendered under `full-present`/`beta-enforce` (Sigkill in beta), absent in `nocrd-defaults`.

- [ ] **Step 3: Commit**

```bash
git add k8s/helm/waddleai/templates/tetragon-exec-policy.yaml tests/helm/golden/
git commit -m "feat(helm): Tetragon exec-guard TracingPolicy for AIProxy + fleet pods

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task B3: egress observe/flag + unexpected file-activity TracingPolicy

**Files:** Create `templates/tetragon-egress-policy.yaml`; regenerate golden files.

- [ ] **Step 1:** Create `templates/tetragon-egress-policy.yaml` (same render gate + `tetragon.egress.enabled`). Hook `tcp_connect` on AIProxy pods to observe outbound connections; when `mode: enforce` and `allowedCIDRs` is set, `NotDAddr`-match outside the allow-list → `Sigkill`, else `Post` (flag). Add a second kprobe (guarded by `flagUnexpectedFileWrites`) on `security_file_permission`/write into non-writable paths to flag unexpected file activity. **Note in a template comment** that FQDN provider allow-listing is the cilium-reconciler CNP's responsibility (§12.1) — this policy observes/CIDR-blocks only, no FQDN duplication.

- [ ] **Step 2: Record + verify:**

```bash
cd /home/penguin/code/waddleai
GOLDEN_RECORD=1 bash tests/helm/render-golden.sh
grep -q "tcp_connect\|tcp_v4_connect" tests/helm/golden/full-present.yaml && echo "egress observed"
grep -q "action: Post" tests/helm/golden/full-present.yaml && echo "observe mode default"
helm lint k8s/helm/waddleai
```

- [ ] **Step 3: Commit**

```bash
git add k8s/helm/waddleai/templates/tetragon-egress-policy.yaml tests/helm/golden/
git commit -m "feat(helm): Tetragon egress-observe + file-activity TracingPolicy (CIDR block optional)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task Group C — Admission Policies (§12.2)

### Task C1: ValidatingAdmissionPolicy — securityContext hardening

**Files:** Create `templates/admission-securitycontext.yaml`; regenerate golden files.

- [ ] **Step 1:** Create `templates/admission-securitycontext.yaml`, guarded by `{{- if eq (include "waddleai.vap.render" .) "true" }}`. Emit a `ValidatingAdmissionPolicy` with CEL `validations` (each individually toggled by the `enforce*` values) and a `ValidatingAdmissionPolicyBinding` scoped by `namespaceSelector`/`objectSelector` to WaddleAI workloads, using `.Values.admissionPolicies.validationActions` and `failurePolicy`:

```gotemplate
{{- if eq (include "waddleai.vap.render" .) "true" }}
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: {{ include "waddleai.fullname" . }}-securitycontext
  labels:
    {{- include "waddleai.labels" . | nindent 4 }}
spec:
  failurePolicy: {{ .Values.admissionPolicies.failurePolicy }}
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources: ["pods"]
  validations:
    {{- if .Values.admissionPolicies.validatingAdmissionPolicy.enforceNonRoot }}
    - expression: "object.spec.securityContext.runAsNonRoot == true || object.spec.containers.all(c, has(c.securityContext) && c.securityContext.runAsNonRoot == true)"
      message: "WaddleAI workloads must run as non-root (runAsNonRoot: true)"
    {{- end }}
    {{- if .Values.admissionPolicies.validatingAdmissionPolicy.enforceReadOnlyRootFs }}
    - expression: "object.spec.containers.all(c, has(c.securityContext) && c.securityContext.readOnlyRootFilesystem == true)"
      message: "WaddleAI containers must set readOnlyRootFilesystem: true"
    {{- end }}
    {{- if .Values.admissionPolicies.validatingAdmissionPolicy.enforceDropAllCaps }}
    - expression: "object.spec.containers.all(c, has(c.securityContext) && has(c.securityContext.capabilities) && c.securityContext.capabilities.drop.exists(d, d == 'ALL'))"
      message: "WaddleAI containers must drop ALL capabilities"
    {{- end }}
    {{- if .Values.admissionPolicies.validatingAdmissionPolicy.enforceNoPrivilegeEscalation }}
    - expression: "object.spec.containers.all(c, has(c.securityContext) && c.securityContext.allowPrivilegeEscalation == false)"
      message: "WaddleAI containers must set allowPrivilegeEscalation: false"
    {{- end }}
    - expression: "!has(object.spec.volumes) || object.spec.volumes.all(v, !has(v.hostPath))"
      message: "WaddleAI workloads must not mount hostPath volumes"
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: {{ include "waddleai.fullname" . }}-securitycontext-binding
spec:
  policyName: {{ include "waddleai.fullname" . }}-securitycontext
  validationActions: {{ .Values.admissionPolicies.validationActions | toJson }}
  matchResources:
    namespaceSelector:
      matchExpressions:
        - key: kubernetes.io/metadata.name
          operator: In
          values: ["{{ .Values.namespace }}"]
    objectSelector:
      matchLabels:
        {{- include "waddleai.workloadSelectorLabels" . | nindent 8 }}
{{- end }}
```

- [ ] **Step 2: Record + verify** VAP renders only when the API is available, actions honor env profile:

```bash
cd /home/penguin/code/waddleai
GOLDEN_RECORD=1 bash tests/helm/render-golden.sh
grep -q "kind: ValidatingAdmissionPolicy" tests/helm/golden/full-present.yaml && echo "VAP rendered"
grep -q '"Warn"' tests/helm/golden/admission-only.yaml || grep -q '"Deny"' tests/helm/golden/full-present.yaml && echo "actions bound"
grep -q "ValidatingAdmissionPolicy" tests/helm/golden/nocrd-defaults.yaml && echo "LEAKED" || echo "absent without VAP API (correct)"
helm lint k8s/helm/waddleai
```

- [ ] **Step 3: Commit**

```bash
git add k8s/helm/waddleai/templates/admission-securitycontext.yaml tests/helm/golden/
git commit -m "feat(helm): ValidatingAdmissionPolicy enforcing non-root/RORFS/drop-caps/no-privesc/no-hostPath

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task C2: digest-pinned-image VAP + optional Kyverno alternative

**Files:** Create `templates/admission-image-digest.yaml`; regenerate golden files.

- [ ] **Step 1:** Create `templates/admission-image-digest.yaml` with **two independently-gated** blocks:
  - a `ValidatingAdmissionPolicy` + binding (gated by `waddleai.vap.render` **and** `enforceDigestPinnedImages`) whose CEL requires every container image to contain an `@sha256:` digest — `object.spec.containers.all(c, c.image.contains('@sha256:'))` — scoped to WaddleAI workloads;
  - an optional Kyverno `ClusterPolicy` (gated by `waddleai.kyverno.render`) expressing the same digest-pin + non-root rules for operators who standardize on Kyverno instead of core VAP. Kyverno objects render **only** when explicitly opted-in and its CRDs are present.

- [ ] **Step 2: Record + verify** both engines and the opt-in/absent matrix:

```bash
cd /home/penguin/code/waddleai
GOLDEN_RECORD=1 bash tests/helm/render-golden.sh
grep -q "@sha256:" tests/helm/golden/full-present.yaml && echo "digest VAP rendered"
grep -q "kind: ClusterPolicy" tests/helm/golden/kyverno-present.yaml && echo "kyverno rendered when present+opted-in"
grep -q "kind: ClusterPolicy" tests/helm/golden/kyverno-absent.yaml && echo "LEAKED" || echo "kyverno absent when CRDs missing (correct)"
helm lint k8s/helm/waddleai
```

- [ ] **Step 3: Commit**

```bash
git add k8s/helm/waddleai/templates/admission-image-digest.yaml tests/helm/golden/
git commit -m "feat(helm): digest-pinned-image VAP + optional Kyverno ClusterPolicy alternative

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task Group D — CRD/Agent Bootstrap & Graceful Status (§12.3)

### Task D1: Optional Tetragon dependency + NOTES.txt detect/offer/no-op status

**Files:** Modify `Chart.yaml`; Create `templates/NOTES.txt`; regenerate golden files.

- [ ] **Step 1: Optional Tetragon sub-chart dependency.** Add to `Chart.yaml` `dependencies` a condition-gated Tetragon chart (from the Cilium Helm repo), **disabled by default** so it is opt-in per §12.3 (`condition: tetragon.install.dependency`). Pin the chart `version` exactly (house dependency-pinning rule). Note in a comment that operators run `helm dep update` after opting in:

```yaml
  # Optional runtime-security agent — opt-in via tetragon.install.dependency=true
  # then `helm repo add cilium https://helm.cilium.io && helm dep update`.
  - name: tetragon
    version: "1.3.0"
    repository: "https://helm.cilium.io"
    condition: tetragon.install.dependency
```

- [ ] **Step 2: Bootstrap/status messaging.** Create `templates/NOTES.txt` that, using the capability helpers, tells the operator exactly what happened:
  - Tetragon present + enabled → "Runtime protections active (exec: enforce/observe, egress: observe)".
  - Tetragon enabled but CRDs absent → a clear **no-op status** ("Tetragon CRDs not found — runtime policies were skipped") **plus the install offer** (when `tetragon.install.offer`): the `helm dep update`/`--set tetragon.install.dependency=true` path or the documented standalone install command.
  - VAP API absent → "ValidatingAdmissionPolicy API unavailable (cluster < 1.30 or feature-gate off) — admission enforcement skipped."
  - All classes off → "Cilium runtime/admission protections disabled by values."

- [ ] **Step 3: Verify the graceful path renders a status, never an error.** `helm template` on a bare cluster must succeed (exit 0) and print the skip notice:

```bash
cd /home/penguin/code/waddleai
helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/values.yaml 1>/dev/null && echo "renders clean on non-Cilium"
helm template waddleai k8s/helm/waddleai --set tetragon.install.dependency=true --api-versions cilium.io/v1alpha1/TracingPolicyNamespaced 1>/dev/null && echo "dependency-opt-in renders"
GOLDEN_RECORD=1 bash tests/helm/render-golden.sh
helm lint k8s/helm/waddleai
```

- [ ] **Step 4: Commit**

```bash
git add k8s/helm/waddleai/Chart.yaml k8s/helm/waddleai/templates/NOTES.txt tests/helm/golden/
git commit -m "feat(helm): opt-in Tetragon dependency + NOTES.txt bootstrap/no-op status (§12.3)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task Group E — kind e2e (§12.4)

### Task E1: Tetragon policy effective in kind (exec blocked, egress observed)

**Files:** Create `tests/helm/test_tetragon_admission_kind.sh` (Tetragon portion).

- [ ] **Step 1:** Write the kind bring-up portion of `tests/helm/test_tetragon_admission_kind.sh`: create a kind cluster with Cilium + Tetragon installed (or install the Tetragon agent via the opt-in dependency), `helm install` the chart with `tetragon.exec.mode=enforce`, deploy a stub pod carrying the WaddleAI workload labels + `component: proxy`.

- [ ] **Step 2: Assert exec is blocked** — `kubectl exec` into the labeled stub pod must fail (Sigkill), and the Tetragon event log records the exec attempt. Assert egress observe emits a `process_connect` event for an outbound connection. Clean up the cluster on exit (trap).

```bash
# exec into a protected pod must be killed under enforce mode
kubectl exec deploy/waddleai-proxy-stub -- /bin/sh -c 'echo hi' && { echo "FAIL: exec allowed"; exit 1; } || echo "exec blocked (correct)"
kubectl logs -n kube-system ds/tetragon -c export-stdout | grep -q "process_exec" && echo "exec event captured"
```

- [ ] **Step 3: Run + commit**

```bash
cd /home/penguin/code/waddleai
make test-tetragon-kind   # Tetragon assertions green (skips gracefully if kind unavailable in CI tier)
git add tests/helm/test_tetragon_admission_kind.sh Makefile
git commit -m "test(helm): kind e2e — Tetragon exec-block enforced, egress observed

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task E2: Admission negative test + CRD-absent graceful path

**Files:** Modify `tests/helm/test_tetragon_admission_kind.sh` (admission + graceful portions).

- [ ] **Step 1: Admission negative tests** (on a kind cluster ≥1.30 with VAP feature enabled, `validationActions: [Deny]`, `failurePolicy: Fail`): applying a pod that violates policy is **rejected** by the API server; a compliant pod is admitted:

```bash
# root pod rejected
kubectl apply -f tests/helm/fixtures/root-pod.yaml 2>&1 | grep -q "denied\|must run as non-root" && echo "root pod rejected (correct)"
# hostPath pod rejected
kubectl apply -f tests/helm/fixtures/hostpath-pod.yaml 2>&1 | grep -q "denied\|hostPath" && echo "hostPath pod rejected (correct)"
# non-digest image rejected
kubectl apply -f tests/helm/fixtures/mutable-tag-pod.yaml 2>&1 | grep -q "denied\|@sha256" && echo "mutable-tag pod rejected (correct)"
# compliant pod admitted
kubectl apply -f tests/helm/fixtures/compliant-pod.yaml && echo "compliant pod admitted (correct)"
```

Create the four fixture pods under `tests/helm/fixtures/` (all carrying WaddleAI workload labels so the binding selects them).

- [ ] **Step 2: CRD-absent graceful deploy** — on a plain kind cluster **without** Cilium/Tetragon and (optionally) a <1.30 node, `helm install` succeeds, all first-party workloads become Ready, and **no** policy CRDs are created (the protections no-op with the NOTES status):

```bash
helm install waddleai k8s/helm/waddleai -f k8s/helm/waddleai/values-alpha.yaml --wait --timeout 5m && echo "installs on bare cluster"
kubectl get tracingpoliciesnamespaced.cilium.io -A 2>&1 | grep -q "No resources\|not found\|the server doesn't have" && echo "no Tetragon CRs (graceful)"
```

- [ ] **Step 3: RBAC least-privilege note** — the cilium-reconciler branch owns the reconciler ServiceAccount; confirm **this** branch adds no cluster-admin grants (the chart-shipped CRDs are static manifests, applied by Helm, needing no runtime SA). Assert no new `ClusterRoleBinding` to `cluster-admin` is introduced:

```bash
grep -rn "cluster-admin" k8s/helm/waddleai/templates/ && echo "REVIEW" || echo "no cluster-admin grants added (correct)"
```

- [ ] **Step 4: Run full kind e2e + commit**

```bash
cd /home/penguin/code/waddleai
make test-tetragon-kind
git add tests/helm/test_tetragon_admission_kind.sh tests/helm/fixtures/
git commit -m "test(helm): admission negative tests + CRD-absent graceful deploy path

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task F — Acceptance Gate (§12.4)

**Files:** none (verification + a residue-fix commit only if needed).

- [ ] **Step 1: Golden combination matrix green** (the merge gate):

```bash
cd /home/penguin/code/waddleai
bash tests/helm/render-golden.sh && echo "all golden combinations stable"
helm lint k8s/helm/waddleai
```

- [ ] **Step 2: Non-Cilium render is empty of policy CRDs** (graceful no-op proof):

```bash
grep -c "kind: TracingPolicyNamespaced\|kind: ValidatingAdmissionPolicy\|kind: ClusterPolicy" tests/helm/golden/nocrd-defaults.yaml
# expected: 0
```

- [ ] **Step 3: Per-toggle independence** — each class renders in isolation without the others:

```bash
helm template waddleai k8s/helm/waddleai --set admissionPolicies.enabled=false --api-versions cilium.io/v1alpha1/TracingPolicyNamespaced | grep -q TracingPolicyNamespaced && echo "tetragon-only OK"
helm template waddleai k8s/helm/waddleai --set tetragon.enabled=false --api-versions admissionregistration.k8s.io/v1/ValidatingAdmissionPolicy | grep -q ValidatingAdmissionPolicy && echo "admission-only OK"
```

- [ ] **Step 4: kind e2e green** — Tetragon exec-block effective, admission negative tests reject root/hostPath/mutable-tag pods, compliant pod admitted, CRD-absent deploy graceful:

```bash
make test-tetragon-kind
```

- [ ] **Step 5: No duplication of the cilium-reconciler branch** — this branch must not emit rate-limit CiliumEnvoyConfig or the CNP reconciler objects (those are §12.1):

```bash
grep -rn "CiliumEnvoyConfig\|kind: CiliumNetworkPolicy" k8s/helm/waddleai/templates/ && echo "DUPLICATION — remove" || echo "no §12.1 overlap (correct)"
```

- [ ] **Step 6: Final commit (only if a residue fix was required)**

```bash
git add -A
git commit -m "chore: wave4 tetragon+admission acceptance — golden matrix stable, graceful no-op, kind e2e green

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review Against Spec §12.2/§12.3/§12.4

| Spec requirement | Task |
|---|---|
| §12.2 Tetragon TracingPolicies block/observe exec into AIProxy + fleet pods | B2 |
| §12.2 Egress allow-list / observe from AIProxy to provider endpoints (CIDR; FQDN left to CNP) | B3 |
| §12.2 Flag unexpected file/network activity | B3 |
| §12.2 Admission policies enforce rootless / non-root UID / readOnlyRootFilesystem / dropped caps | C1 |
| §12.2 Admission enforces digest-pinned images | C2 |
| §12.2 ValidatingAdmissionPolicy core + Kyverno optional | C1, C2 |
| §12.2 Scoped to WaddleAI workloads only | A1 (workload selector), C1, C2 |
| §12.2 ON by default, per-class Helm values switches | A1 |
| §12.3 Detect missing Tetragon CRDs/agent; render clean; no-op with clear status | A1 (capability gate), D1 (NOTES status) |
| §12.3 Offer install as opt-in Helm dependency / documented step | D1 (Chart.yaml condition + NOTES offer) |
| §12.3 `helm template` renders cleanly with every protection class independently toggled | A1, F3 |
| §12.4 `helm template` golden files per toggle combination | A1 harness, B2/B3/C1/C2/D1 golden regen, F1 |
| §12.4 kind deploy: Tetragon policy effective | E1 |
| §12.4 kind: admission negative test rejects root/hostPath pod | E2 |
| §12.4 CRD-absent path renders and deploys without Cilium CRDs | E2, F2 |
| §12.4 RBAC least-privilege on reconciler ServiceAccount | E2 (no new grants; SA owned by cilium-reconciler branch) |
| §14.1 Branch `chore/tetragon-admission-policies`, depends on cilium-reconciler, merges to `release/v0.2.X` | header |
| §12.1 NOT duplicated (rate-limit CEC + CNP reconciler are the prior branch) | header, F5 |
| §14.2 Values-gated infra hardening — golden files are the gate (no PostHog flag) | Flag posture note |
