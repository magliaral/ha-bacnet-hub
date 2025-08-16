from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .server import BacnetHubServer

_LOGGER = logging.getLogger(__name__)

# Mehrere Instanzen: ein Server je Entry-ID
DATA_SERVERS = f"{DOMAIN}_servers"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BACnet Hub from a config entry."""
    merged: Dict[str, Any] = {**entry.data, **(entry.options or {})}
    _LOGGER.debug("Merged options for %s (%s): %s", DOMAIN, entry.entry_id, merged)

    domain_data = hass.data.setdefault(DOMAIN, {})
    servers: Dict[str, BacnetHubServer] = domain_data.setdefault(DATA_SERVERS, {})

    # Falls schon ein Server für diesen Entry läuft: erst sauber stoppen
    old = servers.get(entry.entry_id)
    if old:
        try:
            _LOGGER.debug("Stoppe bestehenden BACnet-Server für Entry %s", entry.entry_id)
            await old.stop()
            # kleiner Yield, damit der UDP-Socket garantiert frei ist
            await asyncio.sleep(0)
        except Exception:
            _LOGGER.exception("Fehler beim Stoppen des alten Servers (Entry %s)", entry.entry_id)

    server = BacnetHubServer(hass, merged)
    servers[entry.entry_id] = server

    try:
        await server.start()
    except Exception as err:
        # True zurückgeben, damit der Eintrag im UI bleibt
        _LOGGER.error("BACnet Hub konnte nicht starten (Entry %s): %s", entry.entry_id, err, exc_info=True)
        return True

    async def _update_listener(hass: HomeAssistant, updated_entry: ConfigEntry) -> None:
        """Auf Options-Änderungen reagieren, ohne den Options-Flow-Request zu blockieren."""
        _LOGGER.debug("Optionen geändert – %s (%s) wird neu geladen", DOMAIN, updated_entry.entry_id)

        # WICHTIG: nicht awaiten, sondern asynchron planen -> blockiert die Web-UI nicht
        hass.async_create_task(hass.config_entries.async_reload(updated_entry.entry_id))

    unsub = entry.add_update_listener(_update_listener)
    entry.async_on_unload(unsub)

    _LOGGER.info("BACnet Hub gestartet (Entry %s)", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stoppe nur den Server dieses Eintrags."""
    domain_data = hass.data.get(DOMAIN, {})
    servers: Dict[str, BacnetHubServer] = domain_data.get(DATA_SERVERS, {})
    server: Optional[BacnetHubServer] = servers.pop(entry.entry_id, None)

    if server:
        try:
            await server.stop()
        except Exception as err:
            _LOGGER.warning("Fehler beim Stoppen des BACnet Hub (Entry %s): %s", entry.entry_id, err, exc_info=True)

    # optional: Datenstruktur aufräumen, wenn keine Server mehr laufen
    if not servers:
        domain_data.pop(DATA_SERVERS, None)

    return True
