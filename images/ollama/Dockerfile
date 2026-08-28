# Hardened Ollama image (spec §10.2, plan Task 13).
#
# Re-bases the upstream ollama/ollama binary + bundled GPU backend libraries
# onto a minimal Debian runtime, rather than rebuilding from source (CGO +
# CUDA/ROCm/Vulkan toolchains) — spec §10.2 calls for a "digest-pinned
# upstream base", not a from-source rebuild.
#
# Version: 0.32.13 (upstream tags queried 2026-08-14; latest non-RC stable).
# NOT 0.7.1, which is what k8s/helm/waddleai/values.yaml's plain
# ollama.image.* still points at pre-Task-14 — 0.7.1 carries HIGH/CRITICAL
# Go-stdlib CVEs (net/net-http DoS + idna privilege escalation, confirmed via
# `trivy image` against a locally built hardened:0.7.1) that a "Trivy-gated"
# image cannot ship with. 0.32.13 scans clean at HIGH/CRITICAL as of this
# writing. Task 14 repoints the chart's ollama.image.* at this image, which
# is what actually retires the stale 0.7.1 pin.
#
# Base OS note: upstream 0.7.1 was Ubuntu 20.04/glibc 2.31 and ran fine
# re-hosted on debian:bookworm-slim (glibc 2.36, forward-compatible). 0.32.13
# upstream moved to Ubuntu 24.04/glibc 2.39 — NEWER than bookworm's 2.36 — so
# bookworm-slim is no longer a valid re-host target (missing GLIBC_2.38+
# symbol versions at runtime). debian:trixie-slim (glibc 2.41) is used
# instead, per the documented Debian 12 -> 11 -> 13 -> Ubuntu LTS fallback
# order for exactly this situation.
#
# Two final targets from one Dockerfile (`docker build --target hardened|debug`):
#   hardened (default) - no shell, no package manager, minimal attack surface
#   debug              - adds bash/coreutils/procps/curl for `kubectl exec` triage
#
# GPU access (NVIDIA/AMD/Intel) comes entirely from the Kubernetes device
# plugin + container runtime (nvidia-container-toolkit etc.) mounting driver
# libs at runtime — no GPU vendor SDK is baked into this image, matching
# upstream's own approach (LD_LIBRARY_PATH=/usr/local/nvidia/lib*).

# ---------------------------------------------------------------------------
# Stage 1: pinned upstream — source of the ollama binary + backend libraries.
# ---------------------------------------------------------------------------
FROM ollama/ollama:0.32.13@sha256:77f1a2a54460f0380f2611e1464233d9b82cb6e58afc8f60abec0061049d2d82 AS upstream

# ---------------------------------------------------------------------------
# Stage 2: runtime-base — shared by both final targets.
# ---------------------------------------------------------------------------
FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS runtime-base

