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
    """Version aus manifest.json (async, non-blocking)."""
    try:
        integration = await async_get_integration(hass, domain)
        ver = integration.version or None
        if ver is None:
            _LOGGER.debug("Integration '%s' hat keine Version im Manifest.", domain)
        return ver
    except Exception as err:
        _LOGGER.debug(
            "Konnte Integrationsversion für '%s' nicht ermitteln: %s",
            domain, err, exc_info=True
        )
        return None

def _get_bacpypes3_version_sync() -> str | None:
    """Sync: im Executor aufrufen."""
    try:
        return version("bacpypes3")
    except PackageNotFoundError:
        _LOGGER.debug("importlib.metadata fand 'bacpypes3' nicht; versuche __version__.")
    except Exception as err:
        _LOGGER.debug("Fehler bei metadata version('bacpypes3'): %s", err, exc_info=True)

    try:
        import bacpypes3  # type: ignore
        ver = getattr(bacpypes3, "__version__", None)
        if ver is None:
            _LOGGER.debug("bacpypes3.__version__ fehlt.")
        return ver
    except Exception as err:
        _LOGGER.debug("Fallback-Import bacpypes3 für Version schlug fehl: %s", err, exc_info=True)
        return None

async def get_bacpypes3_version(hass: HomeAssistant) -> str | None:
    """Async-Wrapper (Executor), damit kein blocking I/O im Event-Loop landet."""
    try:
        return await hass.async_add_executor_job(_get_bacpypes3_version_sync)
    except Exception as err:
        _LOGGER.debug("Executor-Aufruf für bacpypes3-Version schlug fehl: %s", err, exc_info=True)
        return None
