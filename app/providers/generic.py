"""Generic yt-dlp provider for recognized non-YouTube websites."""

import asyncio
import ipaddress
import json
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import structlog

from app.models.video import DownloadResult, VideoFormat
from app.providers.exceptions import (
    DownloadError,
    InvalidURLError,
    TranscriptNotFoundError,
)
from app.providers.youtube import YouTubeProvider
from app.utils.transcript import parse_vtt

logger = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def _get_site_extractors():
    """Load specific yt-dlp extractors, excluding the catch-all extractor."""
    from yt_dlp.extractor import gen_extractor_classes

    extractors = []

    for extractor in gen_extractor_classes():
        extractor_name = str(
            getattr(extractor, "IE_NAME", "") or ""
        ).lower()

        class_name = str(
            getattr(extractor, "__name__", "") or ""
        ).lower()

        # Arbitrary URLs must not qualify merely because the generic
        # extractor can try to inspect an ordinary webpage.
        if extractor_name in {"generic", "generic:search"}:
            continue

        if class_name in {"genericie", "genericauthie"}:
            continue

        extractors.append(extractor)

    return tuple(extractors)


def _is_youtube_host(host: str) -> bool:
    """Identify YouTube hosts reserved for the dedicated provider."""
    host = host.lower().rstrip(".")

    return (
        host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtu.be"
        or host == "youtube-nocookie.com"
        or host.endswith(".youtube-nocookie.com")
    )


