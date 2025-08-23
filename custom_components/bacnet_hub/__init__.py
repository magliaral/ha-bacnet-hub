from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

KEY_SERVERS = "servers"
KEY_LOCKS = "locks"
KEY_PUBLISHED_CACHE = "published"

PLATFORMS: List[str] = ["sensor", "binary_sensor"]


def _ensure_domain(hass: HomeAssistant) -> Dict[str, Any]:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(KEY_SERVERS, {})
    hass.data[DOMAIN].setdefault(KEY_LOCKS, {})
    hass.data[DOMAIN].setdefault(KEY_PUBLISHED_CACHE, {})
    return hass.data[DOMAIN]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    data = _ensure_domain(hass)

    async def _svc_reload(call: ServiceCall) -> None:
        entry_id = call.data.get("entry_id")
        servers: dict = data[KEY_SERVERS]
        if not entry_id:
            if len(servers) != 1:
                _LOGGER.error(
                    "Reload-Service ohne entry_id aufgerufen, aber %d Einträge vorhanden.",
                    len(servers),
                )
                return
            entry_id = next(iter(servers.keys()))
        _LOGGER.info("Service reload für Entry %s", entry_id)
        await hass.config_entries.async_reload(entry_id)

    hass.services.async_register(DOMAIN, "reload", _svc_reload)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Lazy-Import, damit der Loader nicht im Eventloop blockiert
    from .server import BacnetHubServer

    data = _ensure_domain(hass)
    servers: dict[str, BacnetHubServer] = data[KEY_SERVERS]
    locks: dict[str, asyncio.Lock] = data[KEY_LOCKS]

    prev = servers.get(entry.entry_id)
    if prev is not None:
        _LOGGER.warning("Vorhandene Instanz für %s gefunden – stoppe vor Neu-Setup.", entry.entry_id)
        try:
            await prev.stop()
        except Exception:
            _LOGGER.exception("Fehler beim Stoppen der alten Instanz.")
        finally:
            servers.pop(entry.entry_id, None)

    merged_config: Dict[str, Any] = {**(entry.data or {}), **(entry.options or {})}
    _LOGGER.debug("Merged options for %s (%s): %s", DOMAIN, entry.entry_id, merged_config)

    # Publish-Mappings für Plattformen cachen (falls Sensor/BS später genutzt)
    published = merged_config.get("published") or []
    data[KEY_PUBLISHED_CACHE][entry.entry_id] = published

    server = BacnetHubServer(hass, merged_config)
    lock = locks.setdefault(entry.entry_id, asyncio.Lock())

    async with lock:
        await server.start()

    servers[entry.entry_id] = server

    # Device Registry
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="Home Assistant",
        model="BACpypes 3",
        name="BACnet Hub (Local Device)",
        sw_version=str(merged_config.get("application_software_version", "0.1.1")),
    )

    # Optional: eigene Plattformen laden (aktuell Dummy)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _LOGGER.info("%s gestartet (Entry %s)", DOMAIN, entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = _ensure_domain(hass)
    servers: dict[str, Any] = data[KEY_SERVERS]
    locks: dict[str, asyncio.Lock] = data[KEY_LOCKS]

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    server = servers.pop(entry.entry_id, None)
    lock = locks.setdefault(entry.entry_id, asyncio.Lock())

    data[KEY_PUBLISHED_CACHE].pop(entry.entry_id, None)

    if server is None:
        _LOGGER.debug("Kein laufender Server für Entry %s – nichts zu entladen.", entry.entry_id)
        return unload_ok

    async with lock:
        try:
            await server.stop()
        except Exception:
            _LOGGER.exception("Fehler beim Stoppen von %s (Entry %s).", DOMAIN, entry.entry_id)

    _LOGGER.info("%s gestoppt (Entry %s)", DOMAIN, entry.entry_id)
    return unload_ok


@callback
async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    _LOGGER.debug("Options-Update für %s (Entry %s) – starte Reload.", DOMAIN, entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
