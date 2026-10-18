"""FastAPI application entry point.

This module assembles all components and creates the main application.
Implements requirements 12, 16, 19, 20, 29, 39, and 46.
"""

import asyncio
import contextlib
import ipaddress
import json
import re
import socket
import time
from pathlib import Path
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

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
from app.core.validation import ValidationResult
from app.middleware.auth import configure_auth
from app.middleware.rate_limit import RateLimitMiddleware
from app.models.video import DownloadResult, VideoFormat
from app.providers.exceptions import DownloadError, InvalidURLError, ProviderError
from app.providers.manager import ProviderManager
from app.providers.youtube import YouTubeProvider
from app.services.cookie_service import CookieService
from app.services.download_queue import configure_download_queue, get_download_queue
from app.services.download_worker import configure_download_worker, get_download_worker
from app.services.job_service import configure_job_service, get_job_service
from app.services.storage import configure_storage
from app.services.webhook_service import configure_webhook_service

logger = structlog.get_logger(__name__)


# ============================================================
# INSTAGRAM PROVIDER
# Kept inside main.py; no generic.py file is required.
# ============================================================

class InstagramYtDlpProvider(YouTubeProvider):
    """Instagram provider using the existing yt-dlp execution helpers."""

    ALLOWED_HOSTS = {
        "instagram.com",
        "www.instagram.com",
        "m.instagram.com",
        "instagr.am",
        "www.instagr.am",
    }

    MEDIA_PATH_PATTERN = re.compile(
        r"^/(?:reel|reels|p|tv)/([^/]+)/?",
        re.IGNORECASE,
    )

    def __init__(self, config: dict):
        # Instagram downloads do not use the YouTube CookieService.
        super().__init__(config, cookie_service=None)

    def validate_url(self, url: str) -> bool:
        if not url:
            return False

        try:
            parsed = urlparse(url)
        except ValueError:
            return False

        host = (parsed.hostname or "").lower().rstrip(".")

        if parsed.scheme.lower() not in {"http", "https"}:
            return False

        if host not in self.ALLOWED_HOSTS:
            return False

        return self.MEDIA_PATH_PATTERN.match(parsed.path or "") is not None

    def extract_video_id(self, url: str) -> Optional[str]:
        try:
            parsed = urlparse(url)
        except ValueError:
            return None

        match = self.MEDIA_PATH_PATTERN.match(parsed.path or "")
        return match.group(1) if match else None

    async def get_info(
        self,
        url: str,
        include_formats: bool = False,
        include_subtitles: bool = False,
    ) -> dict:
        if not self.validate_url(url):
            raise InvalidURLError(f"Invalid Instagram media URL: {url}")

        cmd = [
            "yt-dlp",
            "--dump-single-json",
            "--skip-download",
            "--no-playlist",
            "--socket-timeout",
            "20",
            url,
        ]

        result = await self._execute_with_retry(cmd, timeout=45.0)

        try:
            info = json.loads(
                result.stdout.decode("utf-8", errors="replace")
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DownloadError(
                f"Could not parse Instagram media information: {exc}"
            ) from exc

        video_info = {
            "video_id": (
                info.get("id")
                or self.extract_video_id(url)
                or "instagram-media"
            ),
            "title": info.get("title", ""),
            "duration": self._to_int(info.get("duration")) or 0,
            "author": info.get("uploader") or info.get("channel") or "",
            "upload_date": info.get("upload_date", ""),
            "view_count": info.get("view_count", 0),
            "thumbnail_url": info.get("thumbnail", ""),
            "description": info.get("description", ""),
        }

        if include_formats and isinstance(info.get("formats"), list):
            video_info["formats"] = self._parse_formats(info["formats"])

        if include_subtitles and isinstance(info.get("subtitles"), dict):
            video_info["subtitles"] = self._parse_subtitles(
                info["subtitles"]
            )

        return video_info

    async def list_formats(self, url: str) -> list[VideoFormat]:
        info = await self.get_info(url, include_formats=True)
        formats = []

        for fmt in info.get("formats", []):
            formats.append(
                VideoFormat(
                    format_id=fmt.get("format_id", ""),
                    ext=fmt.get("ext", ""),
                    resolution=fmt.get("resolution"),
                    audio_bitrate=fmt.get("audio_bitrate"),
                    video_codec=fmt.get("video_codec"),
                    audio_codec=fmt.get("audio_codec"),
                    filesize=fmt.get("filesize"),
                    format_type=fmt.get(
                        "format_type",
                        "video+audio",
                    ),
                )
            )

        formats.sort(
            key=lambda item: (
                self._get_resolution_value(item.resolution),
                item.filesize or 0,
                item.format_id,
            ),
            reverse=True,
        )

        return formats

    async def download(
        self,
        url: str,
        format_id: Optional[str] = None,
        output_template: Optional[str] = None,
        extract_audio: bool = False,
        audio_format: Optional[str] = None,
        include_subtitles: bool = False,
        subtitle_lang: Optional[str] = None,
    ) -> DownloadResult:
        if not self.validate_url(url):
            raise InvalidURLError(
                f"Invalid Instagram media URL: {url}"
            )

        media_id = self.extract_video_id(url) or "instagram-media"

        # Do not pass YouTube cookies to Instagram.
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--concurrent-fragments",
            "8",
            "--socket-timeout",
            "20",
            "--retries",
            "5",
            "--fragment-retries",
            "5",
            "--file-access-retries",
            "3",
            "--print",
            "after_move:filepath",
        ]

        # Instagram format selection: support portrait and landscape Reels.
        # Use the shorter video dimension when prioritizing resolution.
        if format_id:
            quality_match = re.fullmatch(
                r"best\[height<=([0-9]+)\]",
                format_id,
            )

            if quality_match:
                quality_cap = quality_match.group(1)

                # Prefer formats at or below the requested resolution.
                # If none are available, select the best available format
                # instead of failing with "Requested format is not available".
                cmd.extend([
                    "-f", "best",
                    "-S", f"res:{quality_cap}",
                ])
            else:
                cmd.extend(["-f", format_id])

        if extract_audio:
            cmd.extend([
                "-x",
                "--audio-format",
                audio_format or "mp3",
            ])

            if audio_format in {"mp3", "m4a"}:
                cmd.extend(["--audio-quality", "0"])

        if output_template:
            cmd.extend(["-o", output_template])

        if include_subtitles:
            cmd.append("--write-subs")

            if subtitle_lang:
                cmd.extend(["--sub-langs", subtitle_lang])

        cmd.append(url)

        logger.debug(
            "Executing Instagram yt-dlp command",
            command=self._redact_command(cmd),
        )

        loop = asyncio.get_running_loop()
        started = loop.time()

        result = await self._execute_with_retry(cmd)

        stdout = result.stdout or b""
        stderr = result.stderr or b""

        logger.debug(
            "Instagram yt-dlp execution completed",
            exit_code=result.returncode,
            stdout_lines=len(
                stdout.decode(
                    "utf-8",
                    errors="replace",
                ).splitlines()
            ),
            stderr_preview=(
                stderr.decode(
                    "utf-8",
                    errors="replace",
                )[:500] or None
            ),
        )

        file_path = self._extract_file_path(
            stdout.decode("utf-8", errors="replace")
        )

        if not file_path:
            raise DownloadError(
                "Could not determine the downloaded Instagram file path"
            )

        output_file = Path(file_path)

        try:
            file_size = output_file.stat().st_size
        except OSError as exc:
            raise DownloadError(
                f"Downloaded Instagram file is unavailable: {exc}"
            ) from exc

        duration = loop.time() - started

        logger.info(
            "Instagram download completed",
            media_id=media_id,
            file_path=str(output_file),
            file_size=file_size,
            duration=duration,
        )

        return DownloadResult(
            file_path=str(output_file),
            file_size=file_size,
            duration=duration,
            format_id=format_id or "best",
        )

    async def get_transcript(
        self,
        url: str,
        lang: str = "en",
        source: str = "any",
    ) -> dict:
        raise ProviderError(
            "Transcript extraction is not supported by the Instagram provider."
        )

    def get_cookie_path(self) -> Optional[str]:
        return None


