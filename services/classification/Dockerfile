# WaddleAI Classification Service (PII-NER: Presidio + spaCy behind gRPC)
# Build context is the REPO ROOT: the service imports the top-level `shared`
# package (content_filter fail-mode metric, feature flags, license bypass).
#
#   docker build -f services/classification/Dockerfile -t waddleai-classification .
#   docker build -f services/classification/Dockerfile --target test -t waddleai-classification:test .
#
# Offline-start contract: the spaCy model is baked at BUILD time from a
# hash-verified wheel (requirements.txt). WADDLEAI_NER_ALLOW_DOWNLOAD is never
# set, and no torch/transformers is installed, so startup needs zero egress.

FROM python:3.13-slim-trixie@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb AS builder

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY services/classification/requirements.txt /tmp/requirements.txt
# pip pinned (an unpinned `--upgrade pip` is an unpinned dep of every other dep).
# Binary wheels only (no compiler in the image) and every artifact hash-verified,
# including the en_core_web_lg model wheel.
RUN pip install --no-cache-dir "pip==26.2.1" && \
    pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt && \
    pip uninstall -y pip
# pip is removed from the shipped venv: it vendors urllib3/certifi/msgpack/setuptools
# copies that scanners flag, and the runtime image never installs anything.


FROM python:3.13-slim-trixie@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb AS runtime

# Debian 13 (trixie) base: bookworm's latest digest still carries 59 HIGH OS advisories.
# The base image's own pip is deleted too (vendored-dependency advisories; unused at runtime).
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
    /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 && \
    useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY shared/ /app/shared/
COPY services/classification/app/ /app/services/classification/app/
COPY services/classification/asgi.py /app/services/classification/asgi.py

# readOnlyRootFilesystem-safe: nothing writes outside /tmp (an emptyDir in K8s).
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    TMPDIR=/tmp \
    HTTP_PORT=8080 \
    GRPC_PORT=50051

USER appuser
EXPOSE 8080 50051

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/livez', timeout=5)"]

# `--workers 0` = serve in the main process (NOT a forked hypercorn worker). Required:
# the engine owns a spawn-context NER ProcessPoolExecutor, and under `--workers 1`
# hypercorn runs the app inside a multiprocessing child where the pool start fails
# (AssertionError -> engine never ready). It also keeps the app single-instance
# (the gRPC port binds in before_serving; extra ASGI workers would clash and multiply
# the ~0.85 GB/worker model RSS -- scale via NER_POOL_WORKERS / replicas) and makes
# hypercorn PID 1 so SIGTERM drains gracefully.
CMD ["sh", "-c", "exec hypercorn services.classification.asgi:app --bind 0.0.0.0:${HTTP_PORT} --workers 0"]


# Test-only stage (never the default image): real-model suite run in-image.
FROM runtime AS test
USER root
COPY services/classification/requirements-test.txt /tmp/requirements-test.txt
RUN python3 -m ensurepip --default-pip && \
    python3 -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements-test.txt
COPY pytest.ini /app/pytest.ini
COPY tests/__init__.py tests/conftest.py /app/tests/
COPY tests/integration/__init__.py tests/integration/conftest.py /app/tests/integration/
COPY tests/integration/classification/ /app/tests/integration/classification/
COPY tests/unit/classification/auth_fakes.py /app/tests/unit/classification/auth_fakes.py
USER appuser
CMD ["python3", "-m", "pytest", "tests/integration/classification", "-m", "ner_model"]


# Default (last) stage = the shipped runtime image.
FROM runtime AS image
