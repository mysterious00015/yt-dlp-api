"""Provider manager for registration and selection."""

from typing import Any, Callable, Dict, Optional

import structlog

from app.providers.base import VideoProvider
from app.providers.exceptions import InvalidURLError, ProviderError

logger = structlog.get_logger(__name__)


class ProviderManager:
    """Manages video provider registration and selection."""

    def __init__(self) -> None:
        """Initialize the provider manager."""
        self._providers: Dict[str, VideoProvider] = {}
        self._enabled_providers: Dict[str, bool] = {}

    def register_provider(
        self,
        name: str,
        provider: VideoProvider,
        enabled: bool = True,
    ) -> None:
        """Register a provider and configure whether it is enabled."""
        if not name or not isinstance(name, str):
            raise ValueError("Provider name must be a non-empty string")

        self._providers[name] = provider
        self._enabled_providers[name] = enabled

        logger.info(
            "Provider registered",
            provider=name,
            enabled=enabled,
        )

    def enable_provider(self, name: str) -> None:
        """Enable a registered provider."""
        if name not in self._providers:
            raise ValueError(f"Provider '{name}' is not registered")

        self._enabled_providers[name] = True
        logger.info("Provider enabled", provider=name)

    def disable_provider(self, name: str) -> None:
        """Disable a registered provider."""
        if name not in self._providers:
            raise ValueError(f"Provider '{name}' is not registered")

        self._enabled_providers[name] = False
        logger.info("Provider disabled", provider=name)

    def is_provider_enabled(self, name: str) -> bool:
        """Return whether a provider is enabled."""
        return self._enabled_providers.get(name, False)

    @staticmethod
    def _provider_priority(name: str) -> tuple[int, str]:
        """Return provider selection priority.

        YouTube must use its dedicated provider. Other specifically
        registered providers get a chance before the generic provider.
        The generic provider is the fallback for recognized yt-dlp sites.
        """
        normalized = name.lower()

        if normalized == "youtube":
            return (0, normalized)

        if normalized in {
            "generic_ytdlp",
            "generic",
            "ytdlp_generic",
        }:
            return (100, normalized)

        return (10, normalized)

    def get_provider_for_url(self, url: str) -> VideoProvider:
        """Select the highest-priority enabled provider that accepts a URL.

        Raises:
            InvalidURLError: If no enabled provider accepts the URL.
        """
        if not isinstance(url, str) or not url.strip():
            raise InvalidURLError("A valid media URL is required.")

        ordered_names = sorted(
            self._providers.keys(),
            key=self._provider_priority,
        )

        for name in ordered_names:
            provider = self._providers[name]

            if not self._enabled_providers.get(name, False):
                continue

            try:
                if provider.validate_url(url):
                    logger.info(
                        "Provider selected for URL",
                        provider=name,
                        # Avoid logging the full URL, which may contain
                        # private query parameters or access tokens.
                        hostname=self._safe_hostname(url),
                    )
                    return provider

            except Exception as exc:
                # A faulty provider must not prevent other providers
                # from checking the URL.
                logger.warning(
                    "Provider validation error",
                    provider=name,
                    hostname=self._safe_hostname(url),
                    error=str(exc)[:300],
                )

        raise InvalidURLError(
            "No enabled provider can handle this URL. "
            "The website may not be supported by the installed "
            "yt-dlp extractors, or the relevant provider may be disabled."
        )

    @staticmethod
    def _safe_hostname(url: str) -> str:
        """Extract a hostname for logs without recording URL parameters."""
        from urllib.parse import urlparse

        try:
            return (urlparse(url).hostname or "unknown")[:200]
        except ValueError:
            return "unknown"

    def get_provider_by_name(
        self,
        name: str,
    ) -> Optional[VideoProvider]:
        """Return a registered provider by name, or None if absent."""
        return self._providers.get(name)

    def list_providers(self) -> Dict[str, bool]:
        """Return registered provider names and their enabled states."""
        return {
            name: self._enabled_providers.get(name, False)
            for name in self._providers
        }

    async def execute_with_error_isolation(
        self,
        provider_name: str,
        operation: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute a provider operation with consistent error handling."""
        try:
            logger.debug(
                "Executing provider operation",
                provider=provider_name,
                operation=getattr(operation, "__name__", "unknown"),
            )

            result = await operation(*args, **kwargs)

            logger.debug(
                "Provider operation completed",
                provider=provider_name,
                operation=getattr(operation, "__name__", "unknown"),
            )

            return result

        except ProviderError:
            raise

        except Exception as exc:
            operation_name = getattr(
                operation,
                "__name__",
                "unknown",
            )

            logger.error(
                "Provider operation failed",
                provider=provider_name,
                operation=operation_name,
                error=str(exc),
                exc_info=True,
            )

            raise ProviderError(
                f"Provider '{provider_name}' encountered an unexpected error: "
                f"{str(exc)}"
            ) from exc
