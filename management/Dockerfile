FROM python:3.13-slim

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

# Copy management application code
COPY management/ /app/

# Create non-root user
RUN adduser --disabled-password --gecos '' waddleai
RUN chown -R waddleai:waddleai /app
USER waddleai

# Expose port
EXPOSE 8001

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8001/healthz || exit 1

# Set Python path to include /app for shared module imports
ENV PYTHONPATH=/app

# Run the application
CMD ["python", "apps/management_server/main.py"]