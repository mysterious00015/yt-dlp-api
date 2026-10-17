"""Download worker for processing queued download jobs.

Handles queued downloads, provider selection, retries, job status,
storage registration, metrics, and completion webhooks.
"""

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import structlog

from app.core.metrics import MetricsCollector
from app.models.video import DownloadResult
from app.providers.base import VideoProvider
from app.providers.exceptions import DownloadError, ProviderError
from app.providers.manager import ProviderManager
from app.services.download_queue import (
    DownloadQueue,
    get_download_queue,
)
from app.services.job_service import (
    JobService,
    get_job_service,
)
from app.services.storage import (
    StorageManager,
    get_storage_manager,
)
from app.services.webhook_service import get_webhook_service

logger = structlog.get_logger(__name__)


def _safe_hostname(url: str) -> str:
    """Return a hostname for logging without URL query parameters."""
    try:
        return (urlparse(url).hostname or "unknown")[:200]
    except (TypeError, ValueError):
        return "unknown"


class DownloadWorker:
    """Worker that processes download jobs from the queue.

    Responsibilities:
    - Dequeue jobs when capacity is available.
    - Select a provider dynamically for each URL.
    - Execute downloads through the selected provider.
    - Retry eligible download failures.
    - Update job status and download metrics.
    - Register completed files with storage.
    - Notify configured webhooks about terminal job states.
    """

    def __init__(
        self,
        provider_manager: ProviderManager,
        job_service: Optional[JobService] = None,
        download_queue: Optional[DownloadQueue] = None,
        storage_manager: Optional[StorageManager] = None,
        poll_interval: float = 1.0,
    ) -> None:
        """Initialize the download worker."""
        self.provider_manager = provider_manager
        self._job_service = job_service
        self._download_queue = download_queue
        self._storage_manager = storage_manager
        self.poll_interval = poll_interval

        self._running = False
        self._worker_task: Optional[asyncio.Task] = None

        # Keep references to active webhook-delivery tasks.
        self._webhook_tasks: set[asyncio.Task] = set()

        logger.debug(
            "download_worker_initialized",
            poll_interval=poll_interval,
        )

    @property
    def job_service(self) -> JobService:
        """Get the configured job service."""
        return self._job_service or get_job_service()

    @property
    def download_queue(self) -> DownloadQueue:
        """Get the configured download queue."""
        return self._download_queue or get_download_queue()

    @property
    def storage_manager(self) -> StorageManager:
        """Get the configured storage manager."""
        return self._storage_manager or get_storage_manager()

    async def start(self) -> None:
        """Start the background queue-processing loop."""
        if self._running:
            logger.warning("download_worker_already_running")
            return

        self._running = True
        self._worker_task = asyncio.create_task(self._run())

        logger.info("download_worker_started")

    async def stop(self) -> None:
        """Stop the worker loop gracefully."""
        if not self._running:
            return

        self._running = False

        if self._worker_task:
            self._worker_task.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task

            self._worker_task = None

        logger.info("download_worker_stopped")

    async def _run(self) -> None:
        """Poll the queue and process jobs as they become available."""
        logger.info("download_worker_loop_started")

        while self._running:
            try:
                job_id = await self.download_queue.dequeue()

                if job_id:
                    asyncio.create_task(self._process_job(job_id))
                else:
                    await asyncio.sleep(self.poll_interval)

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                logger.error(
                    "download_worker_loop_error",
                    error=str(exc),
                    exc_info=True,
                )
                await asyncio.sleep(self.poll_interval)

    async def _process_job(self, job_id: str) -> None:
        """Process one queued download job."""
        job = self.job_service.get_job(job_id)

        if not job:
            logger.error("job_not_found_for_processing", job_id=job_id)
            await self.download_queue.release_slot(job_id)
            return

        logger.info(
            "job_processing_started",
            job_id=job_id,
            hostname=_safe_hostname(job.url),
            retry_count=job.retry_count,
        )

        start_time = time.time()
        provider_name = "unselected"
        download_status = "failed"
        download_size = 0

        try:
            self.job_service.start_processing(job_id)

            # Each URL is matched against enabled providers.
            # The manager prioritizes the dedicated YouTube provider
            # and uses generic_ytdlp as the fallback provider.
            provider = self.provider_manager.get_provider_for_url(job.url)

            provider_name = getattr(
                provider,
                "name",
                type(provider).__name__,
            )

            logger.info(
                "job_provider_selected",
                job_id=job_id,
                provider=provider_name,
                hostname=_safe_hostname(job.url),
            )

            result = await self._execute_download(job_id, provider)

            if result:
                download_status = "success"
                download_size = result.file_size

                file_path = Path(result.file_path)

                self.storage_manager.register_active_job(
                    job_id,
                    file_path,
                )

                self.job_service.complete_job(
                    job_id=job_id,
                    file_path=result.file_path,
                    file_size=result.file_size,
                    duration=result.duration,
                )

                logger.info(
                    "job_completed_successfully",
                    job_id=job_id,
                    provider=provider_name,
                    file_size=result.file_size,
                    duration=result.duration,
                )

                self._notify_webhook(job_id, "job.completed")

        except DownloadError as exc:
            await self._handle_download_error(job_id, exc)

        except ProviderError as exc:
            self.job_service.fail_job(job_id, str(exc))

            logger.error(
                "job_failed_provider_error",
                job_id=job_id,
                provider=provider_name,
                error=str(exc),
            )

            self._notify_webhook(job_id, "job.failed")

        except Exception as exc:
            self.job_service.fail_job(
                job_id,
                f"Unexpected error: {str(exc)}",
            )

            logger.error(
                "job_failed_unexpected_error",
                job_id=job_id,
                provider=provider_name,
                error=str(exc),
                exc_info=True,
            )

            self._notify_webhook(job_id, "job.failed")

        finally:
            download_duration = time.time() - start_time

            MetricsCollector.record_download(
                provider=provider_name,
                status=download_status,
                duration=download_duration,
                size=download_size,
            )

            await self.download_queue.release_slot(job_id)

    def _notify_webhook(self, job_id: str, event: str) -> None:
        """Deliver a configured webhook for a terminal job state."""
        job = self.job_service.get_job(job_id)

        if not job:
            return

        webhook_url = (job.params or {}).get("webhook_url")

        if not webhook_url:
            return

        service = get_webhook_service()

        if not service.enabled:
            return

        payload = {
            "event": event,
            "job_id": job.job_id,
            "url": job.url,
            "status": job.status.value,
            "file_path": job.file_path,
            "file_size": job.file_size,
            "duration": job.duration,
            "error_message": job.error_message,
            "completed_at": (
                job.completed_at.isoformat()
                if job.completed_at
                else None
            ),
        }

        task = asyncio.create_task(
            service.deliver(webhook_url, event, payload)
        )

        self._webhook_tasks.add(task)
        task.add_done_callback(self._webhook_tasks.discard)

    async def _execute_download(
        self,
        job_id: str,
        provider: VideoProvider,
    ) -> DownloadResult:
        """Execute a job through its selected provider."""
        job = self.job_service.get_job(job_id)

        if not job:
            raise ProviderError(f"Job not found: {job_id}")

        params = job.params or {}

        result = await provider.download(
            url=job.url,
            format_id=params.get("format_id"),
            output_template=params.get("output_template"),
            extract_audio=params.get("extract_audio", False),
            audio_format=params.get("audio_format"),
            include_subtitles=params.get("include_subtitles", False),
            subtitle_lang=params.get("subtitle_lang"),
        )

        return result

    async def _handle_download_error(
        self,
        job_id: str,
        error: DownloadError,
    ) -> None:
        """Retry eligible download errors or mark the job as failed."""
        job = self.job_service.get_job(job_id)

        if not job:
            return

        if job.can_retry():
            self.job_service.start_retry(job_id)

            logger.warning(
                "job_retrying",
                job_id=job_id,
                retry_count=job.retry_count,
                max_retries=job.max_retries,
                error=str(error),
            )

            try:
                await self.download_queue.enqueue(job_id)

            except ValueError as queue_error:
                self.job_service.fail_job(
                    job_id,
                    f"Retry failed: queue full. Original error: {str(error)}",
                )

                logger.error(
                    "job_retry_failed_queue_full",
                    job_id=job_id,
                    retry_count=job.retry_count,
                    queue_error=str(queue_error),
                    original_error=str(error),
                )

                self._notify_webhook(job_id, "job.failed")
                return

        else:
            self.job_service.fail_job(
                job_id,
                f"Max retries ({job.max_retries}) exceeded. "
                f"Last error: {str(error)}",
            )

            logger.error(
                "job_failed_max_retries",
                job_id=job_id,
                retry_count=job.retry_count,
                error=str(error),
            )

            self._notify_webhook(job_id, "job.failed")

    async def process_single_job(self, job_id: str) -> None:
        """Process one job directly, for synchronous mode or tests."""
        await self._process_job(job_id)


# Global download worker instance.
_download_worker: Optional[DownloadWorker] = None


def configure_download_worker(
    provider_manager: ProviderManager,
    job_service: Optional[JobService] = None,
    download_queue: Optional[DownloadQueue] = None,
    storage_manager: Optional[StorageManager] = None,
    poll_interval: float = 1.0,
) -> DownloadWorker:
    """Configure and initialize the global download worker."""
    global _download_worker

    _download_worker = DownloadWorker(
        provider_manager=provider_manager,
        job_service=job_service,
        download_queue=download_queue,
        storage_manager=storage_manager,
        poll_interval=poll_interval,
    )

    return _download_worker


def get_download_worker() -> DownloadWorker:
    """Get the configured global download worker."""
    if _download_worker is None:
        raise RuntimeError(
            "Download worker not configured. "
            "Call configure_download_worker() first."
        )

    return _download_worker


async def start_download_worker() -> None:
    """Start the global download worker."""
    worker = get_download_worker()
    await worker.start()


async def stop_download_worker() -> None:
    """Stop the global download worker."""
    worker = get_download_worker()
    await worker.stop()
