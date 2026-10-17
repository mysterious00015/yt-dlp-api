"""Download API endpoint.

Implements POST /api/v1/download with async and sync modes.
Supports dedicated providers and the generic yt-dlp provider.
"""

from urllib.parse import urlsplit

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from app.api.schemas import (
    DownloadRequest,
    DownloadResponse,
    SyncDownloadResponse,
)
from app.core.template import TemplateProcessor
from app.core.validation import FormatValidator, URLValidator
from app.middleware.auth import require_api_key
from app.models.job import JobStatus
from app.providers.exceptions import InvalidURLError
from app.providers.manager import ProviderManager
from app.services.download_queue import (
    PRIORITY_DOWNLOAD,
    DownloadQueue,
)
from app.services.download_worker import DownloadWorker
from app.services.job_service import JobService
from app.services.webhook_service import (
    WebhookService,
    get_webhook_service,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["download"])

# Validators
url_validator = URLValidator()
format_validator = FormatValidator()
template_processor = TemplateProcessor()


# Dependency placeholders configured by the main app.
async def get_provider_manager() -> ProviderManager:
    """Get the provider manager instance."""
    raise NotImplementedError("Provider manager dependency not configured")


async def get_job_service() -> JobService:
    """Get the job service instance."""
    raise NotImplementedError("Job service dependency not configured")


async def get_download_queue() -> DownloadQueue:
    """Get the download queue instance."""
    raise NotImplementedError("Download queue dependency not configured")


async def get_download_worker() -> DownloadWorker:
    """Get the download worker instance."""
    raise NotImplementedError("Download worker dependency not configured")


def _safe_hostname(url: str) -> str:
    """Return the URL hostname without query strings or credentials."""
    try:
        return (urlsplit(url).hostname or "unknown")[:200]
    except ValueError:
        return "unknown"


