"""Health check endpoints.

This module implements requirements 11, 30, and 37:
- Req 11: Health monitoring with component verification
- Req 30: Detailed health checks
- Req 37: Container health probes (liveness/readiness)
"""

import asyncio
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal

import structlog
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app import __version__
from app.api.schemas import (
    ComponentHealth,
    HealthResponse,
    LivenessResponse,
    ReadinessResponse,
)
from app.core.checks import check_ffmpeg, check_nodejs, check_ytdlp
from app.core.config import ConfigService


def _is_test_mode() -> bool:
    """Check if test mode is enabled via environment variable."""
    return os.environ.get(
        "APP_TESTING_TEST_MODE", ""
    ).lower() in ("true", "1", "yes")


# Capture test mode at module import time.
_TEST_MODE_CACHED = _is_test_mode()

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["health"])

# Track application start time for uptime calculation.
_start_time: float = time.time()

# Lazily loaded and cached from application configuration.
_health_check_timeout: int | None = None


def reset_start_time() -> None:
    """Reset the start time for testing."""
    global _start_time
    _start_time = time.time()


async def _check_ytdlp() -> ComponentHealth:
    """Check yt-dlp availability and version."""
    result = await check_ytdlp()

    if result.available:
        return ComponentHealth(
            status="healthy",
            version=result.version,
        )

    return ComponentHealth(
        status="unhealthy",
        details={
            "error": result.error or "yt-dlp not available"
        },
    )


async def _check_ffmpeg() -> ComponentHealth:
    """Check FFmpeg availability and version."""
    result = await check_ffmpeg()

    if result.available:
        return ComponentHealth(
            status="healthy",
            version=result.version,
        )

    return ComponentHealth(
        status="unhealthy",
        details={
            "error": result.error or "FFmpeg not available"
        },
    )


async def _check_nodejs() -> ComponentHealth:
    """Check that Node.js 22 or newer is available."""
    result = await check_nodejs(min_version=22)

    if result.available:
        return ComponentHealth(
            status="healthy",
            version=result.version,
        )

    return ComponentHealth(
        status="unhealthy",
        version=result.version,
        details={
            "error": result.error or "Node.js >= 22 not available"
        },
    )


def _check_storage() -> ComponentHealth:
    """Check storage availability."""
    try:
        from app.services.storage import get_storage_manager

        storage = get_storage_manager()
        usage = storage.get_disk_usage()

        return ComponentHealth(
            status="healthy",
            details={
                "available_gb": round(
                    usage.available / (1024**3), 2
                ),
                "used_percent": round(usage.percent_used, 1),
            },
        )

    except RuntimeError:
        return ComponentHealth(
            status="unhealthy",
            details={
                "error": "Storage manager not configured"
            },
        )

    except Exception as error:
        logger.warning(
            "storage_health_check_failed",
            error=str(error),
        )
        return ComponentHealth(
            status="unhealthy",
            details={"error": str(error)},
        )


def _get_health_check_timeout() -> int:
    """Get the configured YouTube connectivity timeout."""
    global _health_check_timeout

    if _health_check_timeout is None:
        _health_check_timeout = (
            ConfigService().load().timeouts.health_check
        )

    return _health_check_timeout


