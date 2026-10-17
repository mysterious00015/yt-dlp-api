# syntax=docker/dockerfile:1
# Multi-stage Dockerfile for yt-dlp REST API

# =============================================================================
# Stage 1: Builder - Install Python dependencies
# =============================================================================
FROM python:3.11-slim AS builder

ARG PIP_NO_CACHE_DIR=1
ARG PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install application dependencies
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Install yt-dlp and related dependencies
COPY requirements-ytdlp.txt .
RUN pip install --user --no-cache-dir -r requirements-ytdlp.txt


# =============================================================================
# Stage 2: Runtime
# =============================================================================
FROM python:3.11-slim AS runtime

ARG VERSION=dev

LABEL maintainer="Francesco Vadicamo" \
      description="REST API for video downloads using yt-dlp" \
      org.opencontainers.image.title="yt-dlp REST API" \
      org.opencontainers.image.source="https://github.com/fvadicamo/yt-dlp-api" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/home/appuser/.local/bin:$PATH" \
    APP_SERVER_HOST=0.0.0.0 \
    APP_SERVER_PORT=8000

WORKDIR /app


# =============================================================================
# Install FFmpeg, Node.js 22, and required system packages
# =============================================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
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
# Install Deno for yt-dlp JavaScript challenge resolution
# =============================================================================
RUN curl -fsSL https://deno.land/install.sh \
    | DENO_INSTALL=/usr/local sh -s -- -y \
    && test -x /usr/local/bin/deno \
    && /usr/local/bin/deno --version


# =============================================================================
# Create non-root application user
# =============================================================================
RUN useradd --create-home --uid 1000 --shell /bin/bash appuser


# =============================================================================
# Copy installed Python dependencies
# =============================================================================
COPY --from=builder /root/.local /home/appuser/.local

RUN mkdir -p /app/downloads /app/cookies /app/logs \
    && chown -R appuser:appuser /app /home/appuser/.local


# =============================================================================
# Copy application source
# =============================================================================
COPY --chown=appuser:appuser app/ /app/app/
COPY --chown=appuser:appuser requirements.txt /app/requirements.txt


# =============================================================================
# Switch user and verify runtime dependencies
# =============================================================================
USER appuser

RUN python --version \
    && node --version \
    && deno --version \
    && python -m yt_dlp --version \
    && yt-dlp --version \
    && ffmpeg -version


# =============================================================================
# Container health check
# =============================================================================
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail --silent http://localhost:${APP_SERVER_PORT}/liveness || exit 1


# =============================================================================
# Expose ports
# =============================================================================
EXPOSE 8000 10000


# =============================================================================
# Start API
# =============================================================================
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host \"$APP_SERVER_HOST\" --port \"$APP_SERVER_PORT\""]
