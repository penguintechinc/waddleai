# Multi-stage Dockerfile for WaddleAI WebUI
# React frontend served via nginx

FROM node:24-bookworm-slim@sha256:3638d9a6fe4030bd716be989438248074489337ba3275657f93595428be4fc03 AS builder

WORKDIR /app

# Lockfile is required, not optional — npm ci refuses to run without it, and a
# glob that silently tolerates its absence would defeat the point.
COPY package.json package-lock.json ./

# npm ci, not npm install: install can resolve versions that differ from the
# lockfile, so a dependency pinned for a security fix is not guaranteed to be
# the one that ships. ci installs exactly what the lockfile records.
RUN npm ci

# Copy source code
COPY . .

# Build for production
RUN npm run build

# Production stage
FROM nginx:stable-bookworm@sha256:552e7481ca93ffccd046aa658dbbed22caefbc09c66fa7cd247cbb90b8a5c609

# Upgrade base-image packages carrying fixed HIGH/CRITICAL CVEs not yet baked
# into this digest's build (confirmed via `trivy image` against the built
# image). Bounded to this digest's already-configured bookworm/bookworm-
# security suite (no new packages, no suite change) -- not a version string
# pin because trivy's advisory-DB "fixed version" strings lag the live apt
# archive (observed mismatch: trivy reported libssl3/openssl fixed at
# 3.0.19-1~deb12u2, the archive's actual candidate was already 3.0.20-1~deb12u2),
# so hardcoding trivy's version string is brittle -- `upgrade` always resolves
# to the current in-suite candidate for the packages already installed.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# Copy built assets from builder
COPY --from=builder /app/dist /usr/share/nginx/html

# Copy nginx configuration
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Add logo to public assets
COPY public/logo.png /usr/share/nginx/html/logo.png

# Create non-root user
RUN groupadd -r -g 1000 waddleai && \
    useradd -r -u 1000 -g waddleai waddleai && \
    chown -R waddleai:waddleai /usr/share/nginx/html && \
    chown -R waddleai:waddleai /var/cache/nginx && \
    chown -R waddleai:waddleai /var/log/nginx && \
    chown -R waddleai:waddleai /etc/nginx/conf.d && \
    touch /var/run/nginx.pid && \
    chown -R waddleai:waddleai /var/run/nginx.pid

# Switch to non-root user
USER waddleai

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/ || exit 1

# Start nginx
CMD ["nginx", "-g", "daemon off;"]
