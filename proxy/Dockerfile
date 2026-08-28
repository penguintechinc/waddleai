FROM python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies. Build context is the
# repo root (see .github/workflows/docker-build.yml build-platform job), so
# this must be qualified as proxy/requirements.txt -- an unqualified
# `COPY requirements.txt .` silently resolves to the ROOT requirements.txt
# instead (both exist at context root), which is missing hypercorn/quart/
# redis/celery/grpc (proxy/requirements.in's `-r ../requirements.txt` pulls
# the root file in as a *subset*, then adds proxy-only deps on top -- the two
# files are not interchangeable). That mismatch let `pip install` succeed
# and the image build/push green while the container silently couldn't run:
# `exec: "hypercorn": executable file not found in $PATH` at container start,
# caught by integration-test's "Start services" step. Mirrors the explicit
# full-path COPY services/management/Dockerfile already uses for the same
# repo-root-context reason.
COPY proxy/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared utilities first
COPY shared/ /app/shared/

# Copy proxy application code
COPY proxy/ /app/

# Create non-root user
RUN adduser --disabled-password --gecos '' waddleai
RUN chown -R waddleai:waddleai /app
USER waddleai

# Expose ports (HTTP + gRPC)
EXPOSE 8080 50051

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

# Set Python path to include /app for shared module imports
ENV PYTHONPATH=/app

# Run the application with hypercorn
CMD ["hypercorn", "apps.proxy_server.main:app", "--bind", "0.0.0.0:8080", "--workers", "4"]