async def _check_youtube_connectivity() -> ComponentHealth:
    """Check YouTube access using configured cookies and Node.js.

    Uses a known public YouTube video to test whether yt-dlp can
    communicate with YouTube.

    The configured cookie file and JavaScript runtime are passed
    explicitly so the probe does not depend solely on yt-dlp's
    per-user configuration file.
    """
    proc = None
    start_time = time.time()
    test_video_id = "jNQXAC9IVRw"
    test_url = (
        f"https://www.youtube.com/watch?v={test_video_id}"
    )

    try:
        config = ConfigService().load()
        youtube_config = config.providers.youtube

        if not youtube_config.enabled:
            return ComponentHealth(
                status="unhealthy",
                details={
                    "error": "YouTube provider is disabled"
                },
            )

        cookie_path = str(
            youtube_config.cookie_path or ""
        ).strip()

        if not cookie_path:
            return ComponentHealth(
                status="unhealthy",
                details={
                    "error": "No cookie path configured for YouTube"
                },
            )

        if not Path(cookie_path).is_file():
            logger.warning(
                "youtube_connectivity_cookie_missing",
                cookie_path=cookie_path,
            )
            return ComponentHealth(
                status="unhealthy",
                details={
                    "error": "Configured YouTube cookie file was not found"
                },
            )

        node_path = shutil.which("node")

        if not node_path:
            return ComponentHealth(
                status="unhealthy",
                details={
                    "error": "Node.js runtime not found"
                },
            )

        timeout = _get_health_check_timeout()

        # Use the configured cookie file and Node.js executable
        # explicitly for this YouTube connectivity test.
        command = [
            "yt-dlp",
            "--simulate",
            "--no-playlist",
            "--skip-download",
            "--no-warnings",
            "--js-runtimes",
            f"node:{node_path}",
            "--cookies",
            cookie_path,
            "--print",
            "id",
            test_url,
        ]

        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        communicate_task = asyncio.create_task(proc.communicate())

try:
    stdout, stderr = await asyncio.wait_for(
        asyncio.shield(communicate_task),
        timeout=float(timeout),
    )
except asyncio.TimeoutError:
    if proc.returncode is None:
        proc.kill()

    try:
        stdout, stderr = await asyncio.wait_for(
            communicate_task,
            timeout=3.0,
        )
    except Exception as capture_error:
        stdout, stderr = b"", b""
        logger.warning(
            "youtube_timeout_output_capture_failed",
            error=str(capture_error),
        )

    logger.warning(
        "youtube_connectivity_check_timeout",
        timeout_seconds=timeout,
        stdout=stdout.decode(errors="replace")[-500:],
        stderr=stderr.decode(errors="replace")[-1500:],
    )

    return ComponentHealth(
        status="unhealthy",
        details={
            "error": (
                f"YouTube connectivity test timed out (>{timeout}s)"
            )
        },
    )
        latency_ms = int(
            (time.time() - start_time) * 1000
        )

        if (
            proc.returncode == 0
            and test_video_id.encode() in stdout
        ):
            logger.info(
                "youtube_connectivity_check_passed",
                latency_ms=latency_ms,
            )

            return ComponentHealth(
                status="healthy",
                details={"latency_ms": latency_ms},
            )

        # Log the diagnostic output for debugging. Do not expose
        # command arguments, cookie contents, or secrets to clients.
        stderr_text = stderr.decode(
            errors="replace"
        )

        logger.warning(
            "youtube_connectivity_check_failed",
            returncode=proc.returncode,
            stderr=stderr_text[:1000],
        )

        return ComponentHealth(
            status="unhealthy",
            details={
                "error": "YouTube connectivity test failed"
            },
        )

    except asyncio.TimeoutError:
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()

        logger.warning(
            "youtube_connectivity_check_timeout",
            timeout_seconds=_get_health_check_timeout(),
        )

        return ComponentHealth(
            status="unhealthy",
            details={
                "error": (
                    "YouTube connectivity test timed out "
                    f"(>{_get_health_check_timeout()}s)"
                )
            },
        )

    except FileNotFoundError:
        return ComponentHealth(
            status="unhealthy",
            details={
                "error": "yt-dlp or a required executable was not found"
            },
        )

    except Exception as error:
        logger.warning(
            "youtube_connectivity_check_error",
            error=str(error),
        )

        return ComponentHealth(
            status="unhealthy",
            details={
                "error": "YouTube connectivity test encountered an error"
            },
        )