class GenericYtDlpProvider(YouTubeProvider):
    """Downloads media from recognized non-YouTube yt-dlp sites.

    Reuses the existing YouTube provider's retry, parsing, and subprocess
    infrastructure while intentionally not using YouTube cookies.
    """

    name = "generic_ytdlp"

    def __init__(self, config: dict):
        super().__init__(
            config=config,
            cookie_service=None,
        )

        self.output_dir = Path(
            config.get("output_dir") or "/app/downloads"
        ).resolve()

        self.output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        logger.info(
            "Generic yt-dlp provider initialized",
            output_dir=str(self.output_dir),
            retry_attempts=self.retry_attempts,
        )

    def validate_url(self, url: str) -> bool:
        """Accept recognized public HTTP(S) URLs, excluding YouTube."""
        if not isinstance(url, str) or not url.strip():
            return False

        try:
            parsed = urlparse(url.strip())

            if parsed.scheme.lower() not in {"http", "https"}:
                return False

            # Reject embedded credentials.
            if parsed.username or parsed.password:
                return False

            host = (parsed.hostname or "").lower().rstrip(".")

            if not host:
                return False

            # Reject local and internal hostnames.
            if (
                host == "localhost"
                or host.endswith(
                    (
                        ".localhost",
                        ".local",
                        ".internal",
                        ".test",
                    )
                )
            ):
                return False

            # Reject direct IP-address URLs.
            try:
                ipaddress.ip_address(host)
                return False
            except ValueError:
                pass

            # The dedicated provider must handle YouTube.
            if _is_youtube_host(host):
                return False

            # Require a site-specific extractor to recognize the URL.
            for extractor in _get_site_extractors():
                try:
                    if extractor.suitable(url.strip()):
                        return True
                except Exception:
                    continue

            return False

        except (TypeError, ValueError):
            return False

    def get_cookie_path(self) -> Optional[str]:
        """Generic downloads do not use the YouTube cookie file."""
        return None

    async def get_info(
        self,
        url: str,
        include_formats: bool = False,
        include_subtitles: bool = False,
    ) -> Dict[str, Any]:
        """Extract metadata for a recognized non-YouTube URL."""
        if not self.validate_url(url):
            raise InvalidURLError(
                "URL is not recognized by the generic yt-dlp provider."
            )

        command = [
            "yt-dlp",
            "--ignore-config",
            "--no-warnings",
            "--no-playlist",
            "--skip-download",
            "--dump-single-json",
            url,
        ]

        logger.info(
            "Getting generic media information",
            host=urlparse(url).hostname,
            include_formats=include_formats,
            include_subtitles=include_subtitles,
        )

        try:
            result = await self._execute_with_retry(
                command,
                timeout=60.0,
            )

            info = json.loads(
                result.stdout.decode(
                    "utf-8",
                    errors="replace",
                )
            )

        except json.JSONDecodeError as exc:
            raise DownloadError(
                "Could not parse media information returned by yt-dlp."
            ) from exc

        video_info: Dict[str, Any] = {
            "video_id": str(info.get("id") or ""),
            "title": str(info.get("title") or "Untitled"),
            "duration": self._to_int(info.get("duration")) or 0,
            "author": str(
                info.get("uploader") or info.get("channel") or ""
            ),
            "upload_date": str(info.get("upload_date") or ""),
            "view_count": self._to_int(info.get("view_count")) or 0,
            "thumbnail_url": str(info.get("thumbnail") or ""),
            "description": str(info.get("description") or ""),
        }

        if include_formats and isinstance(info.get("formats"), list):
            video_info["formats"] = self._parse_formats(
                info["formats"]
            )

        if include_subtitles:
            video_info["subtitles"] = self._parse_subtitles(
                info.get("subtitles") or {}
            )

            video_info["automatic_subtitles"] = self._parse_subtitles(
                info.get("automatic_captions") or {}
            )

        logger.info(
            "Generic media information extracted",
            media_id=video_info["video_id"],
        )

        return video_info

    async def list_formats(
        self,
        url: str,
    ) -> List[VideoFormat]:
        """List available formats for a recognized media URL."""
        info = await self.get_info(
            url,
            include_formats=True,
        )

        formats: List[VideoFormat] = []

        for item in info.get("formats", []):
            formats.append(
                VideoFormat(
                    format_id=item.get("format_id", ""),
                    ext=item.get("ext", ""),
                    resolution=item.get("resolution"),
                    audio_bitrate=item.get("audio_bitrate"),
                    video_codec=item.get("video_codec"),
                    audio_codec=item.get("audio_codec"),
                    filesize=item.get("filesize"),
                    format_type=item.get("format_type", "unknown"),
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

    async def get_transcript(
        self,
        url: str,
        lang: str = "en",
        source: str = "any",
    ) -> Dict[str, Any]:
        """Retrieve subtitle text where the website provides subtitles."""
        if not self.validate_url(url):
            raise InvalidURLError(
                "URL is not recognized by the generic yt-dlp provider."
            )

        if source not in {"manual", "auto", "any"}:
            raise ValueError(
                "source must be 'manual', 'auto', or 'any'"
            )

        attempts = (
            ["manual", "auto"]
            if source == "any"
            else [source]
        )

        with tempfile.TemporaryDirectory(
            prefix="generic-transcript-"
        ) as temp_dir:
            for attempt_source in attempts:
                subtitle_flag = (
                    "--write-subs"
                    if attempt_source == "manual"
                    else "--write-auto-subs"
                )

                command = [
                    "yt-dlp",
                    "--ignore-config",
                    "--no-warnings",
                    "--no-playlist",
                    "--skip-download",
                    subtitle_flag,
                    "--sub-langs",
                    lang,
                    "--sub-format",
                    "vtt",
                    "--paths",
                    temp_dir,
                    "--output",
                    "transcript.%(ext)s",
                    url,
                ]

                try:
                    await self._execute_with_retry(
                        command,
                        timeout=45.0,
                    )
                except DownloadError as exc:
                    logger.info(
                        "Generic subtitle attempt unsuccessful",
                        source=attempt_source,
                        error=str(exc)[:200],
                    )
                    continue

                for vtt_file in sorted(
                    Path(temp_dir).glob("transcript*.vtt")
                ):
                    content = vtt_file.read_text(
                        encoding="utf-8",
                        errors="replace",
                    )

                    segments = parse_vtt(content)

                    if segments:
                        return {
                            "video_id": "",
                            "lang": lang,
                            "source": attempt_source,
                            "segments": segments,
                            "raw_vtt": content,
                        }

                    vtt_file.unlink(missing_ok=True)

        raise TranscriptNotFoundError(
            f"No transcript available for language '{lang}'."
        )

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
        """Download media and return the resulting file information."""
        if not self.validate_url(url):
            raise InvalidURLError(
                "URL is not recognized by the generic yt-dlp provider."
            )

        template = (
            output_template
            or "%(title).150B [%(id)s].%(ext)s"
        )

        # Keep downloads inside the configured output directory.
        # yt-dlp template substitutions such as %(title)s are permitted.
        template_path = Path(template)

        if (
            template_path.is_absolute()
            or ".." in template_path.parts
            or "/" in template
            or "\\" in template
        ):
            raise DownloadError(
                "Output template must be a filename template, not a path."
            )

        command = [
            "yt-dlp",
            "--ignore-config",
            "--no-warnings",
            "--no-playlist",
            "--print",
            "after_move:filepath",
            "--paths",
            str(self.output_dir),
            "--output",
            template,
        ]

        if format_id:
            command.extend(["--format", format_id])

        if extract_audio:
            selected_audio_format = (
                audio_format or "mp3"
            ).lower()

            allowed_audio_formats = {
                "mp3",
                "m4a",
                "wav",
                "opus",
            }

            if selected_audio_format not in allowed_audio_formats:
                raise DownloadError(
                    "Unsupported audio format requested."
                )

            command.extend([
                "--extract-audio",
                "--audio-format",
                selected_audio_format,
            ])

            if selected_audio_format in {"mp3", "m4a"}:
                command.extend([
                    "--audio-quality",
                    "0",
                ])

        if include_subtitles:
            command.append("--write-subs")

            if subtitle_lang:
                command.extend([
                    "--sub-langs",
                    subtitle_lang,
                ])

        command.append(url)

        logger.info(
            "Starting generic media download",
            host=urlparse(url).hostname,
            format_id=format_id or "best",
            extract_audio=extract_audio,
        )

        start_time = asyncio.get_running_loop().time()

        result = await self._execute_with_retry(command)

        output = result.stdout.decode(
            "utf-8",
            errors="replace",
        )

        raw_path = self._extract_file_path(output)

        if not raw_path:
            raise DownloadError(
                "yt-dlp did not return the downloaded file path."
            )

        file_path = Path(raw_path)

        if not file_path.is_absolute():
            file_path = self.output_dir / file_path

        file_path = file_path.resolve()

        # Prevent a returned file path from escaping the output directory.
        try:
            file_path.relative_to(self.output_dir)
        except ValueError as exc:
            raise DownloadError(
                "The downloaded file is outside the configured output directory."
            ) from exc

        if not file_path.is_file():
            raise DownloadError(
                "The downloaded file could not be found."
            )

        file_size = file_path.stat().st_size

        duration = (
            asyncio.get_running_loop().time() - start_time
        )

        logger.info(
            "Generic media download completed",
            file_size=file_size,
            duration=duration,
        )

        return DownloadResult(
            file_path=str(file_path),
            file_size=file_size,
            duration=duration,
            format_id=format_id or "best",
        )
