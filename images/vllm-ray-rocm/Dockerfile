# vLLM-on-Ray worker image, AMD/ROCm (A7, AM20). Rootless (uid 1000); KubeRay supplies the start
# command, so there is no ENTRYPOINT/CMD override.
#
# AM20: no rocm/vllm tag pairs vLLM 0.29 with Python 3.12, so vLLM is BUILT FROM SOURCE at exactly the
# version bundled in the CUDA image (rayproject/ray-llm:2.60.0-py312-cu130 -> vLLM 0.29.0):
#   vLLM tag v0.29.0 = commit 98dff2a81d747d1dba01a47f939f48c3526d4206
#   reference: docker/Dockerfile.rocm + docker/Dockerfile.rocm_base + requirements/rocm.txt at that commit.
# DEVIATION from the reference (recorded in DEVIATIONS-A.md): vLLM's rocm_base compiles a patched
# ROCm/PyTorch from source; here the official PyTorch ROCm wheel (torch 2.13.0+rocm7.2, the exact torch
# vLLM 0.29.0 pins) is installed instead. Needs GPU validation on a real ROCm node (cannot run in CI).
#
# Base pin (vLLM's own rocm_base BASE_IMAGE), digest verified with
#   docker buildx imagetools inspect rocm/dev-ubuntu-22.04:7.2.3-complete \
#     --format '{{json .Manifest}}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["digest"])'
# Python packages are hash-pinned: requirements.txt / build-requirements.txt are generated with
#   uv pip compile <name>.in --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
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
ARG VLLM_COMMIT=98dff2a81d747d1dba01a47f939f48c3526d4206
ARG VLLM_VERSION=0.29.0
# Default covers MI200/MI300/MI350 + RDNA3/4; narrow it to shorten the build for a known fleet.
ARG PYTORCH_ROCM_ARCH="gfx90a;gfx942;gfx950;gfx1100;gfx1101;gfx1200;gfx1201"
ENV VLLM_TARGET_DEVICE=rocm PYTORCH_ROCM_ARCH=${PYTORCH_ROCM_ARCH} CARGO_HOME=/opt/cargo RUSTUP_HOME=/opt/rustup \
    PATH=/opt/cargo/bin:${PATH} SETUPTOOLS_SCM_PRETEND_VERSION=${VLLM_VERSION}
COPY build-requirements.txt /tmp/build-requirements.txt
# hadolint ignore=DL3008
RUN apt-get update -y && apt-get install -y --no-install-recommends git curl ca-certificates mold \
    && rm -rf /var/lib/apt/lists/* \
    && uv pip install --no-cache --require-hashes --no-deps -r /tmp/build-requirements.txt
WORKDIR /src/vllm
RUN git init -q . && git fetch -q --depth 1 https://github.com/vllm-project/vllm.git "${VLLM_COMMIT}" \
    && git checkout -q FETCH_HEAD && test "$(git rev-parse HEAD)" = "${VLLM_COMMIT}"
# Rust toolchain: channel comes from the pinned source tree's rust-toolchain.toml (build stage only).
RUN TOOLCHAIN="$(grep '^channel' rust-toolchain.toml | sed 's/.*= *"\(.*\)"/\1/')" \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain none \
    && rustup toolchain install "${TOOLCHAIN}" && rustup default "${TOOLCHAIN}" \
    && CARGO_BUILD_JOBS=4 bash build_rust.sh && test -x vllm/vllm-rs
RUN LDFLAGS="-fuse-ld=mold" MAX_JOBS="${MAX_JOBS:-$(nproc)}" python3 setup.py bdist_wheel --dist-dir=/dist \
    && ls /dist/*.whl

# ---- runtime: deps + the built wheel, rootless ----
FROM deps AS runtime
ARG RAY_VERSION=2.60.0
ARG PYTHON_MINOR=3.12
ENV RAY_VERSION=${RAY_VERSION} PYTHON_MINOR=${PYTHON_MINOR} HSA_NO_SCRATCH_RECLAIM=1
RUN --mount=type=bind,from=build,source=/dist,target=/dist \
    uv pip install --no-cache --no-deps /dist/*.whl
# Build-time contract: Ray + Python minor match the head image; vLLM is the CUDA image's version.
RUN python3 -c "import os, sys, ray, importlib.metadata as m; assert ray.__version__ == os.environ['RAY_VERSION'], ray.__version__; assert '%d.%d' % sys.version_info[:2] == os.environ['PYTHON_MINOR']; assert m.version('vllm').startswith('0.29.0'), m.version('vllm')" \
    && chown 1000:1000 /home/ray
USER 1000
WORKDIR /home/ray
