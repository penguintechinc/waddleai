# vLLM-on-Ray worker image, AMD/ROCm (A7, AM20). Rootless (uid 1000); KubeRay supplies the start
# command, so there is no ENTRYPOINT/CMD override.
#
# AM20: no rocm/vllm tag pairs vLLM 0.32 with Python 3.12, so vLLM is BUILT FROM SOURCE at exactly the
# same version as the CUDA image (rayproject/ray-llm:2.60.0-py312-cu130 -> vLLM 0.32.0):
#   vLLM tag v0.32.0 = commit 5df63a7203cdb9328d824a27b682e77c7b9928be
#   reference: docker/Dockerfile.rocm + docker/Dockerfile.rocm_base + requirements/rocm.txt at that commit.
# DEVIATION from the reference (recorded in DEVIATIONS-A.md): vLLM's rocm_base compiles a patched
# ROCm/PyTorch from source; here the official PyTorch ROCm wheel (torch 2.13.0+rocm7.2, the exact torch
# vLLM 0.32.0 pins) is installed instead. Needs GPU validation on a real ROCm node (cannot run in CI).
#
# Base pin (vLLM's own rocm_base BASE_IMAGE), digest verified with
#   docker buildx imagetools inspect rocm/dev-ubuntu-22.04:7.2.3-complete \
#     --format '{{json .Manifest}}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["digest"])'
# Python packages are hash-pinned: requirements.txt / build-requirements.txt are generated with
#   uv pip compile <name>.in --python-version 3.12 --python-platform x86_64-manylinux_2_35 \
#     --generate-hashes --emit-index-url --index-strategy unsafe-best-match [--no-emit-package triton, requirements only] -o <name>.txt
# (xgrammar's PyPI `triton` is dropped: torch's `triton-rocm` already provides the `triton` module.)
# ray[llm] is spelled out in requirements.in without vllm and the CUDA-only nixl wheels (see that file).
# Ray and the Python minor MUST equal the head image (rayproject/ray:2.60.0-py312): the build asserts it.
# The Python env lives at /home/ray/anaconda3 so every Ray image shares one binary layout (A6 allowlist).

ARG ROCM_BASE=rocm/dev-ubuntu-22.04:7.2.3-complete@sha256:b64aecbed6cc2d8227407899831d5c53fa8450d4db08f6a2400c29587ce15be9
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa

# hadolint ignore=DL3006
FROM ${UV_IMAGE} AS uv

# ---- deps: Python 3.12 venv with every hash-pinned dependency (torch ROCm, Ray, vLLM's requirements) ----
# hadolint ignore=DL3006
FROM ${ROCM_BASE} AS deps
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG PYTHON_VERSION=3.12
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python UV_LINK_MODE=copy UV_INDEX_STRATEGY=unsafe-best-match UV_PYTHON_PREFERENCE=only-managed \
    VIRTUAL_ENV=/home/ray/anaconda3 PATH=/home/ray/anaconda3/bin:/opt/rocm/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    ROCM_PATH=/opt/rocm LD_LIBRARY_PATH=/opt/rocm/lib:/usr/local/lib
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /tmp/deps
COPY requirements.txt ./
RUN useradd --uid 1000 --create-home --home-dir /home/ray --shell /bin/bash ray \
    && uv venv --python "${PYTHON_VERSION}" --seed "${VIRTUAL_ENV}" \
    && uv pip install --no-cache --require-hashes --no-deps -r requirements.txt \
    && chmod -R a+rX /opt/uv /home/ray/anaconda3

# ---- build: compile the vLLM ROCm wheel (never shipped) ----
FROM deps AS build
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG VLLM_COMMIT=5df63a7203cdb9328d824a27b682e77c7b9928be
ARG VLLM_VERSION=0.32.0
ARG RUST_TOOLCHAIN=1.95
# Default covers MI200/MI300/MI350 + RDNA3/4; narrow it to shorten the build for a known fleet.
ARG PYTORCH_ROCM_ARCH="gfx90a;gfx942;gfx950;gfx1100;gfx1101;gfx1200;gfx1201"
ENV VLLM_TARGET_DEVICE=rocm PYTORCH_ROCM_ARCH=${PYTORCH_ROCM_ARCH} CARGO_HOME=/opt/cargo RUSTUP_HOME=/opt/rustup \
    PATH=/opt/cargo/bin:${PATH} SETUPTOOLS_SCM_PRETEND_VERSION=${VLLM_VERSION}
