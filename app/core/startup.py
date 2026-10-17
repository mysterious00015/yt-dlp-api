
"""Startup validation for the application.

This module provides startup checks for external dependencies and
configuration validation to ensure the system is properly initialized
before accepting requests.

Implements requirements:
- Req 10: JavaScript Challenge Resolution (Node.js >= 22)
- Req 21: JavaScript Runtime Configuration for yt-dlp
- Req 22: Output Directory Management
- Req 23: Cookie Directory Management
- Req 47: Graceful Startup Mode (degraded mode support)
"""

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from app.core.checks import check_ffmpeg, check_nodejs, check_ytdlp
from app.core.config import Config

logger = structlog.get_logger(__name__)


def _is_test_mode() -> bool:
    """Check if test mode is enabled via environment variable."""
    return os.environ.get(
        "APP_TESTING_TEST_MODE", ""
    ).lower() in ("true", "1", "yes")


@dataclass
class ComponentCheckResult:
    """Result of a startup component check.

    Attributes:
        name: Component name (e.g., "ytdlp", "ffmpeg", "nodejs")
        passed: Whether the check passed
        critical: If True, failure blocks startup (unless degraded mode)
        version: Version string if available
        message: Human-readable message about the result
        details: Additional details about the check
    """

    name: str
    passed: bool
    critical: bool
    version: Optional[str] = None
    message: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StartupResult:
    """Result of full startup validation.

    Attributes:
        success: Whether startup can proceed
        degraded_mode: Whether the application is running in degraded mode
        checks: List of individual component check results
        disabled_providers: List of provider names that were disabled
        errors: List of error messages for critical failures
        warnings: List of warning messages for non-critical issues
    """

    success: bool
    degraded_mode: bool
    checks: List[ComponentCheckResult] = field(default_factory=list)
    disabled_providers: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class StartupValidator:
    """Validates system components at startup.

    This class performs comprehensive startup validation:
    - External binaries: yt-dlp, ffmpeg, Node.js >= 22
    - Storage: output directory creation and write permissions
    - Cookies: file existence and format validation
    - yt-dlp JavaScript runtime configuration

    Deno is preferred when installed. Node.js is the fallback runtime.

    Supports degraded mode where the application can start with
    warnings when optional components are unavailable.
    """

    # Minimum Node.js version required by this deployment.
    MIN_NODEJS_VERSION = 22

    # Components that must be available even in degraded mode.
    # In test mode, yt-dlp is mocked.
    ALWAYS_CRITICAL_COMPONENTS = {"ytdlp", "storage"}

    @property
    def critical_components(self) -> set:
        """Get the set of critical components based on mode."""
        if _is_test_mode():
            return {"storage"}

        return self.ALWAYS_CRITICAL_COMPONENTS

    def __init__(self, config: Config):
        """Initialize the startup validator.

        Args:
            config: Application configuration.
        """
        self.config = config
        self.allow_degraded = config.security.allow_degraded_start
        self.results: List[ComponentCheckResult] = []
        self.disabled_providers: List[str] = []
        self.errors: List[str] = []
        self.warnings: List[str] = []

    async def validate_all(self) -> StartupResult:
        """Run all startup validations.

        Returns:
            StartupResult with overall status and component details.
        """
        logger.info(
            "startup_validation_started",
            allow_degraded_start=self.allow_degraded,
        )

        # Reset state for fresh validation.
        self.results = []
        self.disabled_providers = []
        self.errors = []
        self.warnings = []

        # Run all component checks.
        await self._run_checks()

        critical_failures = [
            result
            for result in self.results
            if not result.passed and result.critical
        ]

        non_critical_failures = [
            result
            for result in self.results
            if not result.passed and not result.critical
        ]

        # Configure the available JavaScript runtime.
        # Prefer Deno; fall back to Node.js.
        self.configure_ytdlp_runtime()

        # Determine whether the application can start.
        if critical_failures:
            if self.allow_degraded:
                truly_critical = [
                    result
                    for result in critical_failures
                    if result.name in self.critical_components
                ]

                if truly_critical:
                    for result in truly_critical:
                        self.errors.append(
                            f"{result.name}: {result.message}"
                        )

                    success = False
                    degraded_mode = False
                else:
                    for result in critical_failures:
                        self.warnings.append(
                            f"{result.name}: {result.message}"
                        )

                    success = True
                    degraded_mode = True
            else:
                for result in critical_failures:
                    self.errors.append(
                        f"{result.name}: {result.message}"
                    )

                success = False
                degraded_mode = False
        else:
            success = True
            degraded_mode = (
                bool(non_critical_failures)
                and self.allow_degraded
            )

        # Add non-critical failures as warnings.
        for result in non_critical_failures:
            self.warnings.append(
                f"{result.name}: {result.message}"
            )

        result = StartupResult(
            success=success,
            degraded_mode=degraded_mode,
            checks=self.results,
            disabled_providers=self.disabled_providers,
            errors=self.errors,
            warnings=self.warnings,
        )

        log_method = logger.info if success else logger.error

        log_method(
            "startup_validation_completed",
            success=success,
            degraded_mode=degraded_mode,
            disabled_providers=self.disabled_providers,
            error_count=len(self.errors),
            warning_count=len(self.warnings),
        )

        return result

    async def _run_checks(self) -> None:
        """Run all component checks."""
        ytdlp_result = await self.check_ytdlp()
        self.results.append(ytdlp_result)

        ffmpeg_result = await self.check_ffmpeg()
        self.results.append(ffmpeg_result)

        nodejs_result = await self.check_nodejs()
        self.results.append(nodejs_result)

        storage_result = await self.check_storage()
        self.results.append(storage_result)

        cookie_result = await self.check_cookies()
        self.results.append(cookie_result)

    async def check_ytdlp(self) -> ComponentCheckResult:
        """Check yt-dlp availability and version."""
        result = await check_ytdlp()

        if result.available:
            logger.info(
                "ytdlp_check_passed",
                version=result.version,
            )

            return ComponentCheckResult(
                name="ytdlp",
                passed=True,
                critical=True,
                version=result.version,
                message="yt-dlp is available",
            )

        logger.error(
            "ytdlp_check_failed",
            error=result.error,
        )

        return ComponentCheckResult(
            name="ytdlp",
            passed=False,
            critical=True,
            message=result.error or "yt-dlp is not available",
        )

    async def check_ffmpeg(self) -> ComponentCheckResult:
        """Check FFmpeg availability and version."""
        result = await check_ffmpeg()

        if result.available:
            logger.info(
                "ffmpeg_check_passed",
                version=result.version,
            )

            return ComponentCheckResult(
                name="ffmpeg",
                passed=True,
                critical=True,
                version=result.version,
                message="FFmpeg is available",
            )

        logger.error(
            "ffmpeg_check_failed",
            error=result.error,
        )

        return ComponentCheckResult(
            name="ffmpeg",
            passed=False,
            critical=True,
            message=result.error or "FFmpeg is not available",
        )

    async def check_nodejs(self) -> ComponentCheckResult:
        """Check Node.js availability and version >= 22."""
        result = await check_nodejs(
            min_version=self.MIN_NODEJS_VERSION
        )

        if result.available:
            logger.info(
                "nodejs_check_passed",
                version=result.version,
            )

            return ComponentCheckResult(
                name="nodejs",
                passed=True,
                critical=True,
                version=result.version,
                message="Node.js is available",
            )

        logger.error(
            "nodejs_check_failed",
            error=result.error,
        )

        return ComponentCheckResult(
            name="nodejs",
            passed=False,
            critical=True,
            message=(
                result.error
                or f"Node.js >= {self.MIN_NODEJS_VERSION} is required"
            ),
        )

    async def check_storage(self) -> ComponentCheckResult:
        """Check storage directory availability and permissions."""
        output_dir = Path(self.config.storage.output_dir)

        try:
            if not output_dir.exists():
                output_dir.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                logger.info(
                    "output_directory_created",
                    path=str(output_dir),
                )

            test_file = output_dir / f".write_test_{os.getpid()}"

            try:
                test_file.touch()
                test_file.unlink(missing_ok=True)
            except PermissionError:
                logger.error(
                    "storage_permission_error",
                    path=str(output_dir),
                )

                return ComponentCheckResult(
                    name="storage",
                    passed=False,
                    critical=True,
                    message=(
                        f"Cannot write to output directory: {output_dir}"
                    ),
                )

            logger.info(
                "storage_check_passed",
                path=str(output_dir),
            )

            return ComponentCheckResult(
                name="storage",
                passed=True,
                critical=True,
                message="Storage is available and writable",
                details={"output_dir": str(output_dir)},
            )

        except OSError as error:
            logger.error(
                "storage_check_failed",
                path=str(output_dir),
                error=str(error),
            )

            return ComponentCheckResult(
                name="storage",
                passed=False,
                critical=True,
                message=f"Storage check failed: {error}",
            )

    def _cookie_failure(
        self,
        message: str,
        provider: str = "youtube",
    ) -> ComponentCheckResult:
        """Create a cookie check failure result."""
        if self.allow_degraded:
            if not _is_test_mode():
                self.disabled_providers.append(provider)

            return ComponentCheckResult(
                name="cookies",
                passed=False,
                critical=False,
                message=message,
            )

        return ComponentCheckResult(
            name="cookies",
            passed=False,
            critical=True,
            message=message,
        )

    def _check_cookie_access(
        self,
        path: Path,
        cookie_path: str,
    ) -> Optional[ComponentCheckResult]:
        """Verify that the cookie file exists and is accessible."""
        try:
            if path.is_file():
                return None
        except OSError as error:
            logger.error(
                "youtube_cookie_access_error",
                path=cookie_path,
                error=str(error),
            )

            return self._cookie_failure(
                f"Cannot access cookie file: {cookie_path} ({error})"
            )

        logger.warning(
            "youtube_cookie_not_found",
            path=cookie_path,
        )

        return self._cookie_failure(
            f"Cookie file not found: {cookie_path}"
        )

    async def check_cookies(self) -> ComponentCheckResult:
        """Check cookie file availability and Netscape format."""
        if not self.config.providers.youtube.enabled:
            logger.info("youtube_provider_disabled")

            return ComponentCheckResult(
                name="cookies",
                passed=True,
                critical=False,
                message=(
                    "YouTube provider is disabled, no cookie check needed"
                ),
            )

        cookie_path = self.config.providers.youtube.cookie_path

        if not cookie_path:
            logger.warning("youtube_cookie_not_configured")

            return self._cookie_failure(
                "No cookie path configured for YouTube"
            )

        path = Path(cookie_path)

        access_failure = self._check_cookie_access(
            path,
            cookie_path,
        )

        if access_failure is not None:
            return access_failure

        try:
            content = path.read_text()

            if not content.strip():
                logger.warning(
                    "youtube_cookie_empty",
                    path=cookie_path,
                )

                return self._cookie_failure(
                    "Cookie file is empty"
                )

            lines = [
                line
                for line in content.strip().splitlines()
                if line.strip() and not line.startswith("#")
            ]

            valid_entries = sum(
                1
                for line in lines
                if len(line.split("\t")) == 7
            )

            if valid_entries == 0:
                logger.warning(
                    "youtube_cookie_invalid_format",
                    path=cookie_path,
                )

                return self._cookie_failure(
                    "Cookie file has invalid format "
                    "(no valid Netscape entries)"
                )

            logger.info(
                "cookies_check_passed",
                path=cookie_path,
                valid_entries=valid_entries,
            )

            return ComponentCheckResult(
                name="cookies",
                passed=True,
                critical=False,
                message="Cookie file is valid",
                details={
                    "path": cookie_path,
                    "valid_entries": valid_entries,
                },
            )

        except PermissionError:
            logger.error(
                "youtube_cookie_permission_error",
                path=cookie_path,
            )

            return self._cookie_failure(
                f"Cannot read cookie file: {cookie_path}"
            )

        except Exception as error:
            logger.error(
                "youtube_cookie_read_error",
                path=cookie_path,
                error=str(error),
            )

            return self._cookie_failure(
                f"Error reading cookie file: {error}"
            )

    def configure_ytdlp_runtime(self) -> None:
        """Configure yt-dlp to prefer Deno, falling back to Node.js.

        Uses an absolute path so yt-dlp can locate the selected runtime.
        This method only configures the runtime; it cannot guarantee that
        YouTube will accept the request or that a challenge will succeed.
        """
        deno_path = shutil.which("deno")
        node_path = shutil.which("node")

        if deno_path:
            runtime_setting = f"deno:{deno_path}"
            runtime_name = "deno"
            runtime_path = deno_path
        elif node_path:
            runtime_setting = f"node:{node_path}"
            runtime_name = "node"
            runtime_path = node_path
        else:
            logger.warning(
                "ytdlp_js_runtime_not_found",
                message="Neither Deno nor Node.js was found",
            )
            return

        xdg_config = os.environ.get("XDG_CONFIG_HOME", "")

        if xdg_config:
            config_dir = Path(xdg_config) / "yt-dlp"
        else:
            config_dir = Path.home() / ".config" / "yt-dlp"

        config_file = config_dir / "config"

        try:
            config_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            existing_lines = (
                config_file.read_text().splitlines()
                if config_file.exists()
                else []
            )

            # Preserve other yt-dlp options, replacing previous
            # JavaScript-runtime selection options.
            preserved_lines = [
                line
                for line in existing_lines
                if not line.strip().startswith("--js-runtimes")
                and not line.strip().startswith("--no-js-runtimes")
            ]

            preserved_lines.append(
                "# JavaScript runtime configured by yt-dlp-api"
            )
            preserved_lines.append(
                f"--js-runtimes {runtime_setting}"
            )

            config_file.write_text(
                "\n".join(preserved_lines).rstrip() + "\n"
            )

            logger.info(
                "ytdlp_runtime_configured",
                runtime=runtime_name,
                runtime_path=runtime_path,
                config_file=str(config_file),
            )

        except Exception as error:
            logger.warning(
                "ytdlp_config_write_failed",
                config_file=str(config_file),
                runtime=runtime_name,
                runtime_path=runtime_path,
                error=str(error),
            )
