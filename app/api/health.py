
"""Health check endpoints.

This module implements requirements 11, 30, and 37:
- Req 11: Health monitoring with component verification
- Req 30: Detailed health checks
- Req 37: Container health probes (liveness/readiness)
"""

import asyncio
import os
import shutil
import tempfile
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

# Cache the configured YouTube connectivity timeout.
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


def _get_ytdlp_js_runtime() -> tuple[str, str] | None:
    """Prefer Deno for yt-dlp JavaScript challenges, then Node.js.

    Returns:
        Runtime name and executable path, or None if neither is available.
    """
    deno_path = shutil.which("deno")

    if deno_path:
        return "deno", deno_path

    node_path = shutil.which("node")

    if node_path:
        return "node", node_path

    return None


async def _check_youtube_connectivity() -> ComponentHealth:
    """Check YouTube access using configured cookies and a JS runtime.

    Render secret files are read-only. A private, writable temporary
    cookie copy is used because yt-dlp may update the cookie jar.

    Diagnostic output is logged server-side. Cookie contents and
    authentication secrets are not returned to API clients.
    """
    proc = None
    communicate_task = None
    writable_cookie_path = None
    start_time = time.time()

    test_video_id = "jNQXAC9IVRw"
    test_url = f"https://www.youtube.com/watch?v={test_video_id}"

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

        cookie_file = Path(cookie_path)

        if not cookie_file.is_file():
            logger.warning(
                "youtube_connectivity_cookie_missing"
            )

            return ComponentHealth(
                status="unhealthy",
                details={
                    "error": "Configured YouTube cookie file was not found"
                },
            )

        runtime = _get_ytdlp_js_runtime()

        if runtime is None:
            logger.warning(
                "youtube_js_runtime_not_found"
            )

            return ComponentHealth(
                status="unhealthy",
                details={
                    "error": "Neither Deno nor Node.js was found"
                },
            )

        # Render Secret Files are read-only. Copy the cookie jar into a
        # writable temporary file owned by the running application user.
        fd, writable_cookie_path = tempfile.mkstemp(
            prefix="youtube-cookies-",
            suffix=".txt",
        )
        os.close(fd)

        shutil.copyfile(
            cookie_file,
            writable_cookie_path,
        )
        os.chmod(
            writable_cookie_path,
            0o600,
        )

        runtime_name, runtime_path = runtime
        timeout = _get_health_check_timeout()

        logger.info(
            "youtube_connectivity_check_started",
            js_runtime=runtime_name,
            timeout_seconds=timeout,
        )

        command = [
            "yt-dlp",
            "--verbose",
            "--socket-timeout",
            "10",
            "--retries",
            "0",
            "--extractor-retries",
            "0",
            "--simulate",
            "--no-playlist",
            "--skip-download",
            "--js-runtimes",
            f"{runtime_name}:{runtime_path}",
            "--cookies",
            writable_cookie_path,
            "--print",
            "id",
            test_url,
        ]

        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        communicate_task = asyncio.create_task(
            proc.communicate()
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communicate_task),
                timeout=float(timeout),
            )

        except asyncio.TimeoutError:
            # Terminate the process and attempt to capture diagnostic output.
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

            # Log only a bounded amount of output; never log the command
            # arguments or the contents of the cookie file.
            logger.warning(
                "youtube_connectivity_check_timeout",
                js_runtime=runtime_name,
                timeout_seconds=timeout,
                stdout=stdout.decode(
                    errors="replace"
                )[-500:],
                stderr=stderr.decode(
                    errors="replace"
                )[-1500:],
            )

            return ComponentHealth(
                status="unhealthy",
                details={
                    "error": (
                        "YouTube connectivity test timed out "
                        f"(>{timeout}s)"
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
                js_runtime=runtime_name,
                latency_ms=latency_ms,
            )

            return ComponentHealth(
                status="healthy",
                details={
                    "latency_ms": latency_ms,
                    "js_runtime": runtime_name,
                },
            )

        stderr_text = stderr.decode(
            errors="replace"
        )

        logger.warning(
            "youtube_connectivity_check_failed",
            js_runtime=runtime_name,
            returncode=proc.returncode,
            stderr=stderr_text[-1500:],
        )

        return ComponentHealth(
            status="unhealthy",
            details={
                "error": "YouTube connectivity test failed"
            },
        )

    except FileNotFoundError:
        return ComponentHealth(
            status="unhealthy",
            details={
                "error": (
                    "yt-dlp or a required executable was not found"
                )
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

    finally:
        # Ensure no subprocess is left running after a failure or cancellation.
        if proc is not None and proc.returncode is None:
            proc.kill()

            try:
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=3.0,
                )
            except Exception:
                pass

        if (
            communicate_task is not None
            and not communicate_task.done()
        ):
            communicate_task.cancel()

            try:
                await communicate_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        # Always remove the temporary writable cookie copy.
        if writable_cookie_path:
            try:
                Path(writable_cookie_path).unlink(
                    missing_ok=True
                )
            except OSError as cleanup_error:
                logger.warning(
                    "youtube_temporary_cookie_cleanup_failed",
                    error=str(cleanup_error),
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
    """Return detailed application health information.

    Checks yt-dlp, FFmpeg, Node.js, storage, cookie files,
    and YouTube connectivity.
    """
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