@router.post(
    "/download",
    dependencies=[Depends(require_api_key)],
    responses={
        200: {
            "description": "Synchronous download completed",
            "model": SyncDownloadResponse,
        },
        202: {
            "description": "Asynchronous download job created",
            "model": DownloadResponse,
        },
        400: {"description": "Invalid request"},
        404: {"description": "Media not found"},
        500: {"description": "Server error"},
    },
)
async def download_video(
    request: DownloadRequest,
    provider_manager: ProviderManager = Depends(get_provider_manager),
    job_service: JobService = Depends(get_job_service),
    download_queue: DownloadQueue = Depends(get_download_queue),
    download_worker: DownloadWorker = Depends(get_download_worker),
    webhook_service: WebhookService = Depends(get_webhook_service),
) -> JSONResponse:
    """Validate a media URL, select a provider, and create a download job.

    Async mode:
        Enqueues a job and returns HTTP 202 with a job ID.

    Sync mode:
        Processes the job and returns HTTP 200 when complete.
    """
    logger.info(
        "download_requested",
        hostname=_safe_hostname(request.url),
        format_id=request.format_id,
        async_mode=request.async_mode,
        extract_audio=request.extract_audio,
    )

    # --------------------------------------------------------
    # 1. Validate the media URL
    # --------------------------------------------------------
    url_validation = url_validator.validate(request.url)

    if not url_validation.is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_URL",
                "message": url_validation.error_message,
            },
        )

    # Use the validated value when the validator normalizes the URL.
    media_url = url_validation.sanitized_value or request.url

    # --------------------------------------------------------
    # 2. Validate the requested format selector
    # --------------------------------------------------------
    if request.format_id:
        format_validation = format_validator.validate_format_id(
            request.format_id
        )

        if not format_validation.is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": "INVALID_FORMAT",
                    "message": format_validation.error_message,
                },
            )

    # --------------------------------------------------------
    # 3. Validate the output filename template
    # --------------------------------------------------------
    if request.output_template:
        template_result = template_processor.validate_template(
            request.output_template
        )

        if not template_result.is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": "INVALID_TEMPLATE",
                    "message": template_result.error_message,
                },
            )

    # --------------------------------------------------------
    # 4. Validate the optional webhook URL
    # --------------------------------------------------------
    if request.webhook_url:
        webhook_validation = webhook_service.validate_url(
            request.webhook_url
        )

        if not webhook_validation.is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": "WEBHOOK_NOT_ALLOWED",
                    "message": webhook_validation.error_message,
                },
            )

    # --------------------------------------------------------
    # 5. Select an enabled provider
    #
    # ProviderManager prioritizes the dedicated YouTube provider
    # and uses generic_ytdlp as the fallback for recognized sites.
    # --------------------------------------------------------
    try:
        provider = provider_manager.get_provider_for_url(media_url)

        logger.info(
            "download_provider_selected",
            provider=getattr(provider, "name", type(provider).__name__),
            hostname=_safe_hostname(media_url),
        )

    except InvalidURLError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_URL",
                "message": str(exc),
            },
        ) from exc

    # --------------------------------------------------------
    # 6. Prepare the download parameters
    # --------------------------------------------------------
    params = {
        "format_id": request.format_id,
        "output_template": request.output_template,
        "extract_audio": request.extract_audio,
        "audio_format": request.audio_format,
        "include_subtitles": request.include_subtitles,
        "subtitle_lang": request.subtitle_lang,
        "webhook_url": request.webhook_url,
    }

    # --------------------------------------------------------
    # 7. Create the job
    # --------------------------------------------------------
    job = job_service.create_job(
        url=media_url,
        params=params,
    )

    # --------------------------------------------------------
    # 8. Asynchronous mode: queue the job and return its ID
    # --------------------------------------------------------
    if request.async_mode:
        try:
            position = await download_queue.enqueue(
                job.job_id,
                priority=PRIORITY_DOWNLOAD,
            )

            job_service.set_queue_position(
                job.job_id,
                position,
            )

        except ValueError as exc:
            job_service.fail_job(
                job.job_id,
                str(exc),
            )

            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error_code": "QUEUE_FULL",
                    "message": str(exc),
                },
            ) from exc

        logger.info(
            "download_job_created",
            job_id=job.job_id,
            queue_position=position,
            provider=getattr(provider, "name", type(provider).__name__),
        )

        response = DownloadResponse(
            job_id=job.job_id,
            status=job.status.value,
            created_at=job.created_at.isoformat(),
            queue_position=position,
            message="Download job created and queued",
        )

        return JSONResponse(
            content=response.model_dump(),
            status_code=status.HTTP_202_ACCEPTED,
        )

    # --------------------------------------------------------
    # 9. Synchronous mode: acquire a slot and process the job
    # --------------------------------------------------------
    slot_acquired = await download_queue.acquire_slot_for_sync(
        job.job_id
    )

    if not slot_acquired:
        job_service.fail_job(
            job.job_id,
            "No download slots available",
        )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "NO_SLOTS_AVAILABLE",
                "message": (
                    "All download slots are in use. "
                    "Try again later or use async mode."
                ),
            },
        )

    logger.info(
        "sync_download_started",
        job_id=job.job_id,
        provider=getattr(provider, "name", type(provider).__name__),
    )

    try:
        await download_worker.process_single_job(job.job_id)

        updated_job = job_service.get_job(job.job_id)

        if updated_job is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error_code": "INTERNAL_ERROR",
                    "message": "Job not found after processing",
                },
            )

        if updated_job.status == JobStatus.COMPLETED:
            logger.info(
                "sync_download_completed",
                job_id=job.job_id,
                file_size=updated_job.file_size,
                provider=getattr(
                    provider,
                    "name",
                    type(provider).__name__,
                ),
            )

            sync_response = SyncDownloadResponse(
                file_path=updated_job.file_path or "",
                file_size=updated_job.file_size or 0,
                duration=updated_job.duration or 0.0,
                format_id=request.format_id or "best",
            )

            return JSONResponse(
                content=sync_response.model_dump(),
                status_code=status.HTTP_200_OK,
            )

        if updated_job.status == JobStatus.FAILED:
            error_message = (
                updated_job.error_message or "Download failed"
            )

            if "unavailable" in error_message.lower():
                status_code = status.HTTP_404_NOT_FOUND
                error_code = "VIDEO_UNAVAILABLE"

            elif "format" in error_message.lower():
                status_code = status.HTTP_400_BAD_REQUEST
                error_code = "FORMAT_NOT_FOUND"

            else:
                status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
                error_code = "DOWNLOAD_FAILED"

            raise HTTPException(
                status_code=status_code,
                detail={
                    "error_code": error_code,
                    "message": error_message,
                },
            )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "INTERNAL_ERROR",
                "message": (
                    f"Unexpected job status: "
                    f"{updated_job.status.value}"
                ),
            },
        )

    except HTTPException:
        raise

    except Exception as exc:
        logger.error(
            "sync_download_error",
            job_id=job.job_id,
            error=str(exc),
            exc_info=True,
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "DOWNLOAD_FAILED",
                "message": str(exc),
            },
        ) from exc

    finally:
        # Always release the concurrency slot.
        await download_queue.release_slot(job.job_id)
