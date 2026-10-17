
"""Job status and completed-file download API endpoints."""

import mimetypes
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from app.api.schemas import JobStatusResponse
from app.core.config import ConfigService
from app.middleware.auth import require_api_key
from app.models.job import JobStatus
from app.services.download_queue import DownloadQueue
from app.services.job_service import JobNotFoundError, JobService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["jobs"])


async def get_job_service() -> JobService:
    """Get configured job service instance."""
    raise NotImplementedError(
        "Job service dependency not configured"
    )


async def get_download_queue() -> DownloadQueue:
    """Get configured download queue instance."""
    raise NotImplementedError(
        "Download queue dependency not configured"
    )


def _get_job_or_404(job_id: str, job_service: JobService):
    """Retrieve a job or raise a consistent 404 response."""
    try:
        return job_service.get_job_or_raise(job_id)
    except JobNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "JOB_NOT_FOUND",
                "message": "Download job was not found or has expired.",
            },
        ) from error


@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    dependencies=[Depends(require_api_key)],
    responses={
        404: {"description": "Job not found"},
        500: {"description": "Server error"},
    },
)
async def get_job_status(
    job_id: str,
    job_service: JobService = Depends(get_job_service),
    download_queue: DownloadQueue = Depends(get_download_queue),
) -> Any:
    """Get job status, progress, result path, and error details."""
    logger.debug("job_status_requested", job_id=job_id)

    job = _get_job_or_404(job_id, job_service)

    queue_position = None
    if job.status == JobStatus.PENDING:
        queue_position = download_queue.get_queue_position(job_id)

    response = JobStatusResponse(
        job_id=job.job_id,
        status=job.status.value,
        url=job.url,
        progress=job.progress,
        retry_count=job.retry_count,
        error_message=job.error_message,
        file_path=job.file_path,
        file_size=job.file_size,
        duration=job.duration,
        created_at=job.created_at.isoformat(),
        started_at=(
            job.started_at.isoformat() if job.started_at else None
        ),
        completed_at=(
            job.completed_at.isoformat() if job.completed_at else None
        ),
        queue_position=queue_position or job.queue_position,
    )

    logger.debug(
        "job_status_retrieved",
        job_id=job_id,
        status=job.status.value,
    )

    return response


@router.get(
    "/jobs/{job_id}/file",
    dependencies=[Depends(require_api_key)],
    responses={
        200: {"description": "Completed media file"},
        404: {"description": "Job or file not found"},
        409: {"description": "Download is not completed"},
        403: {"description": "File is outside the permitted output location"},
    },
)
async def download_completed_job_file(
    job_id: str,
    job_service: JobService = Depends(get_job_service),
) -> FileResponse:
    """Stream a completed job's media file to an authenticated client.

    The path comes from the server-side job record, not from a query
    parameter. Files are restricted to the configured output directory
    or a file directly inside /app, which accommodates the location
    reported by the successful test download.
    """
    job = _get_job_or_404(job_id, job_service)

    if job.status != JobStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "JOB_NOT_COMPLETED",
                "message": (
                    "The media file is available only after the job "
                    "has completed successfully."
                ),
                "status": job.status.value,
            },
        )

    if not job.file_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "FILE_NOT_FOUND",
                "message": "The completed job has no file recorded.",
            },
        )

    try:
        file_path = Path(job.file_path).resolve()
        output_dir = Path(
            ConfigService().load().storage.output_dir
        ).resolve()
        app_root = Path("/app").resolve()

        # Normal output directory, or a file directly in /app.
        allowed = (
            file_path.is_relative_to(output_dir)
            or file_path.parent == app_root
        )

        if not allowed:
            logger.warning(
                "job_file_outside_output_directory",
                job_id=job_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "FILE_PATH_NOT_ALLOWED",
                    "message": "The file is outside the permitted output location.",
                },
            )

        if not file_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "FILE_NOT_FOUND",
                    "message": "The downloaded file no longer exists.",
                },
            )

        media_type, _ = mimetypes.guess_type(file_path.name)
        media_type = media_type or "application/octet-stream"

        logger.info(
            "completed_job_file_requested",
            job_id=job_id,
            file_size=file_path.stat().st_size,
        )

        return FileResponse(
            path=str(file_path),
            media_type=media_type,
            filename=file_path.name,
            headers={"Cache-Control": "no-store"},
        )

    except HTTPException:
        raise

    except OSError as error:
        logger.error(
            "completed_job_file_read_failed",
            job_id=job_id,
            error=str(error),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "FILE_READ_FAILED",
                "message": "The completed media file could not be read.",
            },
        ) from error