def _check_cookies() -> ComponentHealth:
    """Check whether configured cookie files are available."""
    try:
        from app.main import get_cookie_service

        cookie_service = get_cookie_service()
        providers = (
            cookie_service.list_providers_with_cookies()
        )

        if not providers:
            return ComponentHealth(
                status="unhealthy",
                details={
                    "error": "No cookie files configured"
                },
            )

        healthy_providers = [
            provider
            for provider, info in providers.items()
            if info.get("exists", False)
        ]

        if healthy_providers:
            details: Dict[str, Any] = {}

            for provider, info in providers.items():
                details[provider] = {
                    "exists": info.get("exists", False),
                    "age_hours": info.get("age_hours"),
                }

                age_hours = info.get("age_hours")

                if (
                    age_hours is not None
                    and age_hours > 168
                ):
                    details[provider]["warning"] = (
                        "Cookie file is older than 7 days"
                    )

            return ComponentHealth(
                status="healthy",
                details=details,
            )

        return ComponentHealth(
            status="unhealthy",
            details={
                "error": "No valid cookie files found",
                "providers": providers,
            },
        )

    except RuntimeError:
        return ComponentHealth(
            status="unhealthy",
            details={
                "error": "Cookie service not configured"
            },
        )

    except Exception as error:
        logger.warning(
            "cookie_health_check_failed",
            error=str(error),
        )

        return ComponentHealth(
            status="unhealthy",
            details={"error": str(error)},
        )


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={
        200: {"description": "All components healthy"},
        503: {
            "description": "One or more components unhealthy"
        },
    },
)
async def health_check() -> JSONResponse:
    """Return detailed health status for the application.

    Verifies yt-dlp, FFmpeg, Node.js, storage, cookie files,
    and YouTube connectivity.
    """
    # Run independent asynchronous checks concurrently.
    (
        ytdlp_health,
        ffmpeg_health,
        nodejs_health,
        youtube_health,
    ) = await asyncio.gather(
        _check_ytdlp(),
        _check_ffmpeg(),
        _check_nodejs(),
        _check_youtube_connectivity(),
    )

    storage_health = _check_storage()
    cookie_health = _check_cookies()

    components = {
        "ytdlp": ytdlp_health,
        "ffmpeg": ffmpeg_health,
        "nodejs": nodejs_health,
        "storage": storage_health,
        "cookie": cookie_health,
        "youtube_connectivity": youtube_health,
    }

    all_healthy = all(
        component.status == "healthy"
        for component in components.values()
    )

    overall_status: Literal["healthy", "unhealthy"] = (
        "healthy" if all_healthy else "unhealthy"
    )

    response = HealthResponse(
        status=overall_status,
        timestamp=datetime.now(timezone.utc).isoformat(),
        version=__version__,
        uptime_seconds=round(
            time.time() - _start_time, 2
        ),
        test_mode=_TEST_MODE_CACHED,
        components=components,
    )

    status_code = (
        status.HTTP_200_OK
        if all_healthy
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )

    logger.info(
        "health_check_completed",
        status=overall_status,
        components={
            name: component.status
            for name, component in components.items()
        },
    )

    return JSONResponse(
        content=response.model_dump(),
        status_code=status_code,
    )


@router.get(
    "/liveness",
    response_model=LivenessResponse,
)
async def liveness_check() -> LivenessResponse:
    """Return whether the application process is alive."""
    return LivenessResponse(status="alive")


@router.get(
    "/readiness",
    response_model=ReadinessResponse,
    responses={
        200: {
            "description": "Service is ready to accept traffic"
        },
        503: {
            "description": "Service is not ready"
        },
    },
)
async def readiness_check() -> JSONResponse:
    """Check whether the service is ready to accept traffic.

    Readiness requires yt-dlp and storage to be available.
    """
    issues = []

    ytdlp_health = await _check_ytdlp()

    if ytdlp_health.status != "healthy":
        issues.append("yt-dlp not available")

    storage_health = _check_storage()

    if storage_health.status != "healthy":
        issues.append("Storage not ready")

    if issues:
        response = ReadinessResponse(
            status="not_ready",
            ready=False,
            message="; ".join(issues),
        )

        return JSONResponse(
            content=response.model_dump(),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return JSONResponse(
        content=ReadinessResponse(
            status="ready",
            ready=True,
        ).model_dump(),
        status_code=status.HTTP_200_OK,
    )
