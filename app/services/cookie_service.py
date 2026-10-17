
"""Cookie management service."""

import asyncio
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import structlog
from cachetools import TTLCache

from app.providers.exceptions import CookieError
from app.utils.cookies import exec_cookie_copy

logger = structlog.get_logger(__name__)


class CookieService:
    """Manages cookie files for video providers."""

    # Cookie file age warning threshold (7 days)
    WARNING_AGE_DAYS = 7

    # Cache TTL for validation results (1 hour)
    VALIDATION_CACHE_TTL = 3600

    # File modification check interval (60 seconds)
    FILE_CHECK_INTERVAL = 60

    def __init__(self, config: dict):
        """Initialize cookie service.

        Args:
            config: Configuration dictionary with provider cookie paths.
        """
        self.config = config
        self.provider_cookies: Dict[str, str] = {}

        # Capture test mode at construction time.
        self._test_mode = os.environ.get(
            "APP_TESTING_TEST_MODE", ""
        ).lower() in ("true", "1", "yes")

        if self._test_mode:
            logger.info("Cookie service initialized in test mode")

        self.validation_cache: TTLCache = TTLCache(
            maxsize=10,
            ttl=self.VALIDATION_CACHE_TTL,
        )

        self.file_mtimes: Dict[str, float] = {}
        self.last_file_check: Dict[str, float] = {}

        self._load_provider_cookies()

    def _load_provider_cookies(self) -> None:
        """Load cookie paths for all configured providers."""
        providers_config = self.config.get("providers", {})

        for provider_name, provider_config in providers_config.items():
            if not provider_config.get("enabled", True):
                logger.info(
                    "Provider disabled, skipping cookie",
                    provider=provider_name,
                )
                continue

            cookie_path = provider_config.get("cookie_path")

            if cookie_path:
                self.provider_cookies[provider_name] = cookie_path
                logger.info(
                    "Cookie path registered",
                    provider=provider_name,
                    cookie_path=cookie_path,
                )

    def get_cookie_path(self, provider: str) -> Optional[str]:
        """Get cookie file path for a provider.

        Args:
            provider: Provider name, such as "youtube".

        Returns:
            Cookie file path, or None if not configured.
        """
        return self.provider_cookies.get(provider)

    def load_cookie_file(self, provider: str) -> str:
        """Load cookie file content for a provider.

        Args:
            provider: Provider name.

        Returns:
            Cookie file content.

        Raises:
            CookieError: If the cookie file is missing or unreadable.
        """
        cookie_path = self.get_cookie_path(provider)

        if not cookie_path:
            raise CookieError(
                f"No cookie path configured for provider: {provider}"
            )

        path = Path(cookie_path)

        if not path.exists():
            raise CookieError(
                f"Cookie file not found for {provider}: {cookie_path}. "
                "Please export cookies from your browser and place them "
                "at this location."
            )

        try:
            content = path.read_text()
            logger.debug(
                "Cookie file loaded",
                provider=provider,
                size=len(content),
            )
            return content
        except Exception as error:
            raise CookieError(
                f"Failed to read cookie file for {provider}: {error}"
            ) from error

    def validate_netscape_format(self, content: str, provider: str) -> bool:
        """Validate that a cookie file uses Netscape format.

        Args:
            content: Cookie file content.
            provider: Provider name for logging.

        Returns:
            True if the format is valid.

        Raises:
            CookieError: If the format is invalid.
        """
        if not content.strip():
            raise CookieError(f"Cookie file for {provider} is empty")

        lines = content.strip().split("\n")
        first_line = lines[0].strip()

        if first_line.startswith(
            "# Netscape HTTP Cookie File"
        ) or first_line.startswith("# HTTP Cookie File"):
            logger.debug(
                "Netscape format header found",
                provider=provider,
            )
        else:
            logger.debug(
                "No Netscape header, checking cookie entries",
                provider=provider,
            )

        valid_entries = 0

        for line in lines:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            parts = line.split("\t")

            if len(parts) != 7:
                raise CookieError(
                    f"Invalid cookie entry in {provider} cookie file. "
                    f"Expected 7 tab-separated fields, got {len(parts)}. "
                    "Ensure the file is in Netscape format."
                )

            valid_entries += 1

        if valid_entries == 0:
            raise CookieError(
                f"No valid cookie entries found in {provider} cookie file. "
                "The file may be corrupted or in the wrong format."
            )

        logger.info(
            "Cookie file validated",
            provider=provider,
            valid_entries=valid_entries,
        )

        return True

    def check_cookie_age(self, provider: str) -> Optional[str]:
        """Check cookie age and return a warning if it is too old.

        Args:
            provider: Provider name.

        Returns:
            Warning message if the cookie is old, otherwise None.
        """
        cookie_path = self.get_cookie_path(provider)

        if not cookie_path:
            return None

        path = Path(cookie_path)

        if not path.exists():
            return None

        try:
            mtime = path.stat().st_mtime
            age = datetime.now() - datetime.fromtimestamp(mtime)
            age_days = age.days

            if age_days >= self.WARNING_AGE_DAYS:
                warning = (
                    f"Cookie file for {provider} is {age_days} days old. "
                    "Consider refreshing cookies if authentication issues occur."
                )

                logger.warning(
                    "Cookie file is old",
                    provider=provider,
                    age_days=age_days,
                )
                return warning

            logger.debug(
                "Cookie age check passed",
                provider=provider,
                age_days=age_days,
            )
            return None

        except Exception as error:
            logger.error(
                "Failed to check cookie age",
                provider=provider,
                error=str(error),
            )
            return None

    def get_cookie_age_hours(self, provider: str) -> Optional[float]:
        """Get the cookie file age in hours.

        Args:
            provider: Provider name.

        Returns:
            Age in hours, or None if it cannot be determined.
        """
        cookie_path = self.get_cookie_path(provider)

        if not cookie_path:
            return None

        path = Path(cookie_path)

        if not path.exists():
            return None

        try:
            age_seconds = time.time() - path.stat().st_mtime
            return age_seconds / 3600
        except Exception as error:
            logger.error(
                "Failed to get cookie age",
                provider=provider,
                error=str(error),
            )
            return None

    def validate_cookie_file(self, provider: str) -> None:
        """Validate cookie format and age.

        Args:
            provider: Provider name.

        Raises:
            CookieError: If validation fails.
        """
        content = self.load_cookie_file(provider)
        self.validate_netscape_format(content, provider)

        warning = self.check_cookie_age(provider)

        if warning:
            logger.warning(
                "Cookie age warning",
                provider=provider,
                warning=warning,
            )

        logger.info(
            "Cookie file validation complete",
            provider=provider,
        )

    def list_providers_with_cookies(self) -> Dict[str, dict]:
        """List configured providers and their cookie status.

        Returns:
            Dictionary mapping provider names to cookie status.
        """
        result = {}

        for provider, cookie_path in self.provider_cookies.items():
            path = Path(cookie_path)
            exists = path.exists()

            info = {
                "cookie_path": cookie_path,
                "exists": exists,
                "age_hours": (
                    self.get_cookie_age_hours(provider)
                    if exists
                    else None
                ),
            }

            if exists:
                warning = self.check_cookie_age(provider)
                if warning:
                    info["warning"] = warning

            result[provider] = info

        return result

    def _check_file_modification(self, provider: str) -> bool:
        """Check whether a cookie file has changed since the last check.

        Args:
            provider: Provider name.

        Returns:
            True if the file was modified, otherwise False.
        """
        cookie_path = self.get_cookie_path(provider)

        if not cookie_path:
            return False

        path = Path(cookie_path)

        if not path.exists():
            return False

        try:
            current_mtime = path.stat().st_mtime
            last_mtime = self.file_mtimes.get(provider)

            if last_mtime is None or current_mtime != last_mtime:
                self.file_mtimes[provider] = current_mtime

                logger.info(
                    "Cookie file modification detected",
                    provider=provider,
                    mtime=current_mtime,
                )
                return True

            return False

        except Exception as error:
            logger.error(
                "Failed to check file modification",
                provider=provider,
                error=str(error),
            )
            return False

    def _should_check_file(self, provider: str) -> bool:
        """Check whether enough time has passed to check file modification.

        Args:
            provider: Provider name.

        Returns:
            True if the file should be checked.
        """
        now = time.time()
        last_check = self.last_file_check.get(provider, 0)

        if now - last_check >= self.FILE_CHECK_INTERVAL:
            self.last_file_check[provider] = now
            return True

        return False

    def _invalidate_cache(self, provider: str) -> None:
        """Invalidate a provider's validation cache."""
        if provider in self.validation_cache:
            del self.validation_cache[provider]
            logger.info(
                "Validation cache invalidated",
                provider=provider,
            )

    async def validate_cookie(self, provider: str) -> bool:
        """Validate a cookie with caching and a YouTube authentication test.

        Raises:
            CookieError: If validation fails.
        """
        if (
            self._should_check_file(provider)
            and self._check_file_modification(provider)
        ):
            self._invalidate_cache(provider)

        if provider in self.validation_cache:
            cached_result: bool = self.validation_cache[provider]

            logger.debug(
                "Cookie validation from cache",
                provider=provider,
                is_valid=cached_result,
            )
            return cached_result

        logger.info(
            "Performing full cookie validation",
            provider=provider,
        )

        try:
            self.validate_cookie_file(provider)

            if provider == "youtube":
                validation_result = await self._test_youtube_authentication()
            else:
                validation_result = True

            self.validation_cache[provider] = validation_result

            logger.info(
                "Cookie validation complete",
                provider=provider,
                is_valid=validation_result,
            )

            return validation_result

        except CookieError:
            # Do not cache failures so the caller can retry after fixing them.
            raise

    async def _test_youtube_authentication(self) -> bool:
        """Test YouTube access using cookies and a supported JS runtime.

        Deno is preferred for yt-dlp's JavaScript challenge solver, with
        Node.js as the fallback. The configured cookie file is passed through
        exec_cookie_copy(), which provides a writable copy when needed.

        Raises:
            CookieError: If the cookie cannot be validated or the test fails.
        """
        if self._test_mode:
            logger.debug(
                "Skipping YouTube authentication test (test mode)"
            )
            return True

        cookie_path = self.get_cookie_path("youtube")

        if not cookie_path:
            raise CookieError(
                "No cookie path configured for YouTube"
            )

        if not Path(cookie_path).is_file():
            raise CookieError(
                f"YouTube cookie file not found: {cookie_path}"
            )

        runtime_path = shutil.which("deno")
        runtime_name = "deno"

        if not runtime_path:
            runtime_path = shutil.which("node")
            runtime_name = "node"

        if not runtime_path:
            raise CookieError(
                "Neither Deno nor Node.js is available for YouTube "
                "JavaScript challenge solving."
            )

        test_url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
        timeout_seconds = 45.0

        process = None
        communicate_task = None

        auth_cmd = [
            "yt-dlp",
            "--verbose",
            "--socket-timeout",
            "10",
            "--retries",
            "0",
            "--extractor-retries",
            "0",
            "--js-runtimes",
            f"{runtime_name}:{runtime_path}",
            "--cookies",
            cookie_path,
            "--simulate",
            "--no-playlist",
            "--skip-download",
            test_url,
        ]

        logger.info(
            "youtube_authentication_test_started",
            js_runtime=runtime_name,
            timeout_seconds=timeout_seconds,
        )

        try:
            # The helper substitutes the configured cookie path with a
            # temporary writable copy. Do not edit the mounted secret file.
            with exec_cookie_copy(auth_cmd) as exec_cmd:
                process = await asyncio.create_subprocess_exec(
                    *exec_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                communicate_task = asyncio.create_task(
                    process.communicate()
                )

                try:
                    stdout, stderr = await asyncio.wait_for(
                        asyncio.shield(communicate_task),
                        timeout=timeout_seconds,
                    )

                except asyncio.TimeoutError as timeout_error:
                    if process.returncode is None:
                        process.kill()

                    try:
                        stdout, stderr = await asyncio.wait_for(
                            communicate_task,
                            timeout=3.0,
                        )
                    except Exception:
                        stdout, stderr = b"", b""

                    logger.error(
                        "youtube_authentication_test_timed_out",
                        js_runtime=runtime_name,
                        timeout_seconds=timeout_seconds,
                        stderr=stderr.decode(
                            errors="replace"
                        )[-1500:],
                    )

                    raise CookieError(
                        "YouTube authentication test timed out after "
                        f"{int(timeout_seconds)} seconds. Check network "
                        "access and the authentication diagnostics."
                    ) from timeout_error

            if process.returncode == 0:
                logger.info(
                    "YouTube authentication test passed",
                    js_runtime=runtime_name,
                )
                return True

            error_msg = (
                stderr.decode(errors="replace")
                if stderr
                else "Unknown error"
            )

            logger.error(
                "youtube_authentication_test_failed",
                js_runtime=runtime_name,
                returncode=process.returncode,
                error=error_msg[-1500:],
            )

            raise CookieError(
                "YouTube extraction/authentication test failed. "
                "The cookie may be expired or invalid, or YouTube may "
                "be rejecting the request. Check the server logs."
            )

        except CookieError:
            raise

        except FileNotFoundError as error:
            logger.error(
                "yt-dlp or JavaScript runtime not found",
                error=str(error),
            )
            raise CookieError(
                "yt-dlp or the configured JavaScript runtime is not "
                "installed or not in PATH."
            ) from error

        except Exception as error:
            logger.error(
                "YouTube authentication test error",
                error=str(error),
                exc_info=True,
            )
            raise CookieError(
                f"Failed to test YouTube authentication: {error}"
            ) from error

        finally:
            if process is not None and process.returncode is None:
                process.kill()

                try:
                    await asyncio.wait_for(
                        process.wait(),
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

    def get_validation_cache_status(self) -> Dict[str, dict]:
        """Get the validation cache status for each configured provider."""
        result = {}

        for provider in self.provider_cookies.keys():
            cached = provider in self.validation_cache

            result[provider] = {
                "cached": cached,
                "is_valid": (
                    self.validation_cache.get(provider)
                    if cached
                    else None
                ),
                "last_file_check": self.last_file_check.get(provider),
                "file_mtime": self.file_mtimes.get(provider),
            }

        return result

    async def reload_cookie(self, provider: str) -> dict:
        """Reload and validate a provider's cookie file.

        If validation fails, the previous validation state is restored.

        Args:
            provider: Provider name.

        Returns:
            Dictionary describing the reload result.

        Raises:
            CookieError: If the provider is not configured or reload fails.
        """
        if provider not in self.provider_cookies:
            raise CookieError(
                f"Provider '{provider}' not configured"
            )

        logger.info(
            "Starting cookie reload",
            provider=provider,
        )

        previous_valid = self.validation_cache.get(provider)
        previous_mtime = self.file_mtimes.get(provider)

        try:
            self._invalidate_cache(provider)
            self.last_file_check[provider] = 0

            is_valid = await self.validate_cookie(provider)

            if not is_valid:
                raise CookieError(
                    f"New cookie for {provider} failed validation"
                )

            logger.info(
                "Cookie reload successful",
                provider=provider,
            )

            return {
                "success": True,
                "provider": provider,
                "message": (
                    f"Cookie for {provider} reloaded and validated successfully"
                ),
                "age_hours": self.get_cookie_age_hours(provider),
            }

        except Exception as error:
            logger.error(
                "Cookie reload failed, rolling back",
                provider=provider,
                error=str(error),
            )

            if previous_valid is not None:
                self.validation_cache[provider] = previous_valid
                logger.info(
                    "Rolled back to previous validation state",
                    provider=provider,
                )

            if previous_mtime is not None:
                self.file_mtimes[provider] = previous_mtime

            raise CookieError(
                f"Failed to reload cookie for {provider}: {error}. "
                "Previous cookie state has been retained."
            ) from error
