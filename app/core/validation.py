"""Input validation utilities for the API layer.

Provides validation for media URLs, yt-dlp format selectors, audio
parameters, language codes, and numeric request parameters.
"""

import ipaddress
import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import FrozenSet, Optional, Set
from urllib.parse import urlparse

import structlog

logger = structlog.get_logger(__name__)


# ------------------------------------------------------------
# YT-DLP SITE EXTRACTOR DISCOVERY
# ------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_supported_extractors():
    """Load known yt-dlp site extractors, excluding the catch-all generic one."""
    try:
        from yt_dlp.extractor import gen_extractor_classes
    except ImportError:
        logger.error("Could not import yt-dlp extractors")
        return ()

    supported = []

    for extractor in gen_extractor_classes():
        extractor_name = str(
            getattr(extractor, "IE_NAME", "") or ""
        ).lower()

        class_name = str(
            getattr(extractor, "__name__", "") or ""
        ).lower()

        # Do not treat the catch-all generic extractor as support for
        # an arbitrary URL supplied by an API caller.
        if extractor_name in {
            "generic",
            "generic:search",
        }:
            continue

        if class_name in {
            "genericie",
            "genericauthie",
        }:
            continue

        supported.append(extractor)

    return tuple(supported)


def _recognized_by_ytdlp(url: str) -> bool:
    """Return whether a specific installed yt-dlp extractor recognizes a URL."""
    for extractor in _get_supported_extractors():
        try:
            if extractor.suitable(url):
                return True
        except Exception:
            # One extractor failing to inspect a URL must not prevent
            # other extractors from checking it.
            continue

    return False


# ------------------------------------------------------------
# AUDIO OPTIONS
# ------------------------------------------------------------

class AudioFormat(str, Enum):
    """Supported audio output formats."""

    MP3 = "mp3"
    M4A = "m4a"
    WAV = "wav"
    OPUS = "opus"


class AudioQuality(str, Enum):
    """Supported audio quality levels."""

    LOW = "128"
    MEDIUM = "192"
    HIGH = "320"


# ------------------------------------------------------------
# VALIDATION RESULT
# ------------------------------------------------------------

@dataclass(frozen=True)
class ValidationResult:
    """Result of a validation operation."""

    is_valid: bool
    error_message: Optional[str] = None
    sanitized_value: Optional[str] = None


# ------------------------------------------------------------
# URL VALIDATION
# ------------------------------------------------------------

