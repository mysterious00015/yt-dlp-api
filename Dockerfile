# syntax=docker/dockerfile:1
# Multi-stage Dockerfile for yt-dlp REST API
# Implements requirements 32 (container security) and 41 (Docker containerization)

# =============================================================================
# Stage 1: Builder - Install Python dependencies
# =============================================================================
FROM python:3.11-slim AS builder

# Set build arguments
ARG PIP_NO_CACHE_DIR=1
ARG PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install application dependencies
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Install yt-dlp and its related dependencies
COPY requirements-ytdlp.txt .
RUN pip install --user --no-cache-dir -r requirements-ytdlp.txt


# =============================================================================
# Stage 2: Runtime - Minimal production image
# =============================================================================
FROM python:3.11-slim AS runtime

# Container metadata
ARG VERSION=dev

LABEL maintainer="Francesco Vadicamo" \
      description="REST API for video downloads using yt-dlp" \
      org.opencontainers.image.title="yt-dlp REST API" \
      org.opencontainers.image.description="REST API for video downloads and metadata extraction using yt-dlp" \
      org.opencontainers.image.source="https://github.com/fvadicamo/yt-dlp-api" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/home/appuser/.local/bin:$PATH" \
    APP_SERVER_HOST=0.0.0.0 \
    APP_SERVER_PORT=8000

WORKDIR /app


# =============================================================================
# Install runtime dependencies
# - FFmpeg for audio/video processing
# - Node.js 22+ for yt-dlp JavaScript challenge resolution
# - curl for container health checks
# =============================================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    gnupg \
    ca-certificates \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && chmod 644 /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && node --version \
    && npm --version \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/apt/keyrings/nodesource.gpg \
             /etc/apt/sources.list.d/nodesource.list


# =============================================================================
# Create non-root application user
# =============================================================================
RUN useradd --create-home --uid 1000 --shell /bin/bash appuser


# =============================================================================
# Copy Python dependencies from builder
# =============================================================================
COPY --from=builder /root/.local /home/appuser/.local


# =============================================================================
# Create application directories
# =============================================================================
RUN mkdir -p /app/downloads /app/cookies /app/logs \
    && chown -R appuser:appuser /app /home/appuser/.local


# =============================================================================
# Copy application source
# =============================================================================
COPY --chown=appuser:appuser app/ /app/app/
COPY --chown=appuser:appuser requirements.txt /app/requirements.txt


# =============================================================================
# Verify required JavaScript runtime and download utilities
# =============================================================================
RUN python --version \
    && node --version \
    && yt-dlp --version \
    && ffmpeg -version


# =============================================================================
# Run as non-root user
# =============================================================================
USER appuser


# =============================================================================
# Container health check
# =============================================================================
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail --silent http://localhost:${APP_SERVER_PORT}/health || exit 1


# =============================================================================
# Expose API port
# Render uses APP_SERVER_PORT=10000 in your service configuration.
# =============================================================================
EXPOSE 8000 10000


# =============================================================================
# Start the API
# =============================================================================
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host \"$APP_SERVER_HOST\" --port \"$APP_SERVER_PORT\""]
