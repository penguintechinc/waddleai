FROM python:3.14-slim-bookworm@sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
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