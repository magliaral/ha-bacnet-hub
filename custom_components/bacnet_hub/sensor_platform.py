from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_ADDRESS, CONF_INSTANCE, DOMAIN, client_iam_signal, hub_display_name
from .sensor_entities import (
    BacnetClientDetailSensor,
    BacnetClientPointSensor,
    BacnetHubDetailSensor,
    BacnetPublishedSensor,
)
from .sensor_helpers import (
    CLIENT_DIAGNOSTIC_FIELDS,
    CLIENT_DISCOVERY_TIMEOUT_SECONDS,
    CLIENT_REDISCOVERY_INTERVAL,
    CLIENT_SCAN_INTERVAL,
    HUB_DIAGNOSTIC_FIELDS,
    HUB_DIAGNOSTIC_SCAN_INTERVAL,
    NETWORK_DIAGNOSTIC_KEYS,
    _client_cache_get,
    _client_cov_signal,
    _client_diag_signal,
    _client_id,
    _client_points_get,
    _client_points_set,
    _client_points_signal,
    _hub_diag_signal,
    _safe_text,
    _supported_point_type,
    _to_int,
)
from .sensor_runtime import (
    _discover_remote_clients,
    _read_client_object_list,
    _read_client_point_payload,
    _refresh_client_cache,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    data = hass.data[DOMAIN]
    published: List[Dict[str, Any]] = data.get("published", {}).get(entry.entry_id, []) or []
    merged = {**(entry.data or {}), **(entry.options or {})}
    hub_instance = merged.get(CONF_INSTANCE, 0)
    hub_address = merged.get(CONF_ADDRESS, "")
    hub_name = hub_display_name(hub_instance)

    hub_entities: List[SensorEntity] = []
    for key, label in HUB_DIAGNOSTIC_FIELDS:
        hub_entities.append(
            BacnetHubDetailSensor(
                hass=hass,
                entry_id=entry.entry_id,
                merged=merged,
                key=key,
                label=label,
            )
        )
    if hub_entities:
        async_add_entities(hub_entities)

    server = data.get("servers", {}).get(entry.entry_id)
    known_client_instances: set[int] = set()
    client_targets: dict[str, tuple[int, str]] = {}
    client_network_port_hints: dict[str, int] = {}
    client_added_field_keys: dict[str, set[tuple[str, str]]] = {}
    client_added_point_keys: dict[str, set[str]] = {}

    def _client_entities(
        client_instance: int,
        client_address: str,
        include_network: bool = False,
    ) -> list[SensorEntity]:
        client_id = _client_id(int(client_instance))
        prev_instance, prev_address = client_targets.get(
            client_id,
            (int(client_instance), str(client_address)),
        )
        merged_address = str(client_address or "").strip() or str(prev_address or "").strip()
        client_targets[client_id] = (int(prev_instance), merged_address)
        client_network_port_hints.setdefault(client_id, 1)
        added_keys = client_added_field_keys.setdefault(client_id, set())

        client_entities: list[SensorEntity] = []
        for key, label in CLIENT_DIAGNOSTIC_FIELDS:
            source = "network" if key in NETWORK_DIAGNOSTIC_KEYS else "device"
            if source == "network" and not include_network:
                continue
            field_key = (source, key)
            if field_key in added_keys:
                continue
            client_entities.append(
                BacnetClientDetailSensor(
                    hass=hass,
                    entry_id=entry.entry_id,
                    client_id=client_id,
                    client_instance=client_instance,
                    key=key,
                    label=label,
                    source=source,
                )
            )
            added_keys.add(field_key)
        return client_entities

    def _client_point_entities(
        client_instance: int,
        client_id: str,
        point_cache: dict[str, dict[str, Any]],
    ) -> list[SensorEntity]:
        added = client_added_point_keys.setdefault(client_id, set())
        entities_to_add: list[SensorEntity] = []
        for point_key in sorted(point_cache):
            if point_key in added:
                continue
            entities_to_add.append(
                BacnetClientPointSensor(
                    hass=hass,
                    entry_id=entry.entry_id,
                    client_id=client_id,
                    client_instance=int(client_instance),
                    point_key=point_key,
                )
            )
            added.add(point_key)
        return entities_to_add

    async def _import_client_points(
        client_instance: int,
        client_address: str,
        only_new: bool = True,
    ) -> list[SensorEntity]:
        live_server = hass.data.get(DOMAIN, {}).get("servers", {}).get(entry.entry_id)
        app = getattr(live_server, "app", None) if live_server is not None else None
        if app is None:
            return []

        instance = int(client_instance)
        client_id = _client_id(instance)
        address = str(client_address or "").strip()
        if not address:
            _, fallback_address = client_targets.get(client_id, (instance, ""))
            address = str(fallback_address or "").strip()
        if not address:
            return []

        point_cache = _client_points_get(hass, entry.entry_id, client_id)
        existing_point_keys = set(point_cache.keys()) if only_new else set()

        try:
            object_list = await _read_client_object_list(app, address, instance)
        except asyncio.CancelledError:
            raise
        except BaseException:
            _LOGGER.debug(
                "Client object-list read failed for %s (%s)",
                instance,
                address,
                exc_info=True,
            )
            return []
        if not object_list:
            _LOGGER.debug("No client object-list entries for %s (%s)", instance, address)
            return []

        payload: dict[str, dict[str, Any]] = {}
        per_type_counts: dict[str, int] = {}
        read_candidates = 0
        for object_type, object_instance in object_list:
            supported = _supported_point_type(object_type)
            if not supported:
                continue
            type_slug, canonical_type = supported
            point_key = f"{type_slug}_{int(object_instance)}"
            if only_new and point_key in existing_point_keys:
                continue
            read_candidates += 1
            try:
                point = await _read_client_point_payload(
                    app,
                    address,
                    instance,
                    canonical_type,
                    int(object_instance),
                )
            except asyncio.CancelledError:
                raise
            except BaseException:
                continue
            point_key = str(point.get("point_key") or point_key).strip()
            if not point_key:
                continue
            payload[point_key] = point
            type_slug = str(point.get("type_slug") or "").strip() or "unknown"
            per_type_counts[type_slug] = int(per_type_counts.get(type_slug, 0)) + 1

        if not payload:
            if read_candidates > 0:
                _LOGGER.debug("Client point import yielded no payload for %s (%s)", instance, address)
            return []

        _LOGGER.debug(
            "Imported %d client points for %s (%s): %s",
            len(payload),
            instance,
            address,
            per_type_counts,
        )

        _client_points_set(hass, entry.entry_id, client_id, payload)
        async_dispatcher_send(hass, _client_points_signal(entry.entry_id, client_id))
        return _client_point_entities(instance, client_id, _client_points_get(hass, entry.entry_id, client_id))

    def _update_client_point_addresses(client_id: str, client_address: str) -> bool:
        address = str(client_address or "").strip()
        if not address:
            return False
        point_cache = _client_points_get(hass, entry.entry_id, client_id)
        if not point_cache:
            return False

        updated_payload: dict[str, dict[str, Any]] = {}
        for point_key, raw_point in point_cache.items():
            point = dict(raw_point or {})
            if str(point.get("client_address") or "").strip() == address:
                continue
            point["client_address"] = address
            updated_payload[str(point_key)] = point

        if not updated_payload:
            return False
        _client_points_set(hass, entry.entry_id, client_id, updated_payload)
        async_dispatcher_send(hass, _client_points_signal(entry.entry_id, client_id))
        return True

    async def _process_client_iam(client_instance: int, client_address: str) -> None:
        instance = int(client_instance)
        address = str(client_address or "").strip()
        if not address:
            return

        new_entities: list[SensorEntity] = []
        if instance not in known_client_instances:
            known_client_instances.add(instance)
            new_entities.extend(_client_entities(instance, address, include_network=False))
        else:
            _client_entities(instance, address, include_network=False)

        client_id = _client_id(instance)
        await _refresh_client_cache(
            hass=hass,
            entry_id=entry.entry_id,
            client_id=client_id,
            client_instance=instance,
            client_address=address,
            network_port_hints=client_network_port_hints,
            client_targets=client_targets,
            force=True,
        )
        _, latest_address = client_targets.get(client_id, (instance, address))
        cache = _client_cache_get(hass, entry.entry_id, client_id)
        if bool(cache.get("has_network_object")):
            new_entities.extend(_client_entities(instance, latest_address, include_network=True))
        try:
            new_entities.extend(
                await _import_client_points(
                    instance,
                    latest_address,
                    only_new=True,
                )
            )
        except asyncio.CancelledError:
            raise
        except BaseException:
            _LOGGER.debug(
                "Client point import failed for %s (%s)",
                instance,
                latest_address,
                exc_info=True,
            )

        _update_client_point_addresses(client_id, latest_address)
        if new_entities:
            async_add_entities(new_entities)
        async_dispatcher_send(hass, _client_cov_signal(entry.entry_id, client_id))

    @callback
    def _on_client_iam(payload: dict[str, Any] | None = None) -> None:
        data = payload or {}
        instance = _to_int(data.get("instance"))
        address = _safe_text(data.get("address"))
        if instance is None or not address:
            return
        hass.add_job(_process_client_iam(int(instance), str(address)))

    unsub_iam = async_dispatcher_connect(hass, client_iam_signal(entry.entry_id), _on_client_iam)
    entry.async_on_unload(unsub_iam)

    try:
        discovered_clients = await asyncio.wait_for(
            _discover_remote_clients(server),
            timeout=CLIENT_DISCOVERY_TIMEOUT_SECONDS + 1.0,
        )
    except Exception:
        discovered_clients = []

    discovered_by_instance: dict[int, str] = {}
    for client_instance, client_address in discovered_clients:
        discovered_by_instance[int(client_instance)] = str(client_address)

    client_initial_entities: list[SensorEntity] = []
    for client_instance, client_address in discovered_by_instance.items():
        known_client_instances.add(int(client_instance))
        for client_entity in _client_entities(client_instance, client_address, include_network=False):
            client_initial_entities.append(client_entity)

    async def _refresh_known_clients() -> None:
        new_entities: list[SensorEntity] = []
        for client_id, (client_instance, client_address) in list(client_targets.items()):
            try:
                await _refresh_client_cache(
                    hass=hass,
                    entry_id=entry.entry_id,
                    client_id=client_id,
                    client_instance=client_instance,
                    client_address=client_address,
                    network_port_hints=client_network_port_hints,
                    client_targets=client_targets,
                )
            except asyncio.CancelledError:
                raise
            except BaseException:
                _LOGGER.debug(
                    "Client diagnostic refresh failed for %s (%s)",
                    client_instance,
                    client_address,
                    exc_info=True,
                )
                continue
            _, latest_address = client_targets.get(client_id, (client_instance, client_address))
            changed_address = _update_client_point_addresses(client_id, latest_address)
            if changed_address:
                async_dispatcher_send(hass, _client_cov_signal(entry.entry_id, client_id))
            cache = _client_cache_get(hass, entry.entry_id, client_id)
            if bool(cache.get("has_network_object")):
                new_entities.extend(
                    _client_entities(
                        client_instance,
                        latest_address,
                        include_network=True,
                    )
                )
        if new_entities:
            async_add_entities(new_entities)

    async def _scan_and_add_new_clients() -> None:
        live_server = hass.data.get(DOMAIN, {}).get("servers", {}).get(entry.entry_id)
        new_entities: list[SensorEntity] = []
        try:
            scan_clients = await asyncio.wait_for(
                _discover_remote_clients(live_server),
                timeout=CLIENT_DISCOVERY_TIMEOUT_SECONDS + 1.0,
            )
        except Exception:
            scan_clients = []

        discovered_map: dict[int, str] = {}
        for client_instance, client_address in scan_clients:
            discovered_map[int(client_instance)] = str(client_address)

        for client_instance, client_address in discovered_map.items():
            if int(client_instance) not in known_client_instances:
                known_client_instances.add(int(client_instance))
                new_entities.extend(
                    _client_entities(client_instance, client_address, include_network=False)
                )
                try:
                    new_entities.extend(
                        await _import_client_points(
                            client_instance,
                            client_address,
                            only_new=True,
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    _LOGGER.debug(
                        "Client point import failed for %s (%s)",
                        client_instance,
                        client_address,
                        exc_info=True,
                    )
            else:
                _client_entities(client_instance, client_address, include_network=False)
                client_id = _client_id(int(client_instance))
                changed_address = _update_client_point_addresses(client_id, client_address)
                if changed_address:
                    async_dispatcher_send(hass, _client_cov_signal(entry.entry_id, client_id))
                cache = _client_cache_get(hass, entry.entry_id, client_id)
                if bool(cache.get("has_network_object")):
                    new_entities.extend(
                        _client_entities(
                            client_instance,
                            client_address,
                            include_network=True,
                        )
                    )
                try:
                    new_entities.extend(
                        await _import_client_points(
                            client_instance,
                            client_address,
                            only_new=True,
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    _LOGGER.debug(
                        "Client point import failed for %s (%s)",
                        client_instance,
                        client_address,
                        exc_info=True,
                    )
        if new_entities:
            async_add_entities(new_entities)

    async def _refresh_all_clients() -> None:
        if not client_targets:
            await _scan_and_add_new_clients()
        await _refresh_known_clients()

    def _schedule_client_refresh(_now) -> None:
        hass.add_job(_refresh_all_clients())

    def _schedule_rescan(_now) -> None:
        hass.add_job(_scan_and_add_new_clients())

    if client_initial_entities:
        async_add_entities(client_initial_entities)

    published_entities: list[SensorEntity] = []
    for m in published:
        if (m or {}).get("object_type") != "analogValue":
            continue
        ent_id = m.get("entity_id")
        if not ent_id:
            continue
        instance = int(m.get("instance", 0))
        source_attr = m.get("source_attr")
        read_attr = m.get("read_attr")
        units = m.get("units")
        friendly = m.get("friendly_name")
        name = f"(AV-{instance}) {friendly}"
        published_entities.append(
            BacnetPublishedSensor(
                hass=hass,
                entry_id=entry.entry_id,
                hub_instance=hub_instance,
                hub_address=hub_address,
                hub_name=hub_name,
                source_entity_id=ent_id,
                instance=instance,
                name=name,
                source_attr=source_attr,
                read_attr=read_attr,
                configured_unit=units,
            )
        )
    if published_entities:
        async_add_entities(published_entities)

    def _schedule_hub_diag_refresh(_now) -> None:
        hass.add_job(async_dispatcher_send, hass, _hub_diag_signal(entry.entry_id))

    unsub_hub_diag = async_track_time_interval(
        hass,
        _schedule_hub_diag_refresh,
        HUB_DIAGNOSTIC_SCAN_INTERVAL,
    )
    entry.async_on_unload(unsub_hub_diag)
    async_dispatcher_send(hass, _hub_diag_signal(entry.entry_id))

    async def _initial_client_refresh() -> None:
        await asyncio.sleep(5)
        try:
            await _refresh_all_clients()
        except asyncio.CancelledError:
            raise
        except BaseException:
            _LOGGER.debug("Initial client refresh failed", exc_info=True)

    hass.async_create_task(_initial_client_refresh())

    unsub_refresh = async_track_time_interval(hass, _schedule_client_refresh, CLIENT_SCAN_INTERVAL)
    unsub_rescan = async_track_time_interval(hass, _schedule_rescan, CLIENT_REDISCOVERY_INTERVAL)
    entry.async_on_unload(unsub_refresh)
    entry.async_on_unload(unsub_rescan)
