# vLLM-on-Ray worker image, NVIDIA/CUDA (A7). Rootless (uid 1000); KubeRay supplies the start
# command, so there is no ENTRYPOINT/CMD override. vLLM 0.32.0 (verified in the image).
#
# Base pin: Ray 2.60.0 / py312 / CUDA 13.0, digest verified with
#   docker buildx imagetools inspect rayproject/ray-llm:2.60.0-py312-cu130 \
#     --format '{{json .Manifest}}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["digest"])'
# Ray and the Python minor MUST equal the head image (rayproject/ray:2.60.0-py312): the build asserts it.
#
# The base ships vLLM 0.29.0 and a stack of libraries with open HIGH/CRITICAL CVEs, so a hash-pinned upgrade
# layer (requirements.txt) sits on top: vLLM 0.32.0 plus the fixed library versions. torch/CUDA stay with the
# base (vLLM 0.32.0 pins the same torch 2.13.0). requirements.txt holds only the packages that differ from the
# base; regenerate with
#   make regen-vllm-ray-cuda-requirements
# (python3 regen_requirements.py <base-image-ref>: freezes the base, resolves requirements.in with uv against it).
ARG RAY_LLM_TAG=2.60.0-py312-cu130
FROM rayproject/ray-llm:${RAY_LLM_TAG}@sha256:07ab1bbe9304f3de1d957df482db76b3a7b0f4ed5141793d0675555801024725

ARG RAY_VERSION=2.60.0
ARG PYTHON_MINOR=3.12
ENV RAY_VERSION=${RAY_VERSION} PYTHON_MINOR=${PYTHON_MINOR}

# Build-time contract: fail the build on any Ray or Python minor drift from the head image.
RUN python3 -c "import os, sys, ray; assert ray.__version__ == os.environ['RAY_VERSION'], ray.__version__; assert '%d.%d' % sys.version_info[:2] == os.environ['PYTHON_MINOR']"

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# OS security updates (the digest-pinned base lags Ubuntu: apt/libapt-pkg HIGH). Also drop what a runtime worker
# never uses and that carries unfixable findings: Nsight Compute (bundled Go binaries on an old stdlib), Ray's
# Java jars (jackson/guava; Java workers are not used, ray tolerates the missing dir), and conda's package cache
# (stale setuptools 75.8.0 / urllib3 2.3.0 copies that Trivy scans as installed).
# hadolint ignore=DL3005
RUN apt-get update -y && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* /opt/nvidia/nsight-compute \
       /home/ray/anaconda3/lib/python3.12/site-packages/ray/jars /home/ray/anaconda3/pkgs

USER 1000
COPY --chown=1000:1000 requirements.txt /tmp/requirements.txt
RUN uv pip install --python /home/ray/anaconda3/bin/python --no-cache --require-hashes --no-deps -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt \
    && python3 -c "import importlib.metadata as m; assert m.version('vllm') == '0.32.0', m.version('vllm')"
