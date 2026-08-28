# Docker Compose Deployment

!!! danger "Deprecated — Docker Compose is not a supported deployment path"
    Docker Compose is deprecated house-wide for every environment, including local/alpha
    development. There is no supported Docker Compose deployment of WaddleAI. Use
    [Kubernetes via Helm](kubernetes.md) instead — it covers alpha (local MicroK8s),
    beta, and the required `k8s/helm/waddleai` chart configuration.

## Why this page still exists

This page is kept (rather than removed) so the "Deployment → Docker Compose" link in
the docs nav and any existing inbound links resolve instead of 404ing. It intentionally
carries no compose instructions — the previous version of this page documented
`docker-compose up -d` against files that don't exist in this repository (no
`docker-compose.yml` ships at the repository root).

## Where a `docker-compose.yml` is still generated

The only place a Compose file is still produced anywhere in this repository is CI: the
`integration-test` job in `.github/workflows/docker-build.yml` writes an ephemeral
`docker-compose.test.yml` on the fly to smoke-test freshly built `proxy` and
`management` images against Postgres and Redis containers, then tears it down
(`docker compose -f docker-compose.test.yml down -v`) at the end of the job. That file:

- is generated inside the CI runner, never committed to the repository
- exists purely to give the CI job a quick way to exercise the built container images
  end-to-end (health endpoints, an unauthenticated `/v1/chat/completions` 401 check)
- is not a deployment path, has no relationship to Helm values, secrets, or any
  environment configuration used in alpha/beta/prod, and should not be copied or
  adapted for that purpose

## What to use instead

See [Kubernetes Deployment](kubernetes.md) for the real, supported path:

- Prerequisites and cluster contexts
- Quick install for local/alpha (`scripts/deploy-alpha.sh`, MicroK8s)
- Beta deployment (`scripts/deploy-beta.sh`, `dal2-beta`)
- Required secrets and configuration values
- First login and creating your first API key
- Upgrades, rollback, and troubleshooting
