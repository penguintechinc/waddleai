# Multi-stage Dockerfile for WaddleAI WebUI
# React frontend served via nginx

FROM node:18-bookworm-slim@sha256:f9ab18e354e6855ae56ef2b290dd225c1e51a564f87584b9bd21dd651838830e AS builder

WORKDIR /app

# Copy package files
COPY package.json package-lock.json* ./

# Install dependencies
RUN npm install

# Copy source code
COPY . .

# Build for production
RUN npm run build

# Production stage
FROM nginx:stable-bookworm@sha256:552e7481ca93ffccd046aa658dbbed22caefbc09c66fa7cd247cbb90b8a5c609

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
