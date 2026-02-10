from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any, Dict, List

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    AUTO_SYNC_INTERVAL_SECONDS,
    CONF_IMPORT_LABEL,
    CONF_IMPORT_LABELS,
    CONF_PUBLISH_MODE,
    DEFAULT_PUBLISH_MODE,
    DOMAIN,
    PUBLISH_MODE_LABELS,
)
from .discovery import (
    entity_mapping_candidates,
    entity_exists,
    entity_ids_for_labels,
    mapping_friendly_name,
    mapping_key,
)

_LOGGER = logging.getLogger(__name__)

KEY_SERVERS = "servers"
KEY_LOCKS = "locks"
KEY_PUBLISHED_CACHE = "published"
KEY_SUPPRESS_RELOAD = "suppress_reload"
KEY_AUTO_SYNC_UNSUB = "auto_sync_unsub"
KEY_RELOAD_LOCKS = "reload_locks"
KEY_LAST_RELOAD_FP = "last_reload_fingerprint"

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
    hass.data[DOMAIN].setdefault(KEY_RELOAD_LOCKS, {})
    hass.data[DOMAIN].setdefault(KEY_LAST_RELOAD_FP, {})
    return hass.data[DOMAIN]


def _normalize_publish_mode(value: Any) -> str:
    mode = str(value or DEFAULT_PUBLISH_MODE).strip().lower()
    if mode == PUBLISH_MODE_LABELS:
        return mode
    return PUBLISH_MODE_LABELS


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


