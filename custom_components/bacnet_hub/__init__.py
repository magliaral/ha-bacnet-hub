from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict, List

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    AUTO_SYNC_INTERVAL_SECONDS,
    CONF_IMPORT_AREAS,
    CONF_IMPORT_LABEL,
    CONF_PUBLISH_MODE,
    DEFAULT_PUBLISH_MODE,
    DOMAIN,
    PUBLISH_MODE_AREAS,
    PUBLISH_MODE_CLASSIC,
    PUBLISH_MODE_LABELS,
)
from .discovery import (
    determine_object_type_and_units,
    entity_friendly_name,
    entity_ids_for_areas,
    entity_ids_for_label,
)

_LOGGER = logging.getLogger(__name__)

KEY_SERVERS = "servers"
KEY_LOCKS = "locks"
KEY_PUBLISHED_CACHE = "published"
KEY_SUPPRESS_RELOAD = "suppress_reload"
KEY_AUTO_SYNC_UNSUB = "auto_sync_unsub"

PLATFORMS: List[str] = ["sensor", "binary_sensor"]

# Optional: if True, we write the updated friendly_names
# back to entry.options (once). We then suppress the reload.
PERSIST_FRIENDLY_ON_START = True


def _ensure_domain(hass: HomeAssistant) -> Dict[str, Any]:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(KEY_SERVERS, {})
    hass.data[DOMAIN].setdefault(KEY_LOCKS, {})
    hass.data[DOMAIN].setdefault(KEY_PUBLISHED_CACHE, {})
    hass.data[DOMAIN].setdefault(KEY_AUTO_SYNC_UNSUB, {})
    return hass.data[DOMAIN]


def _normalize_publish_mode(value: Any) -> str:
    mode = str(value or DEFAULT_PUBLISH_MODE).strip().lower()
    if mode in (PUBLISH_MODE_CLASSIC, PUBLISH_MODE_LABELS, PUBLISH_MODE_AREAS):
        return mode
    return DEFAULT_PUBLISH_MODE


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        txt = value.strip()
        return [txt] if txt else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _refresh_friendly_names_inplace(hass: HomeAssistant, published: List[Dict[str, Any]]) -> bool:
    """Update friendly_name in-place from current HA state."""
    changed = False
    for mapping in (published or []):
        entity_id = mapping.get("entity_id")
        if not entity_id:
            continue
        new_name = entity_friendly_name(hass, entity_id)
        old_name = mapping.get("friendly_name")
        if new_name and new_name != old_name:
            mapping["friendly_name"] = new_name
            changed = True
    return changed


def _entry_by_id(hass: HomeAssistant, entry_id: str) -> ConfigEntry | None:
    getter = getattr(hass.config_entries, "async_get_entry", None)
    if callable(getter):
        return getter(entry_id)

    entries = hass.config_entries.async_entries(DOMAIN)
    for entry in entries:
        if entry.entry_id == entry_id:
            return entry
    return None


def _ensure_counter_floor(counters: Dict[str, int], published: List[Dict[str, Any]]) -> bool:
    changed = False
    max_by_type: Dict[str, int] = {}
    for mapping in published:
        if not isinstance(mapping, dict):
            continue
        object_type = str(mapping.get("object_type") or "")
        if not object_type:
            continue
        instance = _as_int(mapping.get("instance"), -1)
        if instance < 0:
            continue
        prev = max_by_type.get(object_type, -1)
        if instance > prev:
            max_by_type[object_type] = instance

    for object_type, max_instance in max_by_type.items():
        floor = max_instance + 1
        current = _as_int(counters.get(object_type), 0)
        if current < floor:
            counters[object_type] = floor
            changed = True
    return changed


def _next_instance(
    object_type: str, counters: Dict[str, int], published: List[Dict[str, Any]]
) -> int:
    max_instance = -1
    for mapping in published:
        if mapping.get("object_type") != object_type:
            continue
        inst = _as_int(mapping.get("instance"), -1)
        if inst > max_instance:
            max_instance = inst
    floor = max_instance + 1
    next_idx = max(_as_int(counters.get(object_type), 0), floor)
    counters[object_type] = next_idx + 1
    return next_idx


