"""FastAPI application entry point.

This module assembles all components and creates the main application.
Supports YouTube and generic yt-dlp-compatible providers.
"""

import asyncio
import contextlib
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app import __version__
from app.api import admin, download, health, jobs, metrics, transcript, video
from app.core.config import ConfigService, SecurityConfig
from app.core.errors import APIError, global_exception_handler
from app.core.logging import configure_logging
from app.core.metrics import MetricsCollector, initialize_metrics
from app.core.rate_limiter import configure_rate_limiter
from app.core.startup import StartupValidator
from app.middleware.auth import configure_auth
from app.middleware.rate_limit import RateLimitMiddleware
from app.providers.manager import ProviderManager
from app.providers.youtube import YouTubeProvider
from app.providers.generic import GenericYtDlpProvider
from app.services.cookie_service import CookieService
from app.services.download_queue import (
    configure_download_queue,
    get_download_queue,
)
from app.services.download_worker import (
    configure_download_worker,
    get_download_worker,
)
from app.services.job_service import (
    configure_job_service,
    get_job_service,
)
from app.services.storage import configure_storage
from app.services.webhook_service import configure_webhook_service

logger = structlog.get_logger(__name__)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Middleware to track HTTP request metrics.

    Records request count and duration for all endpoints,
    using FastAPI route templates to normalize paths.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Process request and record metrics."""
        start_time = time.time()
        response = await call_next(request)
        duration = time.time() - start_time

        # Use route templates to normalize paths.
        # Use a fixed label for unmatched routes.
        route = request.scope.get("route")
        endpoint = route.path if route else "/unmatched"

        MetricsCollector.record_request(
            method=request.method,
            endpoint=endpoint,
            status=response.status_code,
            duration=duration,
        )

        return response


# Global service instances
_provider_manager: ProviderManager | None = None
_cookie_service: CookieService | None = None
_cleanup_task: asyncio.Task | None = None
_disabled_providers: list[str] = []


def get_provider_manager() -> ProviderManager:
    """Get the global provider manager instance."""
    if _provider_manager is None:
        raise RuntimeError("Provider manager not configured")
    return _provider_manager


def get_cookie_service() -> CookieService:
    """Get the global cookie service instance."""
    if _cookie_service is None:
        raise RuntimeError("Cookie service not configured")
    return _cookie_service


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager for startup and shutdown."""
    global _provider_manager, _cookie_service, _cleanup_task
    global _disabled_providers

    logger.info("Application starting", version=__version__)

    # Initialize metrics with application version.
    initialize_metrics(__version__)

    # Load configuration.
    config_service = ConfigService()
    config = config_service.load()

    # Configure logging.
    configure_logging(config.logging.level, config.logging.format)

    logger.info(
        "Configuration loaded",
        server_port=config.server.port,
        output_dir=config.storage.output_dir,
    )

    # Run startup validation.
    validator = StartupValidator(config)
    startup_result = await validator.validate_all()

    if not startup_result.success:
        logger.critical(
            "Startup validation failed",
            errors=startup_result.errors,
            allow_degraded_start=config.security.allow_degraded_start,
        )
        raise RuntimeError(
            f"Startup validation failed: {'; '.join(startup_result.errors)}"
        )

    if startup_result.degraded_mode:
        logger.warning(
            "Starting in degraded mode",
            warnings=startup_result.warnings,
            disabled_providers=startup_result.disabled_providers,
        )

    # Store providers disabled by startup validation.
    _disabled_providers = startup_result.disabled_providers

    # Configure authentication.
    configure_auth(api_keys=config.security.api_keys)

    # Configure rate limiter.
    configure_rate_limiter(
        metadata_rpm=config.rate_limiting.metadata_rpm,
        download_rpm=config.rate_limiting.download_rpm,
        burst_capacity=config.rate_limiting.burst_capacity,
    )

    # Configure storage manager.
    storage = configure_storage(config.storage)

    logger.info(
        "Storage manager configured",
        output_dir=config.storage.output_dir,
    )

    # Configure job service with a callback for expired jobs.
    def on_job_expired(job_id: str) -> None:
        """Unregister files when a job expires."""
        storage.unregister_active_job(job_id)

    job_service = configure_job_service(
        job_ttl_hours=config.downloads.job_ttl,
        on_job_expired=on_job_expired,
    )

    logger.info(
        "Job service configured",
        job_ttl_hours=config.downloads.job_ttl,
    )

    # Configure download queue.
    download_queue = configure_download_queue(
        max_concurrent=config.downloads.max_concurrent,
        max_queue_size=config.downloads.queue_size,
    )

    logger.info(
        "Download queue configured",
        max_concurrent=config.downloads.max_concurrent,
        max_queue_size=config.downloads.queue_size,
    )

    # Configure webhook service.
    configure_webhook_service(
        enabled=config.webhooks.enabled,
        allowed_hosts=config.webhooks.allowed_hosts,
        secret=config.webhooks.secret,
        timeout=config.webhooks.timeout,
        max_retries=config.webhooks.max_retries,
    )

    logger.info(
        "Webhook service configured",
        enabled=config.webhooks.enabled,
        allowed_hosts_count=len(config.webhooks.allowed_hosts),
    )

    # Configure cookie service for the dedicated YouTube provider.
    cookie_config = {
        "providers": {
            "youtube": {
                "enabled": config.providers.youtube.enabled,
                "cookie_path": config.providers.youtube.cookie_path,
            }
        }
    }

    _cookie_service = CookieService(cookie_config)
    logger.info("Cookie service configured")

    # Create provider manager.
    _provider_manager = ProviderManager()

    # Register YouTube provider when enabled and startup validation passes.
    if (
        config.providers.youtube.enabled
        and "youtube" not in _disabled_providers
    ):
        youtube_config = {
            "cookie_path": config.providers.youtube.cookie_path,
            "retry_attempts": config.providers.youtube.retry_attempts,
            "retry_backoff": config.providers.youtube.retry_backoff,
            "output_dir": config.storage.output_dir,
        }

        youtube_provider = YouTubeProvider(
            youtube_config,
            _cookie_service,
        )

        _provider_manager.register_provider(
            "youtube",
            youtube_provider,
            enabled=True,
        )

        logger.info("YouTube provider registered")

    elif "youtube" in _disabled_providers:
        logger.warning(
            "YouTube provider disabled due to startup validation failure"
        )

    # Register generic yt-dlp provider for other recognized websites.
    # This provider must exclude YouTube URLs so the dedicated YouTube
    # provider can continue using its configured cookie service.
    generic_config = {
        "retry_attempts": 3,
        "retry_backoff": [2, 4, 8],
        "output_dir": config.storage.output_dir,
    }

    generic_provider = GenericYtDlpProvider(generic_config)

    _provider_manager.register_provider(
        "generic_ytdlp",
        generic_provider,
        enabled=True,
    )

    logger.info(
        "Generic yt-dlp provider registered",
        output_dir=config.storage.output_dir,
    )

    # Configure download worker with the provider manager.
    worker = configure_download_worker(
        provider_manager=_provider_manager,
        job_service=job_service,
        download_queue=download_queue,
        storage_manager=storage,
    )

    # Start download worker.
    await worker.start()
    logger.info("Download worker started")

    # Start cleanup scheduler in the background.
    from app.services.storage import cleanup_scheduler

    _cleanup_task = asyncio.create_task(
        cleanup_scheduler(storage, interval=3600)
    )

    logger.info("Cleanup scheduler started")
    logger.info("Application startup complete", version=__version__)

    yield

    # Shutdown.
    logger.info("Application shutting down")

    if _cleanup_task:
        _cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _cleanup_task

    await worker.stop()
    logger.info("Application shutdown complete")


API_DESCRIPTION = """
## Overview

REST API for downloading media and extracting metadata using yt-dlp.

The API supports YouTube through its dedicated provider and other websites
recognized by installed yt-dlp site extractors through the generic provider.
Availability depends on the website, its access restrictions, and the media.

## Authentication

All endpoints except health checks require an API key in the `X-API-Key` header.

```text
X-API-Key: your-api-key-here
