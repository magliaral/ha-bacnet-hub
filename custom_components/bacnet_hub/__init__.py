# custom_components/bacnet_hub/__init__.py
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

KEY_SERVERS = "servers"
KEY_LOCKS = "locks"
KEY_PUBLISHED_CACHE = "published"
KEY_SUPPRESS_RELOAD = "suppress_reload"

PLATFORMS: List[str] = ["sensor", "binary_sensor"]

# Optional: wenn True, schreiben wir die aktualisierten friendly_names
# zurück in entry.options (einmalig). Wir unterdrücken dann den Reload.
PERSIST_FRIENDLY_ON_START = True


def _ensure_domain(hass: HomeAssistant) -> Dict[str, Any]:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(KEY_SERVERS, {})
    hass.data[DOMAIN].setdefault(KEY_LOCKS, {})
    hass.data[DOMAIN].setdefault(KEY_PUBLISHED_CACHE, {})
    return hass.data[DOMAIN]


def _refresh_friendly_names_inplace(hass: HomeAssistant, published: List[Dict[str, Any]]) -> bool:
    """Aktualisiert friendly_name in-place aus dem aktuellen HA-State.
    Gibt True zurück, wenn sich mindestens ein Name geändert hat."""
    changed = False
    for m in (published or []):
        ent = m.get("entity_id")
        if not ent:
            continue
        st = hass.states.get(ent)
        if not st:
            continue
        new_name = st.name or ent
        old_name = m.get("friendly_name")
        if new_name and new_name != old_name:
            m["friendly_name"] = new_name
            changed = True
    return changed


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
    # Lazy-Import
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

    # Publish-Mappings laden
    published: List[Dict[str, Any]] = merged_config.get("published") or []

    # 1) Sofortige (Best-Effort) Aktualisierung der friendly_names
    updated_now = _refresh_friendly_names_inplace(hass, published)

    # Cache für Plattformen
    data[KEY_PUBLISHED_CACHE][entry.entry_id] = published

    # 2) Optional persistent machen (ohne Reload-Schleife)
    if PERSIST_FRIENDLY_ON_START and updated_now:
        try:
            new_options = dict(entry.options or {})
            new_options["published"] = published
            # Unterdrücke den Reload einmalig
            hass.data[DOMAIN][KEY_SUPPRESS_RELOAD] = True
            await hass.config_entries.async_update_entry(entry, options=new_options)
            _LOGGER.info("friendly_name in Optionen aktualisiert (Entry %s).", entry.entry_id)
        except Exception:
            _LOGGER.exception("Konnte Optionen nicht aktualisieren (friendly_name sync).")

    # 3) Später nochmal syncen, wenn HA vollständig gestartet ist
    async def _late_sync(_):
        try:
            late_published = list(hass.data[DOMAIN][KEY_PUBLISHED_CACHE].get(entry.entry_id, published))
            if _refresh_friendly_names_inplace(hass, late_published):
                hass.data[DOMAIN][KEY_PUBLISHED_CACHE][entry.entry_id] = late_published
                if PERSIST_FRIENDLY_ON_START:
                    new_options = dict(entry.options or {})
                    new_options["published"] = late_published
                    hass.data[DOMAIN][KEY_SUPPRESS_RELOAD] = True
                    await hass.config_entries.async_update_entry(entry, options=new_options)
                    _LOGGER.info("friendly_name nach Start erneut aktualisiert (Entry %s).", entry.entry_id)
        except Exception:
            _LOGGER.debug("Late friendly_name sync fehlgeschlagen.", exc_info=True)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _late_sync)

    # Server starten
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

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _LOGGER.info("%s gestartet (Entry %s)", DOMAIN, entry.entry_id)

    # Debug: aktiviere COV-Logs
    logging.getLogger("bacpypes3.service.cov").setLevel(logging.DEBUG)

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
    # Ein optionaler Writeback der Optionen zum friendly_name würde hier sonst
    # sofort wieder einen Reload triggern. Einmalig unterdrücken.
    if hass.data.get(DOMAIN, {}).pop(KEY_SUPPRESS_RELOAD, False):
        _LOGGER.debug("Reload unterdrückt (freundliche Namen synchronisiert).")
        return
    _LOGGER.debug("Options-Update für %s (Entry %s) – starte Reload.", DOMAIN, entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
