# Hardened Ollama image

`ghcr.io/penguintechinc/waddleai/ollama` — spec §10.2, plan Task 13.

Re-bases the upstream `ollama/ollama` binary + bundled GPU backend libraries
onto a minimal Debian runtime with a non-root user, dropped capabilities
(enforced via the Helm chart's `securityContext`, not baked into the image),
and — on the `hardened` tag — no shell at all. See the Dockerfile's top
comment for the full base-image/version rationale.

## Tags

| Tag | Purpose | Shell | Used by |
|---|---|---|---|
| `hardened` | Normal serving + model-pull path | None | `ollama-daemonset.yaml` / `ollama-deployment.yaml`, both `serve` and `pull` initContainers |
| `debug` | `kubectl exec` troubleshooting only | bash + coreutils + procps (no curl) | Manual operator use — never referenced by the chart |

Both targets build from one Dockerfile:

```bash
docker build --target hardened -t ghcr.io/penguintechinc/waddleai/ollama:hardened .
docker build --target debug    -t ghcr.io/penguintechinc/waddleai/ollama:debug    .
```

(Build context is the repo root only if you add repo-relative COPYs later;
today the Dockerfile has none, so `images/ollama` also works as context.)

## Why no `/bin/sh -c` anywhere in the pull path

`hardened` has no shell, so the old `ollama serve & sleep 5 && ollama pull
... && kill` one-liner is impossible. The chart instead uses a Kubernetes
1.29+ **native sidecar** initContainer (`restartPolicy: Always`) running
`ollama serve`, followed by one plain initContainer per model running
`ollama pull <model>` directly against `localhost:11434` — no shell
involved at any point. See `k8s/helm/waddleai/templates/ollama-daemonset.yaml`.
**This raises the cluster's minimum Kubernetes version to 1.29.**

## Verifying locally

```bash
# Structure — separate configs because the shell assertion is inverted:
container-structure-test test --image ghcr.io/penguintechinc/waddleai/ollama:hardened \
  --config images/ollama/structure-test.yaml
container-structure-test test --image ghcr.io/penguintechinc/waddleai/ollama:debug \
  --config images/ollama/debug-structure-test.yaml

# Vulnerability gate (see .trivyignore for what's excluded and why):
trivy image --severity HIGH,CRITICAL --exit-code 1 \
  --ignorefile images/ollama/.trivyignore \
  ghcr.io/penguintechinc/waddleai/ollama:hardened
```

## `.trivyignore`

Two categories only, both documented inline in the file itself:

1. Go-module CVEs embedded in the vendored `ollama` binary's build info —
   fixable only by an upstream release or a from-source rebuild (out of
   scope per spec §10.2).
2. `debian:trixie-slim` baseline package CVEs with no Debian-published fix
   yet (`perl-base`, `util-linux`, etc.) — confirmed identical on
   `debian:bookworm-slim`, so not something this image's base choice
   introduced.

Re-evaluate on every version/base bump; do not carry entries forward blindly.
