# vLLM-on-Ray worker image, NVIDIA/CUDA (A7). Rootless (uid 1000); KubeRay supplies the start
# command, so there is no ENTRYPOINT/CMD override. Bundled vLLM 0.29.0 (verified in the image).
#
# Base pin: Ray 2.60.0 / py312 / CUDA 13.0, digest verified with
#   docker buildx imagetools inspect rayproject/ray-llm:2.60.0-py312-cu130 \
#     --format '{{json .Manifest}}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["digest"])'
# Ray and the Python minor MUST equal the head image (rayproject/ray:2.60.0-py312): the build asserts it.
ARG RAY_LLM_TAG=2.60.0-py312-cu130
FROM rayproject/ray-llm:${RAY_LLM_TAG}@sha256:07ab1bbe9304f3de1d957df482db76b3a7b0f4ed5141793d0675555801024725

ARG RAY_VERSION=2.60.0
ARG PYTHON_MINOR=3.12
ENV RAY_VERSION=${RAY_VERSION} PYTHON_MINOR=${PYTHON_MINOR}

# Build-time contract: fail the build on any Ray or Python minor drift from the head image.
RUN python3 -c "import os, sys, ray; assert ray.__version__ == os.environ['RAY_VERSION'], ray.__version__; assert '%d.%d' % sys.version_info[:2] == os.environ['PYTHON_MINOR']"

USER 1000
