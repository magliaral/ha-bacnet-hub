from __future__ import annotations
import logging
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from .const import DOMAIN
from .server import BacnetHubServer

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    # keine YAML-Konfig mehr – nur Config Flow
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    server = BacnetHubServer(hass, config=entry.data | entry.options)
    await server.start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = server

    async def _stop(_event):
        await server.stop()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)

    _LOGGER.info("BACnet Hub gestartet (Entry %s)", entry.title or entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    server: BacnetHubServer | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if server:
        await server.stop()
    return True
