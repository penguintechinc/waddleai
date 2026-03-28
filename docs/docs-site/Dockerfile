FROM python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb

WORKDIR /docs

# Install dependencies
RUN pip install --no-cache-dir mkdocs mkdocs-material pymdown-extensions mkdocs-mermaid2-plugin

# Copy documentation files
COPY docs/ /docs/docs/
COPY mkdocs.yml /docs/

# Expose MkDocs development server port
EXPOSE 8080

# Run MkDocs dev server
CMD ["mkdocs", "serve", "-a", "0.0.0.0:8080"]