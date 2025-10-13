FROM python:3.14-slim

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

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8000/healthz || exit 1

# Run the application
CMD ["python", "apps/proxy_server/main.py"]