import logging
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from .const import DOMAIN
from .server import BacnetHubServer

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Nur für YAML-Setup (nicht genutzt)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Setup der Integration über den Config Flow."""
    hass.data.setdefault(DOMAIN, {})

    server = BacnetHubServer(hass, entry.data | entry.options)
    await server.start()
    hass.data[DOMAIN][entry.entry_id] = server

    async def _on_stop(_event):
        await server.stop()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)

    _LOGGER.info("BACnet Hub gestartet (Entry %s)", entry.title or entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Wird beim Entfernen der Integration aufgerufen."""
    server: BacnetHubServer | None = hass.data[DOMAIN].pop(entry.entry_id, None)
    if server:
        await server.stop()
    return True