def _options_fingerprint(options: Dict[str, Any]) -> str:
    """Stable fingerprint to suppress duplicate reloads for identical options."""
    try:
        return json.dumps(options or {}, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    except Exception:
        return repr(options or {})


def _refresh_friendly_names_inplace(hass: HomeAssistant, published: List[Dict[str, Any]]) -> bool:
    """Update friendly_name in-place from current HA state."""
    changed = False
    for mapping in (published or []):
        entity_id = mapping.get("entity_id")
        if not entity_id:
            continue
        new_name = mapping_friendly_name(hass, mapping)
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


def _expected_unique_ids(entry_id: str, published: List[Dict[str, Any]]) -> set[str]:
    expected: set[str] = set()
    for mapping in published:
        if not isinstance(mapping, dict):
            continue
        obj_type = str(mapping.get("object_type") or "")
        inst = _as_int(mapping.get("instance"), -1)
        if inst < 0:
            continue
        if obj_type == "analogValue":
            expected.add(f"{DOMAIN}:{entry_id}:av:{inst}")
        elif obj_type == "binaryValue":
            expected.add(f"{DOMAIN}:{entry_id}:bv:{inst}")
        elif obj_type == "multiStateValue":
            expected.add(f"{DOMAIN}:{entry_id}:msv:{inst}")
    return expected


def _cleanup_orphan_published_entities(
    hass: HomeAssistant, entry: ConfigEntry, published: List[Dict[str, Any]]
) -> int:
    """Delete stale BACnet entities no longer present in published mappings."""
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    expected = _expected_unique_ids(entry.entry_id, published)
    uid_prefix = f"{DOMAIN}:{entry.entry_id}:"
    removed = 0

    for reg_entry in entries:
        unique_id = str(getattr(reg_entry, "unique_id", "") or "")
        entity_id = getattr(reg_entry, "entity_id", None)
        if not unique_id.startswith(uid_prefix):
            continue
        if unique_id in expected:
            continue
        if not entity_id:
            continue
        try:
            registry.async_remove(entity_id)
            removed += 1
        except Exception:
            _LOGGER.debug("Could not remove stale entity %s", entity_id, exc_info=True)

    return removed


def _auto_target_entity_ids(hass: HomeAssistant, options: Dict[str, Any], mode: str) -> set[str]:
    if mode != PUBLISH_MODE_LABELS:
        return set()

    label_ids = set(_as_string_list(options.get(CONF_IMPORT_LABELS)))
    if not label_ids:
        legacy_label = str(options.get(CONF_IMPORT_LABEL) or "").strip()
        if legacy_label:
            label_ids.add(legacy_label)

    if not label_ids:
        return set()

    return entity_ids_for_labels(hass, label_ids)


async def _async_sync_auto_mappings(hass: HomeAssistant, entry_id: str) -> bool:
    entry = _entry_by_id(hass, entry_id)
    if not entry:
        return False

    # Read from merged data/options so first setup works before options are explicitly saved.
    current_options = dict(entry.data or {})
    current_options.update(dict(entry.options or {}))
    had_legacy_ui_keys = any(
        key in current_options for key in ("ui_mode", "show_technical_ids", "label_template")
    )
    options = dict(current_options)
    # Migrate removed UI keys on next save/update.
    options.pop("ui_mode", None)
    options.pop("show_technical_ids", None)
    options.pop("label_template", None)

    had_legacy_mapping_keys = False

    mode = _normalize_publish_mode(options.get(CONF_PUBLISH_MODE, DEFAULT_PUBLISH_MODE))
    if mode != PUBLISH_MODE_LABELS:
        options[CONF_PUBLISH_MODE] = PUBLISH_MODE_LABELS
        mode = PUBLISH_MODE_LABELS
        had_legacy_mapping_keys = True

    label_ids = _as_string_list(options.get(CONF_IMPORT_LABELS))
    if not label_ids:
        legacy_label = str(options.get(CONF_IMPORT_LABEL) or "").strip()
        if legacy_label:
            options[CONF_IMPORT_LABELS] = [legacy_label]
            label_ids = [legacy_label]
            had_legacy_mapping_keys = True
    else:
        # Keep legacy single value aligned for compatibility.
        first_label = str(label_ids[0]).strip()
        if str(options.get(CONF_IMPORT_LABEL) or "").strip() != first_label:
            options[CONF_IMPORT_LABEL] = first_label
            had_legacy_mapping_keys = True

    published: List[Dict[str, Any]] = list(options.get("published", []))
    counters: Dict[str, int] = dict(options.get("counters", {}))
    original_published = list(published)
    original_counters = dict(counters)

    _ensure_counter_floor(counters, published)
    targets = _auto_target_entity_ids(hass, options, mode)

    kept: List[Dict[str, Any]] = []
    existing_mapping_keys: set[str] = set()
    removed_count = 0

    for mapping in published:
        if not isinstance(mapping, dict):
            continue
        current = dict(mapping)
        if "writable" in current:
            current.pop("writable", None)
            had_legacy_mapping_keys = True
        if (
            str(current.get("source_attr") or "").strip().lower() == "temperature"
            and str(current.get("write_action") or "").strip() == "climate_temperature"
        ):
            current["source_attr"] = "set_temperature"
            current["read_attr"] = "temperature"
            if current.get("cov_increment") is None:
                current["cov_increment"] = 0.1
            had_legacy_mapping_keys = True

        entity_id = str(current.get("entity_id") or "")
        if not entity_id:
            removed_count += 1
            continue
        if not entity_exists(hass, entity_id):
            removed_count += 1
            continue

        key = mapping_key(current)
        if key in existing_mapping_keys:
            removed_count += 1
            continue

        is_auto = bool(current.get("auto", False))
        if not is_auto:
            # Manual mappings are no longer part of the simplified labels-only model.
            removed_count += 1
            continue

        auto_mode = str(current.get("auto_mode") or "")
        if auto_mode and auto_mode != mode:
            removed_count += 1
            continue
        if entity_id not in targets:
            removed_count += 1
            continue

        candidates = entity_mapping_candidates(hass, entity_id)
        candidate_by_key = {mapping_key(spec): spec for spec in candidates}
        candidate = candidate_by_key.get(key)
        if not candidate:
            removed_count += 1
            continue

        # Keep stable instance unless object type changed.
        old_object_type = str(current.get("object_type") or "")
        new_object_type = str(candidate.get("object_type") or "")
        if old_object_type != new_object_type:
            current["object_type"] = new_object_type
            current["instance"] = _next_instance(new_object_type, counters, kept)
        else:
            current["object_type"] = new_object_type
        current["units"] = candidate.get("units")
        current["friendly_name"] = str(
            candidate.get("friendly_name") or mapping_friendly_name(hass, current)
        )
        if candidate.get("source_attr"):
            current["source_attr"] = candidate.get("source_attr")
        else:
            current.pop("source_attr", None)
        if candidate.get("read_attr"):
            current["read_attr"] = candidate.get("read_attr")
        else:
            current.pop("read_attr", None)
        if candidate.get("write_action"):
            current["write_action"] = candidate.get("write_action")
        else:
            current.pop("write_action", None)
        if candidate.get("mv_states"):
            current["mv_states"] = list(candidate.get("mv_states") or [])
        else:
            current.pop("mv_states", None)
        if candidate.get("hvac_on_mode"):
            current["hvac_on_mode"] = candidate.get("hvac_on_mode")
        else:
            current.pop("hvac_on_mode", None)
        if candidate.get("hvac_off_mode"):
            current["hvac_off_mode"] = candidate.get("hvac_off_mode")
        else:
            current.pop("hvac_off_mode", None)
        if candidate.get("cov_increment") is not None:
            current["cov_increment"] = float(candidate.get("cov_increment"))
        else:
            current.pop("cov_increment", None)

        kept.append(current)
        existing_mapping_keys.add(key)

    added_count = 0
    for entity_id in sorted(targets):
        if not entity_exists(hass, entity_id):
            continue
        for candidate in entity_mapping_candidates(hass, entity_id):
            key = mapping_key(candidate)
            if key in existing_mapping_keys:
                continue

            object_type = str(candidate.get("object_type") or "").strip()
            if not object_type:
                continue

            instance = _next_instance(object_type, counters, kept)
            new_map = {
                "entity_id": entity_id,
                "object_type": object_type,
                "instance": instance,
                "units": candidate.get("units"),
                "friendly_name": str(
                    candidate.get("friendly_name") or mapping_friendly_name(hass, candidate)
                ),
                "auto": True,
                "auto_mode": mode,
            }
            if candidate.get("source_attr"):
                new_map["source_attr"] = candidate.get("source_attr")
            if candidate.get("read_attr"):
                new_map["read_attr"] = candidate.get("read_attr")
            if candidate.get("write_action"):
                new_map["write_action"] = candidate.get("write_action")
            if candidate.get("mv_states"):
                new_map["mv_states"] = list(candidate.get("mv_states") or [])
            if candidate.get("hvac_on_mode"):
                new_map["hvac_on_mode"] = candidate.get("hvac_on_mode")
            if candidate.get("hvac_off_mode"):
                new_map["hvac_off_mode"] = candidate.get("hvac_off_mode")
            if candidate.get("cov_increment") is not None:
                new_map["cov_increment"] = float(candidate.get("cov_increment"))

            kept.append(new_map)
            existing_mapping_keys.add(key)
            added_count += 1

    changed = (
        (kept != original_published)
        or (counters != original_counters)
        or had_legacy_ui_keys
        or had_legacy_mapping_keys
    )
    if not changed:
        return False

    # Persist merged settings in options so subsequent sync cycles rely on one source of truth.
    new_options = dict(current_options)
    new_options.pop("ui_mode", None)
    new_options.pop("show_technical_ids", None)
    new_options.pop("label_template", None)
    new_options["published"] = kept
    new_options["counters"] = counters
    hass.config_entries.async_update_entry(entry, options=new_options)
    _LOGGER.info(
        "Auto mapping sync updated entry %s (mode=%s, added=%d, removed=%d)",
        entry_id,
        mode,
        added_count,
        removed_count,
    )
    return True


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
    removed_stale_entities = _cleanup_orphan_published_entities(hass, entry, published)
    if removed_stale_entities:
        _LOGGER.info(
            "Removed %d stale BACnet entities for entry %s",
            removed_stale_entities,
            entry.entry_id,
        )

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
            hass.config_entries.async_update_entry(entry, options=new_options)
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
                    hass.config_entries.async_update_entry(entry, options=new_options)
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
    reload_locks: dict[str, asyncio.Lock] = data[KEY_RELOAD_LOCKS]
    reload_fp: dict[str, str] = data[KEY_LAST_RELOAD_FP]

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
    reload_locks.pop(entry.entry_id, None)
    reload_fp.pop(entry.entry_id, None)

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
    data = _ensure_domain(hass)
    reload_locks: dict[str, asyncio.Lock] = data[KEY_RELOAD_LOCKS]
    reload_fp: dict[str, str] = data[KEY_LAST_RELOAD_FP]
    reload_lock = reload_locks.setdefault(entry.entry_id, asyncio.Lock())

    async with reload_lock:
        if hass.data.get(DOMAIN, {}).pop(KEY_SUPPRESS_RELOAD, False):
            _LOGGER.debug("Reload suppressed (friendly names synchronized).")
            return

        try:
            changed = await _async_sync_auto_mappings(hass, entry.entry_id)
            if changed:
                # async_update_entry triggered another listener call that will do the reload.
                _LOGGER.debug(
                    "Auto sync adjusted options for %s (Entry %s); waiting for follow-up reload.",
                    DOMAIN,
                    entry.entry_id,
                )
                return
        except Exception:
            _LOGGER.debug("Auto sync before reload failed for entry %s", entry.entry_id, exc_info=True)

        current_entry = _entry_by_id(hass, entry.entry_id)
        if not current_entry:
            return

        options_fp = _options_fingerprint(dict(current_entry.options or {}))
        if reload_fp.get(entry.entry_id) == options_fp:
            _LOGGER.debug(
                "Skipping duplicate reload for %s (Entry %s): options unchanged.",
                DOMAIN,
                entry.entry_id,
            )
            return

        try:
            current_published: List[Dict[str, Any]] = list(
                (current_entry.options or {}).get("published", [])
            )
            removed = _cleanup_orphan_published_entities(hass, current_entry, current_published)
            if removed:
                _LOGGER.info(
                    "Removed %d stale BACnet entities for entry %s before reload",
                    removed,
                    entry.entry_id,
                )
        except Exception:
            _LOGGER.debug(
                "Orphan cleanup before reload failed for entry %s", entry.entry_id, exc_info=True
            )

        _LOGGER.debug("Options update for %s (Entry %s) - starting reload.", DOMAIN, entry.entry_id)
        await hass.config_entries.async_reload(entry.entry_id)
        reload_fp[entry.entry_id] = options_fp
