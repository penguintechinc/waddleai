# Multi-stage Dockerfile for WaddleAI WebUI
# React frontend built by Vite, served by Express (static files + /api proxy)

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

# Production stage: Node runtime running Express (never nginx — house standard)
FROM node:24-bookworm-slim@sha256:3638d9a6fe4030bd716be989438248074489337ba3275657f93595428be4fc03

ENV NODE_ENV=production

WORKDIR /app

# Runtime dependencies only (express, compression, http-proxy). The lockfile
# pins exact versions; --ignore-scripts blocks install-time script execution.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force

# Built assets (Vite copies public/ into dist/, so no separate logo copy) and
# the server. Files stay root-owned and read-only to the runtime user.
COPY --from=builder /app/dist ./dist
COPY server.js ./server.js

# Non-root: uid/gid 1000 is the base image's built-in `node` user. Numeric so
# Kubernetes runAsNonRoot can verify it. The server writes nothing to disk, so
# a read-only root filesystem works (only /tmp needs to be an emptyDir).
USER 1000:1000

EXPOSE 8080

# Native health check (no curl/wget in the slim image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["node", "-e", "require('http').get('http://127.0.0.1:8080/health', r => process.exit(r.statusCode === 200 ? 0 : 1)).on('error', () => process.exit(1))"]

CMD ["node", "server.js"]
