from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import CONF_INSTANCE, DOMAIN, client_display_name
from .sensor_helpers import (
    CLIENT_DISCOVERY_TIMEOUT_SECONDS,
    CLIENT_OBJECTLIST_READ_TIMEOUT_SECONDS,
    CLIENT_OBJECTLIST_SCAN_LIMIT,
    CLIENT_POINT_REFRESH_TIMEOUT_SECONDS,
    CLIENT_POINT_SCAN_LIMIT,
    CLIENT_READ_TIMEOUT_SECONDS,
    CLIENT_REFRESH_MIN_SECONDS,
    _bacnet_mac_from_ip_port,
    _client_cache_get,
    _client_cache_set,
    _client_diag_signal,
    _client_lock_get,
    _mac_colon,
    _mac_hex,
    _normalize_bacnet_unit,
    _normalize_system_status,
    _object_identifier_compact,
    _object_identifier_instance,
    _object_identifier_instance_text,
    _object_identifier_text,
    _object_instance,
    _parse_object_list_item,
    _point_is_writable,
    _safe_text,
    _supported_point_type,
    _to_int,
    _to_ipv4_text,
)

_LOGGER = logging.getLogger(__name__)


def _merge_non_none(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = dict(previous or {})
    for key, value in dict(current or {}).items():
        if value is not None:
            merged[key] = value
        elif key not in merged:
            merged[key] = None
    return merged


async def _read_remote_property(
    app: Any,
    address: str,
    objid: str,
    prop: str,
    array_index: int | None = None,
    timeout: float = CLIENT_READ_TIMEOUT_SECONDS,
) -> Any:
    return await asyncio.wait_for(
        app.read_property(address, objid, prop, array_index=array_index),
        timeout=timeout,
    )


async def _read_remote_properties(
    app: Any,
    address: str,
    objid: str,
    properties: list[str],
    timeout: float = CLIENT_POINT_REFRESH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    unique_props: list[str] = []
    seen: set[str] = set()
    for prop in properties:
        key = str(prop).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique_props.append(key)

    rpm = getattr(app, "read_property_multiple", None)
    if callable(rpm):
        for args in (
            (address, objid, unique_props),
            (address, [(objid, unique_props)]),
        ):
            try:
                raw = await asyncio.wait_for(rpm(*args), timeout=timeout)
                if isinstance(raw, dict):
                    for prop in unique_props:
                        if prop in raw:
                            result[prop] = raw.get(prop)
                    if result:
                        return result
            except asyncio.CancelledError:
                raise
            except BaseException:
                continue

    for prop in unique_props:
        try:
            result[prop] = await _read_remote_property(
                app,
                address,
                objid,
                prop,
                timeout=CLIENT_READ_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except BaseException:
            result[prop] = None
    return result


async def _read_remote_property_any_objid(
    app: Any,
    address: str,
    objids: list[str],
    prop: str,
) -> Any:
    last_err: BaseException | None = None
    for objid in objids:
        try:
            return await _read_remote_property(app, address, objid, prop)
        except asyncio.CancelledError:
            raise
        except BaseException as err:
            last_err = err
            continue
    if last_err:
        raise last_err
    return None


async def _write_client_point_present_value(
    app: Any,
    address: str,
    object_type: str,
    object_instance: int,
    value: Any,
    *,
    priority: int | None = None,
    timeout: float = CLIENT_READ_TIMEOUT_SECONDS,
) -> Any:
    objid = f"{str(object_type)}{','}{int(object_instance)}"
    write_fn = getattr(app, "write_property", None)
    if not callable(write_fn):
        raise RuntimeError("write_property is not available on BACnet app")

    attempts: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    kwargs_with_priority = {"priority": int(priority)} if priority is not None else {}
    attempts.append(((address, objid, "presentValue", value), kwargs_with_priority))
    if priority is not None:
        attempts.append(((address, objid, "presentValue", value, int(priority)), {}))
        attempts.append(((address, objid, "presentValue", value, None, int(priority)), {}))

    last_err: BaseException | None = None
    for args, kwargs in attempts:
        try:
            return await asyncio.wait_for(write_fn(*args, **kwargs), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except BaseException as err:
            last_err = err
            continue
    if last_err is not None:
        raise last_err
    raise RuntimeError("Unable to write presentValue")


async def _read_client_object_list(
    app: Any,
    address: str,
    client_instance: int,
) -> list[tuple[str, int]]:
    device_obj = f"device,{int(client_instance)}"
    object_list: list[tuple[str, int]] = []
    try:
        list_len = _to_int(
            await _read_remote_property(
                app,
                address,
                device_obj,
                "objectList",
                array_index=0,
                timeout=CLIENT_OBJECTLIST_READ_TIMEOUT_SECONDS,
            )
        )
    except asyncio.CancelledError:
        raise
    except BaseException:
        list_len = None
    if not list_len or list_len <= 0:
        return object_list

    for idx in range(1, min(list_len, CLIENT_POINT_SCAN_LIMIT) + 1):
        try:
            item = await _read_remote_property(
                app,
                address,
                device_obj,
                "objectList",
                array_index=idx,
                timeout=CLIENT_OBJECTLIST_READ_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except BaseException:
            continue
        parsed = _parse_object_list_item(item)
        if parsed is None:
            continue
        item_type, inst = parsed
        type_info = _supported_point_type(item_type)
        if not type_info or inst is None:
            continue
        canonical_type = type_info[1]
        object_list.append((canonical_type, int(inst)))
    return object_list


async def _discover_remote_clients(server: Any) -> list[tuple[int, str]]:
    app = getattr(server, "app", None)
    if app is None:
        return []

    try:
        i_ams = await app.who_is(timeout=CLIENT_DISCOVERY_TIMEOUT_SECONDS)
    except Exception:
        return []

    local_instance = _to_int(getattr(server, "instance", None))
    clients: dict[tuple[int, str], tuple[int, str]] = {}
    for i_am in i_ams or []:
        try:
            dev_ident = getattr(i_am, "iAmDeviceIdentifier", None)
            instance = _to_int(dev_ident[1] if isinstance(dev_ident, tuple) and len(dev_ident) == 2 else None)
            source = _safe_text(getattr(i_am, "pduSource", None))
        except Exception:
            continue
        if instance is None or not source:
            continue
        if local_instance is not None and instance == local_instance:
            continue
        key = (instance, source)
        clients[key] = key

    if clients:
        _LOGGER.debug("Discovered BACnet clients: %s", sorted(clients.values()))
    return sorted(clients.values(), key=lambda item: (item[0], item[1]))


async def _resolve_client_address(
    app: Any,
    server: Any,
    client_instance: int,
    fallback_address: str | None = None,
) -> str:
    instance = int(client_instance)
    address_text = str(fallback_address or "").strip()

    # I-Am / explicit scan source is authoritative for reconnects.
    # Do not override it with potentially stale device-info cache entries.
    if address_text:
        return address_text

    try:
        device_info = await app.get_device_info(instance)
        device_address = getattr(device_info, "device_address", None) if device_info else None
        if device_address:
            addr = str(device_address).strip()
            if addr:
                return addr
    except Exception:
        pass

    try:
        for found_instance, found_address in await _discover_remote_clients(server):
            if int(found_instance) == instance and str(found_address or "").strip():
                return str(found_address).strip()
    except Exception:
        pass

    return address_text


async def _read_client_runtime(
    app: Any,
    client_instance: int,
    client_address: str,
    network_port_instance_hint: int = 1,
) -> dict[str, Any]:
    instance = int(client_instance)
    address = str(client_address)
    network_port_instance = max(1, int(network_port_instance_hint or 1))
    device_obj = f"device,{instance}"

    async def read_device(prop: str) -> Any:
        try:
            return await _read_remote_property(app, address, device_obj, prop)
        except asyncio.CancelledError:
            raise
        except BaseException:
            return None

    async def read_network(prop: str, inst: int) -> Any:
        try:
            return await _read_remote_property_any_objid(
                app,
                address,
                [f"network-port,{inst}", f"networkPort,{inst}"],
                prop,
            )
        except asyncio.CancelledError:
            raise
        except BaseException:
            return None

    probe_object_name = _safe_text(await read_device("objectName"))
    if probe_object_name is None:
        probe_object_identifier = _object_identifier_text(await read_device("objectIdentifier"))
        if probe_object_identifier is None:
            return {
                "online": False,
                "has_device_object": False,
                "has_network_object": False,
                "device": {
                    "client_instance": instance,
                    "client_address": address,
                    "object_identifier": str(instance),
                },
                "network": {
                    "client_instance": instance,
                    "client_address": address,
                },
                "network_port_instance": network_port_instance,
                "name": f"BACnet Client {instance}",
            }

    object_name = probe_object_name or _safe_text(await read_device("objectName"))
    description = _safe_text(await read_device("description"))
    model_name = _safe_text(await read_device("modelName"))
    vendor_name = _safe_text(await read_device("vendorName"))
    vendor_identifier = _to_int(await read_device("vendorIdentifier"))
    firmware_revision = _safe_text(await read_device("firmwareRevision"))
    hardware_revision = _safe_text(await read_device("hardwareRevision"))
    application_software_version = _safe_text(await read_device("applicationSoftwareVersion"))
    serial_number = _safe_text(await read_device("serialNumber"))
    object_identifier = _object_identifier_instance_text(
        await read_device("objectIdentifier"),
        fallback=instance,
    )
    raw_system_status = await read_device("systemStatus")
    _, system_status = _normalize_system_status(raw_system_status)

    net_object_identifier = await read_network("objectIdentifier", network_port_instance)
    if net_object_identifier is None and network_port_instance == 1:
        try:
            list_len = _to_int(
                await _read_remote_property(
                    app,
                    address,
                    device_obj,
                    "objectList",
                    array_index=0,
                    timeout=CLIENT_OBJECTLIST_READ_TIMEOUT_SECONDS,
                )
            )
            if list_len and list_len > 0:
                for idx in range(1, min(list_len, CLIENT_OBJECTLIST_SCAN_LIMIT) + 1):
                    oid = await _read_remote_property(
                        app,
                        address,
                        device_obj,
                        "objectList",
                        array_index=idx,
                        timeout=CLIENT_OBJECTLIST_READ_TIMEOUT_SECONDS,
                    )
                    inst = _object_identifier_instance(oid, "network-port")
                    if inst is not None:
                        network_port_instance = int(inst)
                        break
        except asyncio.CancelledError:
            raise
        except BaseException:
            pass
        net_object_identifier = await read_network("objectIdentifier", network_port_instance)
    has_network_object = net_object_identifier is not None

    ip_address_raw = await read_network("ipAddress", network_port_instance)
    ip_subnet_mask_raw = await read_network("ipSubnetMask", network_port_instance)
    udp_port = _to_int(await read_network("bacnetIPUDPPort", network_port_instance))
    mac_raw = _mac_hex(await read_network("macAddress", network_port_instance))

    ip_address = _to_ipv4_text(ip_address_raw) or _safe_text(ip_address_raw)
    ip_subnet_mask = _to_ipv4_text(ip_subnet_mask_raw) or _safe_text(ip_subnet_mask_raw)
    if not mac_raw:
        mac_raw = _bacnet_mac_from_ip_port(ip_address, udp_port)
    mac_colon = _mac_colon(mac_raw)

    device_data = {
        "client_instance": instance,
        "client_address": address,
        "description": description,
        "firmware_revision": firmware_revision,
        "hardware_revision": hardware_revision,
        "application_software_version": application_software_version,
        "model_name": model_name,
        "object_identifier": object_identifier or str(instance),
        "object_name": object_name,
        "system_status": system_status,
        "vendor_identifier": vendor_identifier,
        "vendor_name": vendor_name,
        "serial_number": serial_number,
    }
    network_data = {
        "client_instance": instance,
        "client_address": address,
        "ip_address": ip_address,
        "ip_subnet_mask": ip_subnet_mask,
        "mac_address_raw": mac_colon,
    }
    online = any(
        value is not None
        for value in (
            object_name,
            model_name,
            vendor_name,
            firmware_revision,
            ip_address,
            mac_raw,
        )
    )
    return {
        "online": online,
        "has_device_object": True,
        "has_network_object": has_network_object,
        "device": device_data,
        "network": network_data,
        "network_port_instance": network_port_instance,
        "name": client_display_name(instance, object_name),
    }


async def _read_client_point_payload(
    app: Any,
    address: str,
    client_instance: int,
    object_type: str,
    object_instance: int,
) -> dict[str, Any]:
    supported = _supported_point_type(object_type)
    if not supported:
        return {}
    type_slug, canonical_type = supported
    objid = f"{canonical_type},{int(object_instance)}"
    props = [
        "objectIdentifier",
        "objectName",
        "description",
        "presentValue",
        "units",
        "statusFlags",
        "outOfService",
        "reliability",
        "stateText",
        "numberOfStates",
        "activeText",
        "inactiveText",
        "priorityArray",
    ]
    values = await _read_remote_properties(app, address, objid, props)
    state_text_value = values.get("stateText")
    state_text: list[str] | None = None
    if isinstance(state_text_value, (list, tuple)):
        state_text = [str(item) for item in state_text_value]
    elif state_text_value is not None and not isinstance(state_text_value, str):
        try:
            state_text = [str(item) for item in list(state_text_value)]
        except Exception:
            state_text = None
    object_identifier_raw = values.get("objectIdentifier")
    object_identifier = _object_identifier_compact(
        object_identifier_raw,
        canonical_type,
        int(object_instance),
    )
    point_key = f"{type_slug}_{int(object_instance)}"
    has_priority_array = values.get("priorityArray") is not None
    writable_from_ha = _point_is_writable(
        {
            "type_slug": type_slug,
            "has_priority_array": has_priority_array,
        }
    )
    return {
        "point_key": point_key,
        "client_instance": int(client_instance),
        "client_address": str(address),
        "object_type": canonical_type,
        "type_slug": type_slug,
        "object_instance": int(object_instance),
        "object_identifier": object_identifier,
        "object_name": _safe_text(values.get("objectName")) or f"{canonical_type} {int(object_instance)}",
        "description": _safe_text(values.get("description")),
        "present_value": values.get("presentValue"),
        "unit": _normalize_bacnet_unit(values.get("units")),
        "status_flags": _safe_text(values.get("statusFlags")),
        "out_of_service": values.get("outOfService"),
        "reliability": _safe_text(values.get("reliability")),
        "state_text": state_text,
        "number_of_states": _to_int(values.get("numberOfStates")),
        "active_text": _safe_text(values.get("activeText")),
        "inactive_text": _safe_text(values.get("inactiveText")),
        "has_priority_array": has_priority_array,
        "writable_from_ha": writable_from_ha,
    }


def _hub_diagnostics(server: Any, merged: Dict[str, Any]) -> Dict[str, Any]:
    device_obj = getattr(server, "device_object", None) if server is not None else None
    network_obj = getattr(server, "network_port_object", None) if server is not None else None

    instance = _to_int(getattr(server, "instance", None)) if server is not None else _to_int(merged.get(CONF_INSTANCE))

    object_name = _safe_text(getattr(device_obj, "objectName", None))
    description = _safe_text(getattr(device_obj, "description", None))
    model_name = _safe_text(getattr(device_obj, "modelName", None))
    vendor_name = _safe_text(getattr(device_obj, "vendorName", None))
    vendor_identifier = _to_int(getattr(device_obj, "vendorIdentifier", None))

    firmware_revision = _safe_text(getattr(device_obj, "firmwareRevision", None))
    integration_version = _safe_text(getattr(device_obj, "applicationSoftwareVersion", None))
    hardware_revision = _safe_text(getattr(device_obj, "hardwareRevision", None))

    _, status_label = _normalize_system_status(getattr(device_obj, "systemStatus", None))
    system_status = status_label

    object_identifier = _object_identifier_instance_text(
        getattr(device_obj, "objectIdentifier", None),
        fallback=instance,
    )

    ip_address = _to_ipv4_text(getattr(network_obj, "ipAddress", None))
    subnet_mask = _to_ipv4_text(getattr(network_obj, "ipSubnetMask", None))
    udp_port = _to_int(getattr(network_obj, "bacnetIPUDPPort", None))

    mac_address_raw = _mac_hex(getattr(network_obj, "macAddress", None))
    if not mac_address_raw:
        mac_address_raw = _bacnet_mac_from_ip_port(ip_address, udp_port)
    mac_colon = _mac_colon(mac_address_raw)

    address = None

    return {
        "device_object_instance": instance,
        "object_identifier": object_identifier,
        "object_name": object_name,
        "description": description,
        "model_name": model_name,
        "vendor_identifier": vendor_identifier,
        "vendor_name": vendor_name,
        "system_status": system_status,
        "firmware_revision": firmware_revision,
        "integration_version": integration_version,
        "firmware": integration_version,
        "application_software_version": integration_version,
        "hardware_revision": hardware_revision,
        "address": address,
        "ip_address": ip_address,
        "ip_subnet_mask": subnet_mask,
        "subnet_mask": subnet_mask,
        "mac_address": mac_colon,
        "mac_address_raw": mac_colon,
    }


def _client_offline_payload(
    client_instance: int,
    client_address: str,
    network_port_instance_hint: int = 1,
) -> dict[str, Any]:
    instance = int(client_instance)
    address = str(client_address)
    return {
        "online": False,
        "has_device_object": True,
        "has_network_object": False,
        "device": {
            "client_instance": instance,
            "client_address": address,
            "object_identifier": str(instance),
        },
        "network": {
            "client_instance": instance,
            "client_address": address,
        },
        "network_port_instance": int(network_port_instance_hint or 1),
        "name": client_display_name(instance, None),
    }


async def _refresh_client_cache(
    hass: HomeAssistant,
    entry_id: str,
    client_id: str,
    client_instance: int,
    client_address: str,
    network_port_hints: dict[str, int],
    client_targets: dict[str, tuple[int, str]] | None = None,
    force: bool = False,
) -> None:
    cache = _client_cache_get(hass, entry_id, client_id)
    now = time.monotonic()
    last_refresh = float(cache.get("_last_refresh_ts") or 0.0)
    if not force and (now - last_refresh) < CLIENT_REFRESH_MIN_SECONDS:
        return

    lock = _client_lock_get(hass, entry_id, client_id)
    async with lock:
        cache = _client_cache_get(hass, entry_id, client_id)
        previous_device = dict(cache.get("device", {}) or {})
        previous_network = dict(cache.get("network", {}) or {})
        previous_name = _safe_text(cache.get("name"))
        now = time.monotonic()
        last_refresh = float(cache.get("_last_refresh_ts") or 0.0)
        if not force and (now - last_refresh) < CLIENT_REFRESH_MIN_SECONDS:
            return

        server = hass.data.get(DOMAIN, {}).get("servers", {}).get(entry_id)
        app = getattr(server, "app", None) if server is not None else None
        hint = int(network_port_hints.get(client_id, 1))
        resolved_address = str(client_address or "").strip()
        if app is not None:
            resolved_address = await _resolve_client_address(
                app=app,
                server=server,
                client_instance=int(client_instance),
                fallback_address=resolved_address,
            )
        data = _client_offline_payload(client_instance, resolved_address, hint)
        if app is not None:
            try:
                data = await _read_client_runtime(
                    app,
                    int(client_instance),
                    resolved_address,
                    network_port_instance_hint=hint,
                )
            except asyncio.CancelledError:
                raise
            except BaseException:
                _LOGGER.debug(
                    "Client runtime read failed for %s (%s)",
                    client_instance,
                    resolved_address,
                    exc_info=True,
                )
                data = _client_offline_payload(client_instance, resolved_address, hint)
                if previous_device:
                    data["device"] = _merge_non_none(previous_device, dict(data.get("device", {}) or {}))
                if previous_network:
                    data["network"] = _merge_non_none(previous_network, dict(data.get("network", {}) or {}))
                if previous_name:
                    data["name"] = previous_name
                data["has_device_object"] = bool(
                    data.get("has_device_object")
                    or previous_device.get("object_identifier")
                )
                data["has_network_object"] = bool(
                    data.get("has_network_object")
                    or previous_network.get("ip_address")
                    or previous_network.get("ip_subnet_mask")
                    or previous_network.get("mac_address_raw")
                )
            else:
                if previous_device:
                    data["device"] = _merge_non_none(previous_device, dict(data.get("device", {}) or {}))
                if previous_network:
                    data["network"] = _merge_non_none(previous_network, dict(data.get("network", {}) or {}))
                if previous_name and not _safe_text(data.get("name")):
                    data["name"] = previous_name
                data["has_device_object"] = bool(
                    data.get("has_device_object")
                    or previous_device.get("object_identifier")
                )
                data["has_network_object"] = bool(
                    data.get("has_network_object")
                    or previous_network.get("ip_address")
                    or previous_network.get("ip_subnet_mask")
                    or previous_network.get("mac_address_raw")
                )

        network_port_hints[client_id] = int(data.get("network_port_instance") or hint or 1)
        if client_targets is not None:
            client_targets[client_id] = (
                int(client_instance),
                str(data.get("device", {}).get("client_address") or resolved_address),
            )
        data["_last_refresh_ts"] = now
        _client_cache_set(hass, entry_id, client_id, data)
    async_dispatcher_send(hass, _client_diag_signal(entry_id, client_id))
