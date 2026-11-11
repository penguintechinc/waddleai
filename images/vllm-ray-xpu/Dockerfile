# vLLM-on-Ray worker image, Intel XPU (A7). Rootless (uid 1000); KubeRay supplies the start command,
# so there is no ENTRYPOINT/CMD override. SINGLE-GPU REPLICAS ONLY (no tensor parallel, spec section 2 non-goal).
#
# vLLM is BUILT FROM SOURCE at the same version as the CUDA image (0.32.0):
#   vLLM tag v0.32.0 = commit 5df63a7203cdb9328d824a27b682e77c7b9928be (XPU is supported at this very version,
#   so no version drift to record). Reference: docker/Dockerfile.xpu + requirements/xpu.txt at that commit.
# vllm_xpu_kernels is 0.1.15.4 (what vLLM 0.32.0 pins in requirements/xpu.txt).
#
# Base pin: intel/oneapi-basekit 2026.0.x does not exist; 2025.3.2 is the newest release (verified on Docker Hub).
# Digest verified with
#   docker buildx imagetools inspect intel/oneapi-basekit:2025.3.2-0-devel-ubuntu24.04 \
#     --format '{{json .Manifest}}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["digest"])'
# Intel UMD/Level Zero .deb files are downloaded from GitHub releases and verified against sha256 below.
# Python packages are hash-pinned: requirements.txt / build-requirements.txt are generated with
#   uv pip compile <name>.in --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
#     --generate-hashes --emit-index-url --index-strategy unsafe-best-match -o <name>.txt
# ray[llm] is spelled out in requirements.in without vllm and the CUDA-only nixl wheels (see that file).
# Ray and the Python minor MUST equal the head image (rayproject/ray:2.60.0-py312): the build asserts it.
# The Python env lives at /home/ray/anaconda3 so every Ray image shares one binary layout (A6 allowlist).

ARG ONEAPI_BASE=intel/oneapi-basekit:2025.3.2-0-devel-ubuntu24.04@sha256:79a5e333ff6f773793d124b78047001c51cbcd53035e5100313abf2f771af95a
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa

# hadolint ignore=DL3006
FROM ${UV_IMAGE} AS uv

# ---- deps: Intel userspace drivers + Python 3.12 venv with every hash-pinned dependency ----
# hadolint ignore=DL3006
FROM ${ONEAPI_BASE} AS deps
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG PYTHON_VERSION=3.12
ENV DEBIAN_FRONTEND=noninteractive UV_PYTHON_INSTALL_DIR=/opt/uv/python UV_LINK_MODE=copy UV_INDEX_STRATEGY=unsafe-best-match \
    UV_PYTHON_PREFERENCE=only-managed VIRTUAL_ENV=/home/ray/anaconda3 \
    PATH=/home/ray/anaconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LD_LIBRARY_PATH=/home/ray/anaconda3/lib:/usr/local/lib

COPY debs.sha256 /tmp/debs.sha256
WORKDIR /tmp/neo
# UMD (compute runtime + Level Zero) + xpu-smi, downloaded then sha256-verified (debs.sha256: "<sum> <url>");
# same versions as vLLM v0.32.0 docker/Dockerfile.xpu.
# Base ships the kobuk-team PPA already; every package (Ubuntu + PPA) is pinned to the exact version the
# digest-pinned base resolves via `apt-cache policy` (transitive deps follow from those pins).
RUN apt-get update -y && apt-get install -y --no-install-recommends ca-certificates=20260816ubuntu0.24.04.2 curl=8.5.0-2ubuntu10.16 \
    libaio-dev=0.3.113-6build1.1 numactl=2.0.18-1ubuntu0.24.04.1 libsndfile1=1.2.2-1ubuntu5.24.04.1 libgl1=1.7.0-1build1 \
    libsm6=2:1.2.3-1build3 libxext6=2:1.3.4-1build2 ffmpeg=7:6.1.1-3ubuntu5 \
    && while read -r _ url; do curl -fsSLO "$url"; done < /tmp/debs.sha256 \
    && while read -r sum url; do echo "${sum}  ${url##*/}"; done < /tmp/debs.sha256 | sha256sum -c - \
    && mkdir /tmp/xpum && mv ./xpu-smi_*.deb /tmp/xpum/ \
    && dpkg -i ./*.deb \
    && rm -rf /var/lib/apt/lists/*

# xpu-smi runtime libs: kobuk-team PPA packages (already configured in the base), exact versions.
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends libhwloc15=2.10.0-1build1 libpciaccess0=0.17-3ubuntu0.24.04.3 \
       libigsc1=1.3.1-1~24.04~ppa2 intel-metrics-library=1.0.240-1~24.04~ppa1 \
    && apt-get install -y --no-install-recommends /tmp/xpum/xpu-smi_*.deb \
    && rm -rf /tmp/neo /tmp/xpum /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /tmp/deps
COPY requirements.txt ./
# Ubuntu 24.04 images ship uid 1000 as `ubuntu`; free it for the Ray user.
RUN if getent passwd 1000 >/dev/null; then userdel -r "$(getent passwd 1000 | cut -d: -f1)"; fi \
    && useradd --uid 1000 --create-home --home-dir /home/ray --shell /bin/bash ray \
    && uv venv --python "${PYTHON_VERSION}" --seed "${VIRTUAL_ENV}" \
    && uv pip install --no-cache --require-hashes --no-deps -r requirements.txt \
    && chmod -R a+rX /opt/uv /home/ray/anaconda3

# ---- build: compile the vLLM XPU wheel (never shipped) ----
FROM deps AS build
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG VLLM_COMMIT=5df63a7203cdb9328d824a27b682e77c7b9928be
ARG VLLM_VERSION=0.32.0
ARG RUST_TOOLCHAIN=1.95
ENV VLLM_TARGET_DEVICE=xpu CARGO_HOME=/opt/cargo RUSTUP_HOME=/opt/rustup PATH=/opt/cargo/bin:${PATH} \
    SETUPTOOLS_SCM_PRETEND_VERSION=${VLLM_VERSION}
COPY build-requirements.txt /tmp/build-requirements.txt
# hadolint ignore=DL3008
RUN apt-get update -y && apt-get install -y --no-install-recommends git=1:2.43.0-1ubuntu7.4 \
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
RUN python3 setup.py bdist_wheel --dist-dir=/dist --py-limited-api=cp38 && ls /dist/*.whl

# ---- runtime: deps + the built wheel, rootless ----
FROM deps AS runtime
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# Security patch layer: the digest-pinned oneAPI base lags Ubuntu security updates (Trivy gate: linux-libc-dev x765,
# apt, openssl, libbrotli1, packagekit all have FixedVersions in noble-security). Upgrade installed OS packages only.
# Also drop SSH host keys baked into the base (private keys must never ship in an image; Trivy secret finding).
# hadolint ignore=DL3005
RUN apt-get update -y && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub
ARG RAY_VERSION=2.60.0
ARG PYTHON_MINOR=3.12
ENV RAY_VERSION=${RAY_VERSION} PYTHON_MINOR=${PYTHON_MINOR} VLLM_WORKER_MULTIPROC_METHOD=spawn
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