def _auto_target_entity_ids(hass: HomeAssistant, options: Dict[str, Any], mode: str) -> set[str]:
    if mode == PUBLISH_MODE_LABELS:
        label_id = str(options.get(CONF_IMPORT_LABEL) or "").strip()
        return entity_ids_for_label(hass, label_id)
    if mode == PUBLISH_MODE_AREAS:
        area_ids = set(_as_string_list(options.get(CONF_IMPORT_AREAS)))
        return entity_ids_for_areas(hass, area_ids)
    return set()


async def _async_sync_auto_mappings(hass: HomeAssistant, entry_id: str) -> None:
    entry = _entry_by_id(hass, entry_id)
    if not entry:
        return

    current_options = dict(entry.options or {})
    had_legacy_ui_keys = any(
        key in current_options for key in ("ui_mode", "show_technical_ids", "label_template")
    )
    options = dict(current_options)
    # Migrate removed UI keys on next save/update.
    options.pop("ui_mode", None)
    options.pop("show_technical_ids", None)
    options.pop("label_template", None)

    mode = _normalize_publish_mode(options.get(CONF_PUBLISH_MODE, DEFAULT_PUBLISH_MODE))
    published: List[Dict[str, Any]] = list(options.get("published", []))
    counters: Dict[str, int] = dict(options.get("counters", {}))
    original_published = list(published)
    original_counters = dict(counters)

    _ensure_counter_floor(counters, published)
    targets = _auto_target_entity_ids(hass, options, mode)

    kept: List[Dict[str, Any]] = []
    existing_entity_ids: set[str] = set()
    removed_count = 0

    for mapping in published:
        if not isinstance(mapping, dict):
            continue
        entity_id = str(mapping.get("entity_id") or "")
        if not entity_id:
            removed_count += 1
            continue

        is_auto = bool(mapping.get("auto", False))
        if not is_auto:
            kept.append(mapping)
            existing_entity_ids.add(entity_id)
            continue

        auto_mode = str(mapping.get("auto_mode") or "")
        if mode not in (PUBLISH_MODE_LABELS, PUBLISH_MODE_AREAS):
            removed_count += 1
            continue
        if auto_mode != mode:
            removed_count += 1
            continue
        if entity_id not in targets:
            removed_count += 1
            continue

        kept.append(mapping)
        existing_entity_ids.add(entity_id)

    added_count = 0
    for entity_id in sorted(targets):
        if entity_id in existing_entity_ids:
            continue

        object_type, units = determine_object_type_and_units(hass, entity_id)
        instance = _next_instance(object_type, counters, kept)
        kept.append(
            {
                "entity_id": entity_id,
                "object_type": object_type,
                "instance": instance,
                "units": units,
                "writable": False,
                "friendly_name": entity_friendly_name(hass, entity_id),
                "auto": True,
                "auto_mode": mode,
            }
        )
        existing_entity_ids.add(entity_id)
        added_count += 1

    changed = (kept != original_published) or (counters != original_counters) or had_legacy_ui_keys
    if not changed:
        return

    new_options = dict(entry.options or {})
    new_options.pop("ui_mode", None)
    new_options.pop("show_technical_ids", None)
    new_options.pop("label_template", None)
    new_options["published"] = kept
    new_options["counters"] = counters
    await hass.config_entries.async_update_entry(entry, options=new_options)
    _LOGGER.info(
        "Auto mapping sync updated entry %s (mode=%s, added=%d, removed=%d)",
        entry_id,
        mode,
        added_count,
        removed_count,
    )


