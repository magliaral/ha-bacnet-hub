# custom_components/bacnet_hub/helpers/versions.py
from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from homeassistant.loader import async_get_integration
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "get_integration_version",
    "get_bacpypes3_version",
]

async def get_integration_version(hass: HomeAssistant, domain: str) -> str | None:
    """Version from manifest.json (async, non-blocking)."""
    try:
        integration = await async_get_integration(hass, domain)
        ver = integration.version or None
        if ver is None:
            _LOGGER.debug("Integration '%s' has no version in manifest.", domain)
        return ver
    except Exception as err:
        _LOGGER.debug(
            "Could not determine integration version for '%s': %s",
            domain, err, exc_info=True
        )
        return None

def _get_bacpypes3_version_sync() -> str | None:
    """Sync: call in executor."""
    try:
        return version("bacpypes3")
    except PackageNotFoundError:
        _LOGGER.debug("importlib.metadata did not find 'bacpypes3'; trying __version__.")
    except Exception as err:
        _LOGGER.debug("Error with metadata version('bacpypes3'): %s", err, exc_info=True)

    try:
        import bacpypes3  # type: ignore
        ver = getattr(bacpypes3, "__version__", None)
        if ver is None:
            _LOGGER.debug("bacpypes3.__version__ is missing.")
        return ver
    except Exception as err:
        _LOGGER.debug("Fallback import bacpypes3 for version failed: %s", err, exc_info=True)
        return None

async def get_bacpypes3_version(hass: HomeAssistant) -> str | None:
    """Async wrapper (Executor) so no blocking I/O lands in the event loop."""
    try:
        return await hass.async_add_executor_job(_get_bacpypes3_version_sync)
    except Exception as err:
        _LOGGER.debug("Executor call for bacpypes3 version failed: %s", err, exc_info=True)
        return None
