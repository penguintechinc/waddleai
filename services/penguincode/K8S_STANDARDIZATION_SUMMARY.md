# PenguinCode Kubernetes Standardization - Summary

**Superseded.** This document originally summarized a hybrid Helm + Kustomize
setup (`k8s/manifests/`, `k8s/kustomize/`). Kustomize and the raw manifest
directory have been retired -- **Helm v4 is the only supported deployment
method**, per PenguinTech's Helm-only rule (no Kustomize, no raw `kubectl
apply -f` manifest dirs).

## Current State

- Chart: `k8s/helm/penguincode/`
- Values files: `values.yaml` (Helm-implicit default) + `alpha.yml` /
  `beta.yml` / `gamma.yml` / `production.yml` (env overrides -- no
  `values-` prefix, `.yml` not `.yaml`)
- Namespace: `penguincode` in every environment -- the environment lives in
  the values filename, never the namespace name
- Deploy: `make k8s-alpha-deploy` / `make k8s-beta-deploy` /
  `make k8s-prod-deploy`, or `./scripts/deploy-beta.sh` for beta CI/CD

See `docs/k8s-deployment.md` for the current Helm-only deployment guide and
`docs/k8s-quick-commands.md` / `docs/k8s-deployment-checklist.md` for
day-to-day command references.