# ca-certificates + libvulkan1 + libopenblas0 mirror upstream's own apt-get
# install list for this exact release (read via `docker buildx imagetools
# inspect ollama/ollama:0.32.13`'s build history) — Vulkan/OpenBLAS backends
# ggml dlopen()s at runtime, invisible to `ldd` on the main binary.
# libstdc++6 + libgomp1 are additionally required (confirmed via
# `ldd /usr/bin/ollama` against the upstream binary) but not in upstream's
# own list because Ubuntu's full (non-slim) base ships them by default.
# Versions pinned to the current trixie candidates for this exact base
# digest (`apt-cache policy` against the pinned image above) per house rule.
#
# bsdutils..util-linux below are NOT new dependencies — they are already part
# of debian:trixie-slim's base rootfs at 2.41-5, which carries CVE-2026-53612/
# -53613/-53614 (util-linux TOCTOU/SUID mount(8) bypass, Trivy: HIGH, status
# "fixed"). Unlike the CVEs in .trivyignore, a patched version
# (2.41.5-0+deb13u1, "login" is 1:4.16.0-2+really2.41.5-0+deb13u1) is already
# published on trixie-security (confirmed via `apt-cache policy` against this
# exact base digest — trixie-security is enabled by default, no extra source
# needed), so this pins an explicit upgrade at build time rather than
# ignoring the finding. Package list matches exactly what Trivy reported
# against the bare base image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates=20250419 \
        libvulkan1=1.4.309.0-1 \
        libopenblas0=0.3.29+ds-3 \
        libstdc++6=14.2.0-19 \
        libgomp1=14.2.0-19 \
        bsdutils=1:2.41.5-0+deb13u1 \
        libblkid1=2.41.5-0+deb13u1 \
        liblastlog2-2=2.41.5-0+deb13u1 \
        libmount1=2.41.5-0+deb13u1 \
        libsmartcols1=2.41.5-0+deb13u1 \
        libuuid1=2.41.5-0+deb13u1 \
        login=1:4.16.0-2+really2.41.5-0+deb13u1 \
        mount=2.41.5-0+deb13u1 \
        util-linux=2.41.5-0+deb13u1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user, UID/GID 1000 (regular range, not --system — 1000 is above
# Debian's SYS_UID_MAX and matches k8s/helm/waddleai/values.yaml's
# podSecurityContext fsGroup/runAsUser 1000, so PVC file ownership lines up
# without an fsGroup mismatch).
RUN groupadd --gid 1000 ollama \
    && useradd --uid 1000 --gid ollama --home-dir /home/ollama \
        --shell /usr/sbin/nologin --no-create-home ollama \
    && mkdir -p /home/ollama /models \
    && chown -R ollama:ollama /home/ollama /models

COPY --from=upstream /bin/ollama /usr/bin/ollama
COPY --from=upstream /usr/lib/ollama /usr/lib/ollama

ENV OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_MODELS=/models \
    HOME=/home/ollama \
    PATH=/usr/bin:/bin

# Values sourced from the chart's readOnlyRootFilesystem: true containers
# (k8s/helm/waddleai/templates/ollama-daemonset.yaml) — the model-store PVC
# and a tmp emptyDir are the only writable mounts at runtime; nothing under
# this image's own filesystem needs to be writable.
WORKDIR /home/ollama
EXPOSE 11434

# Native binary healthcheck (no curl/wget) — `ollama list` calls the local
# API, so it only succeeds once `ollama serve` is actually accepting requests.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["/usr/bin/ollama", "list"]

USER ollama
ENTRYPOINT ["/usr/bin/ollama"]
CMD ["serve"]

# ---------------------------------------------------------------------------
# Stage 3: hardened (default target) — no shell, no package manager.
#
# The initContainer pull path (k8s/helm/waddleai/templates/ollama-daemonset.yaml)
# never uses `/bin/sh -c`: it runs `ollama serve` as a native K8s 1.29+
# sidecar initContainer (restartPolicy: Always) and `ollama pull <model>` as
# plain initContainers, both invoking this image's binary directly. That is
# what makes a genuinely shell-less "hardened" tag possible for the pull path
# too, not just for `serve`.
# ---------------------------------------------------------------------------
FROM runtime-base AS hardened
USER root
# Remove shells and the apt/dpkg *binaries* (no runtime package installs
# possible) but deliberately keep /var/lib/dpkg — it's inert metadata, not an
# attack surface, and image scanners (Trivy et al.) need it to enumerate
# installed packages. Deleting it doesn't hardened anything; it just blinds
# the scanner into reporting a false "0 packages" clean result.
RUN find / -xdev -type f \( \
        -name 'sh' -o -name 'bash' -o -name 'dash' -o -name 'rbash' \
    \) -exec rm -f {} + 2>/dev/null; \
    rm -rf /usr/bin/apt* /usr/bin/dpkg* /var/cache/apt
USER ollama

# ---------------------------------------------------------------------------
# Stage 4: debug — adds a shell + basic tooling for `kubectl exec` triage.
# Never used by the chart's normal DaemonSet/Deployment/initContainer path —
# operators switch a pod to this tag manually when troubleshooting.
# ---------------------------------------------------------------------------
FROM runtime-base AS debug
USER root
# curl deliberately excluded — spec §10.2's stated purpose for this tag is
# "adds a shell for kubectl exec troubleshooting", and curl pulls in
# libcurl4t64/libssh2 with their own unfixed-upstream CVEs (see
# .trivyignore) for no capability this tag actually needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash=5.2.37-2+b9 \
        coreutils=9.7-3 \
        procps=2:4.0.4-9 \
    && rm -rf /var/lib/apt/lists/*
USER ollama
