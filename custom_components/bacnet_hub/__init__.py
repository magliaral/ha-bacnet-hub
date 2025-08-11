from __future__ import annotations
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from .const import DOMAIN
from .server import BacnetServer

PLATFORMS: list[str] = []

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    server = BacnetServer(hass, entry)
    try:
        await server.start()
    except Exception as err:
        raise ConfigEntryNotReady(str(err))

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = server

    async def _options_updated(hass_: HomeAssistant, updated_entry: ConfigEntry):
        await server.reload(updated_entry.options)

    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    server: BacnetServer = hass.data[DOMAIN].pop(entry.entry_id)
    await server.stop()
    return True