COPY build-requirements.txt /tmp/build-requirements.txt
# hadolint ignore=DL3008
RUN apt-get update -y && apt-get install -y --no-install-recommends git=1:2.34.1-1ubuntu1.18 curl=7.81.0-1ubuntu1.30 \
    ca-certificates=20260816ubuntu0.22.04.2 mold=1.0.3+dfsg-2 \
    && rm -rf /var/lib/apt/lists/* \
    && uv pip install --no-cache --require-hashes --no-deps -r /tmp/build-requirements.txt
WORKDIR /src/vllm
RUN git init -q . && git fetch -q --depth 1 https://github.com/vllm-project/vllm.git "${VLLM_COMMIT}" \
    && git checkout -q FETCH_HEAD && test "$(git rev-parse HEAD)" = "${VLLM_COMMIT}"
ARG RUSTUP_VERSION=1.29.3
ARG RUSTUP_SHA256_X86_64=b1c8cb14e30889e7e2999e6b69e62d9e9b9ffc0f4df8180564d306d9680e6c55
ARG RUSTUP_SHA256_AARCH64=9f608c142e0e38a78957d0661ba1dfaf0b6fb37184cfaa4b9a0aa57af6184282
# Rust toolchain: exact 1.95, the channel in vLLM v0.32.0 rust-toolchain.toml (asserted below). rustup-init is a
# pinned-version binary from static.rust-lang.org, sha256-verified (official .sha256 files), never `curl | sh`.
RUN ARCH="$(uname -m)" \
    && case "${ARCH}" in x86_64) SUM="${RUSTUP_SHA256_X86_64}";; aarch64) SUM="${RUSTUP_SHA256_AARCH64}";; *) echo "unsupported arch ${ARCH}" >&2; exit 1;; esac \
    && curl --proto '=https' --tlsv1.2 -fsSLo /tmp/rustup-init "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/${ARCH}-unknown-linux-gnu/rustup-init" \
    && echo "${SUM}  /tmp/rustup-init" | sha256sum -c - \
    && chmod +x /tmp/rustup-init \
    && /tmp/rustup-init -y --default-toolchain "${RUST_TOOLCHAIN}" --profile minimal --no-modify-path \
    && rm /tmp/rustup-init \
    && grep -q "^channel *= *\"${RUST_TOOLCHAIN}\"" rust-toolchain.toml \
    && CARGO_BUILD_JOBS=4 bash tools/build_rust.sh && test -x vllm/vllm-rs
RUN LDFLAGS="-fuse-ld=mold" MAX_JOBS="${MAX_JOBS:-$(nproc)}" python3 setup.py bdist_wheel --dist-dir=/dist \
    && ls /dist/*.whl

# ---- runtime: deps + the built wheel, rootless ----
FROM deps AS runtime
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# Security patch layer: the digest-pinned ROCm base lags Ubuntu security updates (Trivy gate); upgrade installed OS packages only.
# hadolint ignore=DL3005
RUN apt-get update -y && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub
ARG RAY_VERSION=2.60.0
ARG PYTHON_MINOR=3.12
ENV RAY_VERSION=${RAY_VERSION} PYTHON_MINOR=${PYTHON_MINOR} HSA_NO_SCRATCH_RECLAIM=1
RUN --mount=type=bind,from=build,source=/dist,target=/dist \
    uv pip install --no-cache --no-deps /dist/*.whl
# Ray's bundled Java jars (ray_dist.jar: jackson/guava HIGH CVEs) are unused here (Python workers only; Ray
# tolerates the missing directory), so they are removed rather than shipped.
RUN DEL=/home/ray/anaconda3/lib/python3.12/site-packages/ray/jars && test -d "$DEL" && rm -r "$DEL"
# Build-time contract: Ray + Python minor match the head image; vLLM is the CUDA image's version.
RUN python3 -c "import os, sys, ray, importlib.metadata as m; assert ray.__version__ == os.environ['RAY_VERSION'], ray.__version__; assert '%d.%d' % sys.version_info[:2] == os.environ['PYTHON_MINOR']; assert m.version('vllm').startswith('0.32.0'), m.version('vllm')" \
    && chown 1000:1000 /home/ray
USER 1000
WORKDIR /home/ray
