FROM python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb

WORKDIR /docs

# Install dependencies from the hash-pinned lockfile that already sits beside
# this Dockerfile. It was previously ignored in favour of an unpinned
# `pip install mkdocs ...`, so the published docs image installed whatever was
# newest at build time -- an unpinned supply chain for a public artifact.
COPY docs/docs-site/requirements.txt /tmp/docs-requirements.txt
RUN pip install --no-cache-dir --require-hashes --requirement /tmp/docs-requirements.txt \
    && rm -f /tmp/docs-requirements.txt

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser

# Copy documentation files
COPY --chown=appuser:appuser docs/ /docs/docs/
COPY --chown=appuser:appuser mkdocs.yml /docs/

# Set ownership of writable dirs
RUN chown -R appuser:appuser /docs

USER appuser

# Expose MkDocs development server port
EXPOSE 8080

# Run MkDocs dev server
CMD ["mkdocs", "serve", "-a", "0.0.0.0:8080"]
