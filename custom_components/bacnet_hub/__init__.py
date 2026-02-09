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

# Optional: if True, we write the updated friendly_names
# back to entry.options (once). We then suppress the reload.
PERSIST_FRIENDLY_ON_START = True


def _ensure_domain(hass: HomeAssistant) -> Dict[str, Any]:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(KEY_SERVERS, {})
    hass.data[DOMAIN].setdefault(KEY_LOCKS, {})
    hass.data[DOMAIN].setdefault(KEY_PUBLISHED_CACHE, {})
    return hass.data[DOMAIN]


def _refresh_friendly_names_inplace(hass: HomeAssistant, published: List[Dict[str, Any]]) -> bool:
    """Updates friendly_name in-place from current HA state.
    Returns True if at least one name changed."""
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
                    "Reload service called without entry_id, but %d entries exist.",
                    len(servers),
                )
                return
            entry_id = next(iter(servers.keys()))
        _LOGGER.info("Service reload for entry %s", entry_id)
        await hass.config_entries.async_reload(entry_id)

    hass.services.async_register(DOMAIN, "reload", _svc_reload)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Lazy import
    from .server import BacnetHubServer

    data = _ensure_domain(hass)
    servers: dict[str, BacnetHubServer] = data[KEY_SERVERS]
    locks: dict[str, asyncio.Lock] = data[KEY_LOCKS]

    prev = servers.get(entry.entry_id)
    if prev is not None:
        _LOGGER.warning("Existing instance found for %s – stopping before re-setup.", entry.entry_id)
        try:
            await prev.stop()
        except Exception:
            _LOGGER.exception("Error stopping old instance.")
        finally:
            servers.pop(entry.entry_id, None)

    merged_config: Dict[str, Any] = {**(entry.data or {}), **(entry.options or {})}
    _LOGGER.debug("Merged options for %s (%s): %s", DOMAIN, entry.entry_id, merged_config)

    # Load publish mappings
    published: List[Dict[str, Any]] = merged_config.get("published") or []

    # 1) Immediate (best-effort) friendly_name update
    updated_now = _refresh_friendly_names_inplace(hass, published)
    _LOGGER.debug("Initial friendly_name sync: %d names updated",
                  sum(1 for m in published if m.get("friendly_name")))

    # Cache for platforms
    data[KEY_PUBLISHED_CACHE][entry.entry_id] = published

    # 2) Optionally persist (without reload loop)
    if PERSIST_FRIENDLY_ON_START and updated_now:
        try:
            new_options = dict(entry.options or {})
            new_options["published"] = published
            # Suppress reload once
            hass.data[DOMAIN][KEY_SUPPRESS_RELOAD] = True
            await hass.config_entries.async_update_entry(entry, options=new_options)
            _LOGGER.info("friendly_name updated in options (Entry %s).", entry.entry_id)
        except Exception:
            _LOGGER.exception("Could not update options (friendly_name sync).")

    # 3) Sync again later when HA is fully started
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
                    _LOGGER.info("friendly_name updated again after start (Entry %s).", entry.entry_id)

                # Update descriptions in BACnet objects
                server = hass.data[DOMAIN][KEY_SERVERS].get(entry.entry_id)
                if server and server.publisher:
                    await server.publisher.update_descriptions()
                    _LOGGER.debug("BACnet descriptions updated after friendly_name sync")
        except Exception:
            _LOGGER.debug("Late friendly_name sync failed.", exc_info=True)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _late_sync)

    # Start server
    server = BacnetHubServer(hass, merged_config)
    lock = locks.setdefault(entry.entry_id, asyncio.Lock())

    async with lock:
        await server.start()

    servers[entry.entry_id] = server

    # Device registry
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="magliaral",
        model="BACnet Hub",
        name="BACnet Hub",
        sw_version=server.firmware_revision or "unknown",
        configuration_url="https://github.com/magliaral/ha-bacnet-hub",
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _LOGGER.info("%s started (Entry %s)", DOMAIN, entry.entry_id)

    # Debug: enable COV logs
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
        _LOGGER.debug("No running server for entry %s – nothing to unload.", entry.entry_id)
        return unload_ok

    async with lock:
        try:
            await server.stop()
        except Exception:
            _LOGGER.exception("Error stopping %s (Entry %s).", DOMAIN, entry.entry_id)

    _LOGGER.info("%s stopped (Entry %s)", DOMAIN, entry.entry_id)
    return unload_ok


@callback
async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # An optional writeback of options for friendly_name would otherwise
    # trigger a reload immediately. Suppress once.
    if hass.data.get(DOMAIN, {}).pop(KEY_SUPPRESS_RELOAD, False):
        _LOGGER.debug("Reload suppressed (friendly names synchronized).")
        return
    _LOGGER.debug("Options update for %s (Entry %s) – starting reload.", DOMAIN, entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
