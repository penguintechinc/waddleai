# Wave 5 — Control-Plane Follow-ons

**Depends on:** Waves 1–4, especially fleet (`013_fleet`), MCP integrations
(`014_integrations`), agent hooks (`016_hooks`), and their feature gates.

## Goal

Deliver the bounded follow-ons intentionally excluded from Wave 4: managed MCP
deployment contracts, approved Hugging Face artifact intake, declarative
configuration as code (DAC), and the remaining agent-hook harness contract.
This is a management-plane release: it does **not** add a GitOps controller,
Kubernetes CRD, autonomous pruning, arbitrary executable hooks, or inline
secrets.

## Safety invariants

- Managed MCP images must be immutable digest references. A caller cannot set
  service-account permissions, host networking, or an arbitrary namespace.
- Hugging Face inputs are repository/revision/file/digest metadata plus a
  credential reference only. `hf_*` values, bearer tokens, and inline secrets
  are rejected before persistence or logging.
- DAC defaults to dry-run. Applying requires an unexpired, content-bound plan
  identifier. No prune/delete operation exists in v1alpha1.
- Hook policy stays declarative. The existing server-side security engine is
  the only remote evaluator; adapters receive a stable flat response.

## Tasks

- [x] **Managed MCP lifecycle foundation.** Added a strict `ManagedMcpSpec`, immutable-image
  and namespace/network validation, an injected deployment-driver lifecycle,
  readiness/discovery transitions, endpoint ownership, cleanup, Kubernetes
  in-cluster driver, persistence schema, and Management CRUD. The operator
  supplies the restricted service-account permissions, approved namespaces,
  and SDK tool-discovery adapter.
- [x] **Approved Hugging Face artifact foundation.** Added a descriptor validator and a
  download-plan contract that requires a credential reference when private,
  exact repo revision and SHA-256 file digest, approved destination, quota,
  licence/origin acknowledgement, format validation, and explicit revocation.
- [x] **DAC v1alpha1 core + API/CLI.** Added strict YAML/JSON parsing, secret rejection,
  deterministic create/update/no-op planning, plan digests and expiry, and an
  explicit apply executor, Management API, and CLI wiring. Production
  composition supplies the canonical resource lookup/executor before the
  flag is enabled; reviewed plans and audit outcomes now persist through
  `configuration_revisions` and `manifest_apply_runs`.
- [x] **Hook response-contract completion.** Return the full §18.1 response fields,
  with redirect/active-skill fields explicitly null until a trusted
  role-model policy mapper is wired; default harness configuration remains
  disabled.

## Acceptance gates

1. Mutable images, non-allowlisted namespaces, privileged runtime settings,
   inline HF/DAC secrets, unpinned artifacts, unknown fields, stale plans, and
   prune requests are rejected.
2. A fake deployment driver proves managed-MCP provision → readiness/discovery
   → cleanup without requiring a cluster.
3. DAC produces byte-stable plans, never mutates during dry-run, and applies
   only the exact reviewed plan.
4. Hook flag-off remains unconditional allow; Tier-1 denylist remains an
   offline/server floor and cannot be weakened by dynamic recommendations.
5. Before release: migration round-trip, complete unit/integration suites,
   hardened-image/cluster validation, and beta validation with real provider
   credentials and agent clients.
