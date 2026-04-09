# WaddleAI Management Service
# Flask + Flask-Security-Too based management platform
# Multi-stage build for minimal image size
# NOTE: Build context is repo root (.). Paths are relative to repo root.

FROM python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb AS builder

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

# Install Python dependencies from the Flask management service
COPY services/management/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt


# Final stage
FROM python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb

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

# Copy Flask management application
COPY services/management/app/ /app/app/
COPY services/management/wsgi.py /app/wsgi.py

# Copy shared utilities
COPY shared/ /app/shared/

# Create necessary directories
RUN mkdir -p /app/config/marchproxy /app/logs /app/databases && \
    chown -R waddleai:waddleai /app

# Switch to non-root user
USER waddleai

# Environment variables
ENV FLASK_APP=wsgi:app \
    FLASK_ENV=production \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# Expose port
EXPOSE 8001

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8001/healthz || exit 1

# Run with gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8001", "--workers", "2", "--worker-class", "gevent", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
