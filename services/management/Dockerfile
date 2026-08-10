# WaddleAI Management Service
# Flask + Flask-Security-Too based management platform
# Multi-stage build for minimal image size

FROM python:3.13-slim-bookworm@sha256:67a1e1f215ccda113cfc024e8639049257e88f273898f595b61476d128d387e8 AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libffi-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY services/management/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt


# Final stage
FROM python:3.13-slim-bookworm@sha256:67a1e1f215ccda113cfc024e8639049257e88f273898f595b61476d128d387e8

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -s /bin/false waddleai

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Set working directory
WORKDIR /app

# Copy application code. Build context is the REPO ROOT (as for the proxy
# image) because this service imports the top-level `shared` package —
# shared.auth.penguin_auth and shared.security.credential_encryption are used
# by app/api/v1/auth.py, providers.py and services/provider_sync.py. Building
# from services/management/ as context produced an image that ImportErrors on
# startup because shared/ was never copied in.
COPY shared/ /app/shared/
COPY services/management/app/ /app/app/
COPY services/management/asgi.py /app/

# Create necessary directories
RUN mkdir -p /app/config/marchproxy /app/logs /app/databases && \
    chown -R waddleai:waddleai /app

# Switch to non-root user
USER waddleai

# Environment variables
ENV FLASK_ENV=production \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Expose port
EXPOSE 8001

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/healthz')" || exit 1

# Run with hypercorn (ASGI)
# SQLAlchemy handles schema init once, PyDAL connects in each worker
CMD ["hypercorn", "asgi:app", "--bind", "0.0.0.0:8001", "--workers", "2"]