class URLValidator:
    """Validate public HTTP(S) URLs against configured or yt-dlp providers.

    When an explicit allowed_domains allowlist is passed, only those
    domains and their subdomains are accepted.

    When no allowlist is supplied, a URL must be recognized by a specific
    installed yt-dlp site extractor. The catch-all generic extractor is
    deliberately excluded.
    """

    DANGEROUS_SCHEMES: FrozenSet[str] = frozenset(
        {
            "javascript",
            "data",
            "file",
            "vbscript",
            "about",
        }
    )

    BLOCKED_HOST_SUFFIXES: FrozenSet[str] = frozenset(
        {
            ".localhost",
            ".local",
            ".internal",
            ".test",
        }
    )

    def __init__(
        self,
        allowed_domains: Optional[Set[str]] = None,
    ):
        """Initialize URL validation.

        Args:
            allowed_domains:
                Optional strict hostname allowlist. If omitted, the
                installed yt-dlp site extractors determine supported URLs.
        """
        if allowed_domains is None:
            self.allowed_domains = None
        else:
            self.allowed_domains = {
                str(domain).strip().lower().rstrip(".")
                for domain in allowed_domains
                if str(domain).strip()
            }

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Add HTTPS to a URL which has no explicit scheme."""
        value = url.strip()

        if "://" not in value:
            value = "https://" + value.lstrip("/")

        return value

    def _validate_public_hostname(self, hostname: str) -> Optional[str]:
        """Return an error message if a hostname is unsafe or disallowed."""
        host = hostname.lower().rstrip(".")

        if not host:
            return "URL must include a valid hostname"

        if host == "localhost" or any(
            host.endswith(suffix)
            for suffix in self.BLOCKED_HOST_SUFFIXES
        ):
            return "Local and internal hostnames are not allowed"

        # Reject IP literals. This blocks direct attempts to target
        # loopback, private, link-local, or other IP-address endpoints.
        try:
            ipaddress.ip_address(host)
            return "Direct IP-address URLs are not supported"
        except ValueError:
            pass

        if self.allowed_domains is not None:
            allowed = any(
                host == domain or host.endswith("." + domain)
                for domain in self.allowed_domains
            )

            if not allowed:
                return (
                    f"Domain '{host}' is not in the allowed list"
                )

        return None

    def validate(self, url: str) -> ValidationResult:
        """Validate a media URL.

        The default policy permits URLs recognized by installed yt-dlp
        site extractors, subject to the URL safety checks above.
        """
        if not url or not isinstance(url, str):
            return ValidationResult(
                is_valid=False,
                error_message="URL is required and must be a string",
            )

        original = url.strip()

        if not original:
            return ValidationResult(
                is_valid=False,
                error_message="URL cannot be empty",
            )

        # Reject dangerous or unsupported schemes before normalizing URLs.
        try:
            original_parsed = urlparse(original)
        except ValueError as exc:
            logger.warning(
                "URL parsing failed",
                error=str(exc),
            )
            return ValidationResult(
                is_valid=False,
                error_message="Invalid URL format",
            )

        original_scheme = (
            original_parsed.scheme.lower()
            if original_parsed.scheme
            else ""
        )

        if original_scheme in self.DANGEROUS_SCHEMES:
            return ValidationResult(
                is_valid=False,
                error_message=(
                    f"URL scheme '{original_scheme}' is not allowed"
                ),
            )

        # Support URLs pasted without an explicit scheme.
        normalized_url = self._normalize_url(original)

        try:
            parsed = urlparse(normalized_url)

            scheme = parsed.scheme.lower()
            hostname = parsed.hostname

            # Force validation of a malformed port number.
            _ = parsed.port

        except ValueError:
            return ValidationResult(
                is_valid=False,
                error_message="Invalid URL format",
            )

        if scheme not in {"http", "https"}:
            return ValidationResult(
                is_valid=False,
                error_message="URL must use http or https",
            )

        if parsed.username or parsed.password:
            return ValidationResult(
                is_valid=False,
                error_message=(
                    "URLs containing embedded credentials are not allowed"
                ),
            )

        if not hostname:
            return ValidationResult(
                is_valid=False,
                error_message="URL must include a valid hostname",
            )

        host_error = self._validate_public_hostname(hostname)

        if host_error:
            logger.debug(
                "URL rejected by hostname validation",
                hostname=hostname,
                reason=host_error,
            )
            return ValidationResult(
                is_valid=False,
                error_message=host_error,
            )

        # With a strict domain allowlist, the matching hostname is enough.
        # Otherwise require a specific site extractor to recognize the URL.
        if self.allowed_domains is None:
            if not _recognized_by_ytdlp(normalized_url):
                logger.debug(
                    "URL not recognized by an installed yt-dlp extractor",
                    hostname=hostname,
                )
                return ValidationResult(
                    is_valid=False,
                    error_message=(
                        "This URL is not recognized by an installed "
                        "yt-dlp site extractor"
                    ),
                )

        logger.debug(
            "URL validated successfully",
            hostname=hostname,
        )

        return ValidationResult(
            is_valid=True,
            sanitized_value=normalized_url,
        )

    def is_valid(self, url: str) -> bool:
        """Quick check for a valid URL."""
        return self.validate(url).is_valid


# ------------------------------------------------------------
# FORMAT ID VALIDATION
# ------------------------------------------------------------

class FormatValidator:
    """Validate yt-dlp format IDs and approved format selectors."""

    # Standard format IDs may contain these characters.
    # Bracket-based selectors are handled through SPECIAL_SELECTORS.
    FORMAT_ID_PATTERN = re.compile(
        r"^[a-zA-Z0-9_+-]+$"
    )

    MAX_FORMAT_ID_LENGTH = 50

    SPECIAL_SELECTORS: FrozenSet[str] = frozenset(
        {
            "best",
            "worst",
            "bestvideo",
            "worstvideo",
            "bestaudio",
            "worstaudio",
            "bestvideo+bestaudio",
            "best[height<=480]",
            "best[height<=720]",
            "best[height<=1080]",
        }
    )

    def validate_format_id(
        self,
        format_id: str,
    ) -> ValidationResult:
        """Validate a format ID or approved selector."""
        if not format_id or not isinstance(format_id, str):
            return ValidationResult(
                is_valid=False,
                error_message="Format ID is required",
            )

        value = format_id.strip()

        if not value:
            return ValidationResult(
                is_valid=False,
                error_message="Format ID cannot be empty",
            )

        if len(value) > self.MAX_FORMAT_ID_LENGTH:
            return ValidationResult(
                is_valid=False,
                error_message=(
                    f"Format ID exceeds maximum length of "
                    f"{self.MAX_FORMAT_ID_LENGTH}"
                ),
            )

        # Permit only explicitly approved bracket-based selectors.
        if value.lower() in self.SPECIAL_SELECTORS:
            return ValidationResult(
                is_valid=True,
                sanitized_value=value,
            )

        if not self.FORMAT_ID_PATTERN.fullmatch(value):
            return ValidationResult(
                is_valid=False,
                error_message="Format ID contains invalid characters",
            )

        return ValidationResult(
            is_valid=True,
            sanitized_value=value,
        )

    def is_valid_format_id(self, format_id: str) -> bool:
        """Quick check for a valid format ID."""
        return self.validate_format_id(format_id).is_valid


# ------------------------------------------------------------
# OTHER REQUEST PARAMETER VALIDATION
# ------------------------------------------------------------

class ParameterValidator:
    """Validate audio, language, and numeric request parameters."""

    def validate_audio_format(
        self,
        audio_format: str,
    ) -> ValidationResult:
        """Validate the requested audio format."""
        if not audio_format:
            return ValidationResult(
                is_valid=False,
                error_message="Audio format is required",
            )

        value = audio_format.lower().strip()

        try:
            AudioFormat(value)

            return ValidationResult(
                is_valid=True,
                sanitized_value=value,
            )

        except ValueError:
            valid_formats = [
                item.value for item in AudioFormat
            ]

            return ValidationResult(
                is_valid=False,
                error_message=(
                    "Invalid audio format. Valid options: "
                    f"{', '.join(valid_formats)}"
                ),
            )

    def validate_audio_quality(
        self,
        quality: str,
    ) -> ValidationResult:
        """Validate the requested audio bitrate."""
        if not quality:
            return ValidationResult(
                is_valid=False,
                error_message="Audio quality is required",
            )

        value = str(quality).strip()

        try:
            AudioQuality(value)

            return ValidationResult(
                is_valid=True,
                sanitized_value=value,
            )

        except ValueError:
            valid_qualities = [
                item.value for item in AudioQuality
            ]

            return ValidationResult(
                is_valid=False,
                error_message=(
                    "Invalid audio quality. Valid options: "
                    f"{', '.join(valid_qualities)}kbps"
                ),
            )

    def validate_language_code(
        self,
        lang_code: str,
    ) -> ValidationResult:
        """Validate an ISO-style language code."""
        if not lang_code or not isinstance(lang_code, str):
            return ValidationResult(
                is_valid=False,
                error_message="Language code is required",
            )

        value = lang_code.strip()

        pattern = re.compile(
            r"^[a-zA-Z]{2,3}(?:-[a-zA-Z]{2,4})?$"
        )

        if not pattern.fullmatch(value):
            return ValidationResult(
                is_valid=False,
                error_message=(
                    "Invalid language code format. "
                    "Use an ISO-style code such as 'en' or 'en-US'"
                ),
            )

        return ValidationResult(
            is_valid=True,
            sanitized_value=value,
        )

    def validate_positive_integer(
        self,
        value: int,
        name: str,
        max_value: Optional[int] = None,
    ) -> ValidationResult:
        """Validate a positive integer with an optional upper bound."""
        if not isinstance(value, int) or isinstance(value, bool):
            return ValidationResult(
                is_valid=False,
                error_message=f"{name} must be an integer",
            )

        if value <= 0:
            return ValidationResult(
                is_valid=False,
                error_message=f"{name} must be a positive integer",
            )

        if max_value is not None and value > max_value:
            return ValidationResult(
                is_valid=False,
                error_message=(
                    f"{name} exceeds maximum allowed value of "
                    f"{max_value}"
                ),
            )

        return ValidationResult(
            is_valid=True,
            sanitized_value=str(value),
        )


# ------------------------------------------------------------
# SHARED VALIDATOR INSTANCES
# ------------------------------------------------------------

url_validator = URLValidator()
format_validator = FormatValidator()
parameter_validator = ParameterValidator()


# ------------------------------------------------------------
# CONVENIENCE FUNCTIONS
# ------------------------------------------------------------

def validate_youtube_url(url: str) -> bool:
    """Return whether a URL is a recognized YouTube URL."""
    if not url or not isinstance(url, str):
        return False

    normalized = URLValidator._normalize_url(url)

    try:
        parsed = urlparse(normalized)
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False

    is_youtube = (
        hostname == "youtube.com"
        or hostname.endswith(".youtube.com")
        or hostname == "youtu.be"
        or hostname == "youtube-nocookie.com"
        or hostname.endswith(".youtube-nocookie.com")
    )

    if not is_youtube:
        return False

    return url_validator.is_valid(normalized)


def validate_format_id(format_id: str) -> bool:
    """Return whether a format ID is valid."""
    return format_validator.is_valid_format_id(format_id)
