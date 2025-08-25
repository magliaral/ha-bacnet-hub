# custom_components/bacnet_hub/helpers/versions.py

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version

from homeassistant.loader import async_get_integration

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "get_integration_version",
    "get_bacpypes3_version",
]


async def get_integration_version(hass, domain: str) -> str | None:
    """Return the integration version from its manifest (async).

    Args:
        hass: Home Assistant instance.
        domain: Integration domain, e.g. "bacnet_hub".

    Returns:
        The version string if available, otherwise None.
    """
    try:
        integration = await async_get_integration(hass, domain)
        ver = integration.version or None
        if ver is None:
            _LOGGER.debug(
                "Integration '%s' has no version in manifest (returned None).", domain
            )
        return ver
    except Exception as err:
        _LOGGER.debug(
            "Could not resolve integration version for domain '%s': %s",
            domain,
            err,
            exc_info=True,
        )
        return None


def get_bacpypes3_version() -> str | None:
    """Return the installed bacpypes3 package version.

    Tries importlib.metadata first, then falls back to bacpypes3.__version__.

    Returns:
        The version string if bacpypes3 is installed, otherwise None.
    """
    try:
        ver = version("bacpypes3")
        return ver
    except PackageNotFoundError:
        _LOGGER.debug(
            "importlib.metadata could not find 'bacpypes3' distribution; trying fallback."
        )
    except Exception as err:
        _LOGGER.debug(
            "Unexpected error while reading bacpypes3 version via metadata: %s",
            err,
            exc_info=True,
        )

    try:
        import bacpypes3  # type: ignore

        ver = getattr(bacpypes3, "__version__", None)
        if ver is None:
            _LOGGER.debug(
                "bacpypes3 module imported but __version__ attribute is missing."
            )
        return ver
    except Exception as err:
        _LOGGER.debug(
            "Fallback import of bacpypes3 failed while determining version: %s",
            err,
            exc_info=True,
        )
        return None