# ============================================================
# UNIVERSAL YT-DLP EXTRACTOR PROVIDER
# Tries any public HTTP(S) URL and lets installed yt-dlp extractors
# determine whether the source is supported.
# ============================================================

class UniversalYtDlpProvider(InstagramYtDlpProvider):
    """Fallback provider for the site's installed yt-dlp extractors."""

    BLOCKED_SUFFIXES = (
        ".localhost",
        ".local",
        ".internal",
        ".lan",
        ".home",
        ".test",
        ".invalid",
        ".example",
        ".onion",
    )

    def validate_url(self, url: str) -> bool:
        return validate_public_media_url(url)

    def extract_video_id(self, url: str) -> Optional[str]:
        try:
            parsed = urlparse(url)
        except ValueError:
            return None

        # Common query-string identifiers used by media sites.
        for key in ("v", "video_id", "video", "id", "clip", "post"):
            value = parsed.query
            if value:
                from urllib.parse import parse_qs
                candidate = parse_qs(value).get(key, [None])[0]
                if candidate:
                    return str(candidate)[:150]

        segments = [part for part in (parsed.path or "").split("/") if part]
        if segments:
            return segments[-1][:150]

        return (parsed.hostname or "media")[:150]


def validate_public_media_url(url: str) -> bool:
    """Allow public web URLs, rejecting local hosts and private IP targets."""
    if not isinstance(url, str) or not url.strip():
        return False

    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False

    if parsed.scheme.lower() not in {"http", "https"}:
        return False

    if not host or parsed.username or parsed.password:
        return False

    if port is not None:
        # Keep this downloader restricted to ordinary website ports.
        if (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            return False

    if host == "localhost" or host.endswith(UniversalYtDlpProvider.BLOCKED_SUFFIXES):
        return False

    # Do not accept a bare single-label hostname.
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            return False
        labels = host.split(".")
        if any(
            not label
            or len(label) > 63
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
            for label in labels
        ):
            return False

        # Resolve the hostname and reject local/private destinations.
        # This is a defense-in-depth check; it does not guarantee that
        # a site's later HTTP redirects will also be public.
        try:
            results = socket.getaddrinfo(
                host,
                port or (443 if parsed.scheme.lower() == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError:
            return False

        if not results:
            return False

        for result in results:
            try:
                resolved = ipaddress.ip_address(result[4][0].split("%")[0])
            except ValueError:
                return False
            if not resolved.is_global:
                return False
    else:
        # Require a public DNS hostname rather than a literal IP URL.
        return False

    return True


class UniversalExtractorURLValidator:
    """URLValidator-compatible facade for public media websites."""

    def validate(self, url: str) -> ValidationResult:
        if not validate_public_media_url(url):
            return ValidationResult(
                is_valid=False,
                error_message="URL must be a public HTTP(S) media page.",
            )
        return ValidationResult(is_valid=True, sanitized_value=url.strip())

    def is_valid(self, url: str) -> bool:
        return self.validate(url).is_valid


# The download endpoint has a built-in hostname allow-list that otherwise
# prevents yt-dlp from trying many of its supported extractors. Replace only
# that endpoint's validator with the public-URL validator above.
download.url_validator = UniversalExtractorURLValidator()
video.url_validator = UniversalExtractorURLValidator()
transcript.url_validator = UniversalExtractorURLValidator()

# Permit the quality selectors used by the Telegram bot.
download.format_validator.SPECIAL_SELECTORS = frozenset(
    set(download.format_validator.SPECIAL_SELECTORS)
    | {
        "best[height<=480]",
        "best[height<=720]",
        "best[height<=1080]",
    }
)


# ============================================================
# METRICS MIDDLEWARE
# ============================================================

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

        # Use FastAPI route template for normalized endpoint path.
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


# ============================================================
# GLOBAL SERVICE INSTANCES
# ============================================================

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


# ============================================================
# APPLICATION LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager for startup and shutdown."""
    global _provider_manager
    global _cookie_service
    global _cleanup_task
    global _disabled_providers

    logger.info(
        "Application starting",
        version=__version__,
    )

    # Initialize metrics with application version.
    initialize_metrics(__version__)

    # Load configuration.
    config_service = ConfigService()
    config = config_service.load()

    # Configure logging.
    configure_logging(
        config.logging.level,
        config.logging.format,
    )

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

    # Store disabled providers for later use.
    _disabled_providers = startup_result.disabled_providers

    # Configure authentication.
    configure_auth(
        api_keys=config.security.api_keys,
    )

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

    # Configure job service with callback to unregister files on expiry.
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

    # Configure webhook service (disabled unless explicitly enabled).
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

    # Configure cookie service.
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

    # Configure provider manager and register providers.
    _provider_manager = ProviderManager()

    # Register YouTube provider (skip if disabled by startup validation).
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

    # Register Instagram provider inline.
    # No app/providers/generic.py import or file is required.
    instagram_config = {
        "retry_attempts": config.providers.youtube.retry_attempts,
        "retry_backoff": config.providers.youtube.retry_backoff,
        "output_dir": config.storage.output_dir,
    }

    instagram_provider = InstagramYtDlpProvider(instagram_config)

    _provider_manager.register_provider(
        "instagram",
        instagram_provider,
        enabled=True,
    )

    logger.info("Instagram yt-dlp provider registered")

    # Register the universal provider last so the specific YouTube and
    # Instagram providers get first chance, then yt-dlp can try other sites.
    universal_config = {
        "retry_attempts": config.providers.youtube.retry_attempts,
        "retry_backoff": config.providers.youtube.retry_backoff,
        "output_dir": config.storage.output_dir,
    }
    universal_provider = UniversalYtDlpProvider(universal_config)
    _provider_manager.register_provider(
        "universal_ytdlp",
        universal_provider,
        enabled=True,
    )
    logger.info("Universal media extractor provider registered")

    # Configure download worker.
    worker = configure_download_worker(
        provider_manager=_provider_manager,
        job_service=job_service,
        download_queue=download_queue,
        storage_manager=storage,
    )

    # Start download worker.
    await worker.start()
    logger.info("Download worker started")

    # Start cleanup scheduler in background.
    from app.services.storage import cleanup_scheduler

    _cleanup_task = asyncio.create_task(
        cleanup_scheduler(storage, interval=3600)
    )

    logger.info("Cleanup scheduler started")

    logger.info(
        "Application startup complete",
        version=__version__,
    )

    yield

    # Shutdown.
    logger.info("Application shutting down")

    # Stop cleanup scheduler.
    if _cleanup_task:
        _cleanup_task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await _cleanup_task

    # Stop download worker.
    await worker.stop()

    logger.info("Application shutdown complete")


# ============================================================
# API DESCRIPTION
# ============================================================

API_DESCRIPTION = """
## Overview

REST API for media downloads and metadata extraction.
The universal provider lets the installed extractor engine attempt any
public website URL it recognizes. Successful extraction is not guaranteed;
source availability and authentication requirements still apply.

## Authentication

All endpoints except health checks require an API key in the `X-API-Key` header.

## Rate Limiting

- **Metadata operations** (info, formats): 100 requests/minute
- **Download operations**: 10 requests/minute

Rate limited responses include a `Retry-After` header.

## Error Codes

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `INVALID_URL` | 400 | URL is malformed or not from a supported domain |
| `INVALID_FORMAT` | 400 | Format ID is invalid |
| `INVALID_TEMPLATE` | 400 | Format template is invalid or contains path traversal |
| `FORMAT_NOT_FOUND` | 400 | Requested format is not available |
| `AUTH_FAILED` | 401 | API key is missing or invalid |
| `JOB_NOT_FOUND` | 404 | Job ID does not exist or has expired |
| `TRANSCRIPT_NOT_FOUND` | 404 | No transcript is available for the requested language |
| `VIDEO_UNAVAILABLE` | 404 | Video is private, deleted, or geo-blocked |
| `RATE_LIMIT_EXCEEDED` | 429 | Rate limit reached; check `Retry-After` |
| `WEBHOOK_NOT_ALLOWED` | 400 | Webhooks are disabled or host is not in the allow-list |
| `DOWNLOAD_FAILED` | 500 | Download operation failed |
| `PROVIDER_ERROR` | 500 | Provider operation failed |
| `QUEUE_FULL` | 503 | Queue is at capacity |
| `STORAGE_FULL` | 503 | Insufficient storage space |

## Async vs Sync Downloads

- **Async mode** (default): Returns HTTP 202 with `job_id` immediately.
  Poll `GET /api/v1/jobs/{job_id}` to track progress.
- **Sync mode**: Waits for download completion and returns HTTP 200 with the result.
"""


# ============================================================
# FASTAPI APPLICATION CREATION
# ============================================================

def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="YT-DLP REST API",
        description=API_DESCRIPTION,
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
        contact={
            "name": "API Support",
            "url": "https://github.com/fvadicamo/yt-dlp-api",
        },
        license_info={
            "name": "MIT",
            "url": "https://opensource.org/licenses/MIT",
        },
        openapi_tags=[
            {"name": "health", "description": "Health check endpoints for monitoring"},
            {"name": "video", "description": "Video metadata and format listing"},
            {"name": "download", "description": "Download operations"},
            {"name": "jobs", "description": "Job status tracking"},
            {"name": "admin", "description": "Cookie management"},
            {"name": "metrics", "description": "Prometheus metrics"},
        ],
    )

    # Add CORS middleware with configurable origins.
    security_config = SecurityConfig()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=security_config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Add metrics middleware (before rate limiting to capture all requests).
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RateLimitMiddleware)

    # Register global exception handlers.
    app.add_exception_handler(Exception, global_exception_handler)
    app.add_exception_handler(APIError, global_exception_handler)

    # Admin router dependencies.
    app.dependency_overrides[admin.get_cookie_service] = get_cookie_service

    # Video router dependencies.
    app.dependency_overrides[video.get_provider_manager] = get_provider_manager
    app.dependency_overrides[transcript.get_provider_manager] = get_provider_manager

    # Download router dependencies.
    app.dependency_overrides[download.get_provider_manager] = get_provider_manager
    app.dependency_overrides[download.get_job_service] = get_job_service
    app.dependency_overrides[download.get_download_queue] = get_download_queue
    app.dependency_overrides[download.get_download_worker] = get_download_worker

    # Jobs router dependencies.
    app.dependency_overrides[jobs.get_job_service] = get_job_service
    app.dependency_overrides[jobs.get_download_queue] = get_download_queue

    # Register routers.
    app.include_router(health.router)
    app.include_router(video.router)
    app.include_router(transcript.router)
    app.include_router(download.router)
    app.include_router(jobs.router)
    app.include_router(admin.router)
    app.include_router(metrics.router)

    return app


# Create the application instance.
app = create_app()


if __name__ == "__main__":
    import uvicorn  # type: ignore[import-not-found]

    uvicorn.run(app, host="0.0.0.0", port=8000)  # nosec B104