def _start_auto_sync(hass: HomeAssistant, entry_id: str):
    @callback
    def _tick(_now):
        hass.async_create_task(_async_sync_auto_mappings(hass, entry_id))

    return async_track_time_interval(
        hass,
        _tick,
        timedelta(seconds=AUTO_SYNC_INTERVAL_SECONDS),
    )


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
    from .server import BacnetHubServer

    data = _ensure_domain(hass)
    servers: dict[str, BacnetHubServer] = data[KEY_SERVERS]
    locks: dict[str, asyncio.Lock] = data[KEY_LOCKS]
    auto_unsubs: dict[str, Any] = data[KEY_AUTO_SYNC_UNSUB]

    prev = servers.get(entry.entry_id)
    if prev is not None:
        _LOGGER.warning("Existing instance found for %s - stopping before re-setup.", entry.entry_id)
        try:
            await prev.stop()
        except Exception:
            _LOGGER.exception("Error stopping old instance.")
        finally:
            servers.pop(entry.entry_id, None)

    prev_unsub = auto_unsubs.pop(entry.entry_id, None)
    if prev_unsub:
        try:
            prev_unsub()
        except Exception:
            pass

    merged_config: Dict[str, Any] = {**(entry.data or {}), **(entry.options or {})}
    _LOGGER.debug("Merged options for %s (%s): %s", DOMAIN, entry.entry_id, merged_config)

    published: List[Dict[str, Any]] = merged_config.get("published") or []

    updated_now = _refresh_friendly_names_inplace(hass, published)
    _LOGGER.debug(
        "Initial friendly_name sync: %d names updated",
        sum(1 for item in published if item.get("friendly_name")),
    )

    data[KEY_PUBLISHED_CACHE][entry.entry_id] = published

    if PERSIST_FRIENDLY_ON_START and updated_now:
        try:
            new_options = dict(entry.options or {})
            new_options["published"] = published
            hass.data[DOMAIN][KEY_SUPPRESS_RELOAD] = True
            await hass.config_entries.async_update_entry(entry, options=new_options)
            _LOGGER.info("friendly_name updated in options (Entry %s).", entry.entry_id)
        except Exception:
            _LOGGER.exception("Could not update options (friendly_name sync).")

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

                server = hass.data[DOMAIN][KEY_SERVERS].get(entry.entry_id)
                if server and server.publisher:
                    await server.publisher.update_descriptions()
                    _LOGGER.debug("BACnet descriptions updated after friendly_name sync")
        except Exception:
            _LOGGER.debug("Late friendly_name sync failed.", exc_info=True)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _late_sync)

    server = BacnetHubServer(hass, merged_config)
    lock = locks.setdefault(entry.entry_id, asyncio.Lock())

    async with lock:
        await server.start()

    servers[entry.entry_id] = server

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

    auto_unsubs[entry.entry_id] = _start_auto_sync(hass, entry.entry_id)
    hass.async_create_task(_async_sync_auto_mappings(hass, entry.entry_id))

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _LOGGER.info("%s started (Entry %s)", DOMAIN, entry.entry_id)

    logging.getLogger("bacpypes3.service.cov").setLevel(logging.DEBUG)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = _ensure_domain(hass)
    servers: dict[str, Any] = data[KEY_SERVERS]
    locks: dict[str, asyncio.Lock] = data[KEY_LOCKS]
    auto_unsubs: dict[str, Any] = data[KEY_AUTO_SYNC_UNSUB]

    unsub = auto_unsubs.pop(entry.entry_id, None)
    if unsub:
        try:
            unsub()
        except Exception:
            pass

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    server = servers.pop(entry.entry_id, None)
    lock = locks.setdefault(entry.entry_id, asyncio.Lock())

    data[KEY_PUBLISHED_CACHE].pop(entry.entry_id, None)

    if server is None:
        _LOGGER.debug("No running server for entry %s - nothing to unload.", entry.entry_id)
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
    if hass.data.get(DOMAIN, {}).pop(KEY_SUPPRESS_RELOAD, False):
        _LOGGER.debug("Reload suppressed (friendly names synchronized).")
        return
    _LOGGER.debug("Options update for %s (Entry %s) - starting reload.", DOMAIN, entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
