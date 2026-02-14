# custom_components/bacnet_hub/sensor.py
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional, Callable

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
# StateType is the allowed type for native_value (str|int|float|None)
from homeassistant.helpers.typing import StateType
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EVENT_HOMEASSISTANT_STARTED,
)
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send

from .const import (
    CONF_ADDRESS,
    CONF_DEVICE_DESCRIPTION,
    CONF_DEVICE_NAME,
    CONF_INSTANCE,
    DEFAULT_BACNET_DEVICE_DESCRIPTION,
    DEFAULT_BACNET_OBJECT_NAME,
    DOMAIN,
    mirrored_state_attributes,
    published_entity_id,
    published_suggested_object_id,
    published_unique_id,
)

_LOGGER = logging.getLogger(__name__)

HUB_DIAGNOSTIC_FIELDS: list[tuple[str, str]] = [
    ("description", "Description"),
    ("firmware_revision", "Firmware revision"),
    ("model_name", "Model name"),
    ("object_identifier", "Object identifier"),
    ("object_name", "Object name"),
    ("system_status", "System status"),
    ("vendor_identifier", "Vendor identifier"),
    ("vendor_name", "Vendor name"),
    ("ip_address", "IP address"),
    ("ip_subnet_mask", "IP subnet mask"),
    ("mac_address_raw", "MAC address"),
]
HUB_DIAGNOSTIC_SCAN_INTERVAL = timedelta(seconds=60)
CLIENT_DISCOVERY_TIMEOUT_SECONDS = 3.0
CLIENT_READ_TIMEOUT_SECONDS = 2.5
CLIENT_OBJECTLIST_SCAN_LIMIT = 16
CLIENT_OBJECTLIST_READ_TIMEOUT_SECONDS = 0.6
CLIENT_REDISCOVERY_INTERVAL = timedelta(seconds=60)
CLIENT_SCAN_INTERVAL = timedelta(seconds=60)
CLIENT_REFRESH_MIN_SECONDS = 55.0

CLIENT_DIAGNOSTIC_FIELDS: list[tuple[str, str]] = list(HUB_DIAGNOSTIC_FIELDS)
NETWORK_DIAGNOSTIC_KEYS = {"ip_address", "ip_subnet_mask", "mac_address_raw"}


def _to_state(value: Any) -> StateType:
    if value is None:
        return None
    if isinstance(value, (str, int, float)):
        return value
    return str(value)


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _object_identifier_text(value: Any) -> str | None:
    if isinstance(value, tuple) and len(value) == 2:
        obj_type, inst = value
        type_txt = str(obj_type or "").strip().replace("-", "_").upper()
        if type_txt and _to_int(inst) is not None:
            return f"OBJECT_{type_txt}:{int(inst)}"
    text = str(value or "").strip()
    return text or None


def _object_identifier_instance_text(value: Any, fallback: int | None = None) -> str | None:
    if isinstance(value, tuple) and len(value) == 2:
        inst = _to_int(value[1])
        if inst is not None:
            return str(inst)
    text = str(value or "").strip()
    if text:
        m = re.search(r":\s*(\d+)\s*$", text)
        if m:
            return m.group(1)
        if text.isdigit():
            return text
    if fallback is not None:
        return str(int(fallback))
    return None


def _normalize_system_status(value: Any) -> tuple[int | None, str | None]:
    labels = {
        0: "operational",
        1: "operational_read_only",
        2: "download_required",
        3: "download_in_progress",
        4: "non_operational",
        5: "backup_in_progress",
    }
    if value is None:
        return None, None

    # Enum/Int fast path
    for raw in (getattr(value, "value", None), value):
        try:
            code = int(raw)  # type: ignore[arg-type]
            if code in labels:
                return code, labels[code]
        except Exception:
            pass

    text = str(value).strip()
    if not text:
        return None, None
    norm = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")

    m = re.search(r"(?<!\d)(\d+)(?!\d)", text)
    if m:
        code = _to_int(m.group(1))
        if code is not None and code in labels:
            return code, labels[code]

    aliases = {
        "operational": 0,
        "operational_read_only": 1,
        "operational_readonly": 1,
        "download_required": 2,
        "download_in_progress": 3,
        "non_operational": 4,
        "backup_in_progress": 5,
    }
    for token, code in aliases.items():
        if token in norm:
            return code, labels[code]

    return None, None


def _mac_hex(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex().upper()
    text = str(value).strip()
    if not text:
        return None
    hex_only = re.sub(r"[^0-9A-Fa-f]", "", text)
    if len(hex_only) >= 12 and len(hex_only) % 2 == 0:
        return hex_only.upper()
    return None


def _mac_colon(value: Any) -> str | None:
    hex_text = _mac_hex(value)
    if not hex_text:
        return None
    return ":".join(hex_text[idx:idx + 2] for idx in range(0, len(hex_text), 2))


def _bacnet_mac_from_ip_port(ip_address: Any, udp_port: Any) -> str | None:
    ip = str(ip_address or "").strip()
    port = _to_int(udp_port)
    parts = ip.split(".")
    if len(parts) != 4 or port is None or port < 0 or port > 65535:
        return None
    try:
        octets = [int(part) for part in parts]
    except Exception:
        return None
    if any(octet < 0 or octet > 255 for octet in octets):
        return None
    return "".join(f"{octet:02X}" for octet in octets) + f"{port:04X}"


def _to_ipv4_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        if len(raw) == 4:
            return ".".join(str(octet) for octet in raw)
        return None
    try:
        raw = bytes(value)
        if len(raw) == 4:
            return ".".join(str(octet) for octet in raw)
    except Exception:
        pass
    text = str(value).strip()
    if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", text):
        return text
    return None


def _client_id(instance: int) -> str:
    return f"client_{int(instance)}"


def _client_diag_signal(entry_id: str, client_id: str) -> str:
    return f"{DOMAIN}_client_diag_{entry_id}_{client_id}"


def _hub_diag_signal(entry_id: str) -> str:
    return f"{DOMAIN}_hub_diag_{entry_id}"


def _diag_field_slug(key: str) -> str:
    text = str(key or "").strip().lower()
    if text == "mac_address_raw":
        return "mac_address"
    return re.sub(r"[^a-z0-9_]+", "_", text).strip("_") or "value"


def _doi_entity_id(instance: int | str | None, field_key: str, *, network: bool) -> str:
    inst = _to_int(instance)
    if inst is None:
        inst = 0
    prefix = "net_" if network else ""
    return f"sensor.bacnet_doi_{int(inst)}_{prefix}{_diag_field_slug(field_key)}"


def _client_cache_root(hass: HomeAssistant) -> dict[str, dict[str, dict[str, Any]]]:
    root = hass.data.setdefault(DOMAIN, {})
    return root.setdefault("client_diag_cache", {})


def _client_cache_get(hass: HomeAssistant, entry_id: str, client_id: str) -> dict[str, Any]:
    per_entry = _client_cache_root(hass).setdefault(entry_id, {})
    return per_entry.setdefault(client_id, {})


def _client_cache_set(hass: HomeAssistant, entry_id: str, client_id: str, payload: dict[str, Any]) -> None:
    cache = _client_cache_get(hass, entry_id, client_id)
    cache.update(payload or {})


def _client_locks_root(hass: HomeAssistant) -> dict[str, dict[str, asyncio.Lock]]:
    root = hass.data.setdefault(DOMAIN, {})
    return root.setdefault("client_diag_locks", {})


def _client_lock_get(hass: HomeAssistant, entry_id: str, client_id: str) -> asyncio.Lock:
    per_entry = _client_locks_root(hass).setdefault(entry_id, {})
    lock = per_entry.get(client_id)
    if lock is None:
        lock = asyncio.Lock()
        per_entry[client_id] = lock
    return lock


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _object_identifier_instance(value: Any, object_type: str) -> int | None:
    if isinstance(value, tuple) and len(value) == 2:
        obj_type, inst = value
        if str(obj_type).replace("-", "").lower() == object_type.replace("-", "").lower():
            return _to_int(inst)
    text = str(value or "").strip()
    m = re.search(rf"{object_type}[:\s,]+(\d+)$", text, flags=re.IGNORECASE)
    if m:
        return _to_int(m.group(1))
    return None


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


async def _read_remote_property_any_objid(
    app: Any,
    address: str,
    objids: list[str],
    prop: str,
) -> Any:
    last_err: Exception | None = None
    for objid in objids:
        try:
            return await _read_remote_property(app, address, objid, prop)
        except Exception as err:
            last_err = err
            continue
    if last_err:
        raise last_err
    return None


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

    # Prefer BACpypes device cache if available.
    try:
        device_info = await app.get_device_info(instance)
        device_address = getattr(device_info, "device_address", None) if device_info else None
        if device_address:
            addr = str(device_address).strip()
            if addr:
                return addr
    except Exception:
        pass

    # Fallback to a quick discovery pass for this specific instance.
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

    # Fast probe: if device cannot even answer a basic property,
    # return an offline payload quickly and avoid long sequential timeouts.
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

    # Device object
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

    # Resolve network-port instance (if not instance 1).
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
        "name": object_name or f"BACnet Client {instance}",
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
        "name": f"BACnet Client {instance}",
    }


async def _refresh_client_cache(
    hass: HomeAssistant,
    entry_id: str,
    client_id: str,
    client_instance: int,
    client_address: str,
    network_port_hints: dict[str, int],
    client_targets: dict[str, tuple[int, str]] | None = None,
) -> None:
    cache = _client_cache_get(hass, entry_id, client_id)
    now = time.monotonic()
    last_refresh = float(cache.get("_last_refresh_ts") or 0.0)
    if (now - last_refresh) < CLIENT_REFRESH_MIN_SECONDS:
        return

    lock = _client_lock_get(hass, entry_id, client_id)
    async with lock:
        cache = _client_cache_get(hass, entry_id, client_id)
        now = time.monotonic()
        last_refresh = float(cache.get("_last_refresh_ts") or 0.0)
        if (now - last_refresh) < CLIENT_REFRESH_MIN_SECONDS:
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

        network_port_hints[client_id] = int(data.get("network_port_instance") or hint or 1)
        if client_targets is not None:
            client_targets[client_id] = (
                int(client_instance),
                str(data.get("device", {}).get("client_address") or resolved_address),
            )
        data["_last_refresh_ts"] = now
        _client_cache_set(hass, entry_id, client_id, data)
    async_dispatcher_send(hass, _client_diag_signal(entry_id, client_id))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    data = hass.data[DOMAIN]
    published: List[Dict[str, Any]] = data.get("published", {}).get(entry.entry_id, []) or []
    merged = {**(entry.data or {}), **(entry.options or {})}
    hub_instance = merged.get(CONF_INSTANCE, 0)
    hub_address = merged.get(CONF_ADDRESS, "")
    hub_name = str(merged.get(CONF_DEVICE_NAME) or DEFAULT_BACNET_OBJECT_NAME)

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
        # Add hub diagnostics immediately so they are created before other entities.
        async_add_entities(hub_entities)

    server = data.get("servers", {}).get(entry.entry_id)
    known_client_instances: set[int] = set()
    client_targets: dict[str, tuple[int, str]] = {}
    client_network_port_hints: dict[str, int] = {}
    client_added_field_keys: dict[str, set[tuple[str, str]]] = {}

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
            await _refresh_client_cache(
                hass=hass,
                entry_id=entry.entry_id,
                client_id=client_id,
                client_instance=client_instance,
                client_address=client_address,
                network_port_hints=client_network_port_hints,
                client_targets=client_targets,
            )
            cache = _client_cache_get(hass, entry.entry_id, client_id)
            if bool(cache.get("has_network_object")):
                new_entities.extend(
                    _client_entities(
                        client_instance,
                        client_address,
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
            else:
                _client_entities(client_instance, client_address, include_network=False)
                client_id = _client_id(int(client_instance))
                cache = _client_cache_get(hass, entry.entry_id, client_id)
                if bool(cache.get("has_network_object")):
                    new_entities.extend(
                        _client_entities(
                            client_instance,
                            client_address,
                            include_network=True,
                        )
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
        async_dispatcher_send(hass, _hub_diag_signal(entry.entry_id))

    unsub_hub_diag = async_track_time_interval(
        hass,
        _schedule_hub_diag_refresh,
        HUB_DIAGNOSTIC_SCAN_INTERVAL,
    )
    entry.async_on_unload(unsub_hub_diag)
    async_dispatcher_send(hass, _hub_diag_signal(entry.entry_id))

    async def _initial_client_refresh() -> None:
        await asyncio.sleep(5)
        await _refresh_all_clients()

    hass.async_create_task(_initial_client_refresh())

    unsub_refresh = async_track_time_interval(hass, _schedule_client_refresh, CLIENT_SCAN_INTERVAL)
    unsub_rescan = async_track_time_interval(hass, _schedule_rescan, CLIENT_REDISCOVERY_INTERVAL)
    entry.async_on_unload(unsub_refresh)
    entry.async_on_unload(unsub_rescan)


class BacnetPublishedSensor(SensorEntity):
    """BACnet published sensor that mirrors metadata 1:1 from the source.

    - State/Value is taken from the source.
    - device_class/state_class are mirrored if valid.
    - Icon is mirrored if set.
    """

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        hub_instance: int | str,
        hub_address: str,
        hub_name: str,
        source_entity_id: str,
        instance: int,
        name: str,
        source_attr: str | None,
        read_attr: str | None,
        configured_unit: str | None,
    ):
        self.hass = hass
        self._entry_id = entry_id
        self._source = source_entity_id
        self._source_attr = str(source_attr or "").strip()
        self._read_attr = str(read_attr or "").strip()
        self._configured_unit = configured_unit
        self._instance = instance
        self._attr_name = name
        self._remove_listener = None
        self._late_unsub: Optional[Callable[[], None]] = None
        self._attr_unique_id = published_unique_id(
            hub_instance=hub_instance,
            hub_address=hub_address,
            object_type="analogValue",
            object_instance=instance,
        )
        self._suggested_object_id = published_suggested_object_id(
            "analogValue",
            instance,
            hub_instance,
        )
        self.entity_id = published_entity_id(
            "sensor",
            "analogValue",
            instance,
            hub_instance,
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=hub_name or DEFAULT_BACNET_OBJECT_NAME,
            manufacturer="magliaral",
            model="BACnet Hub",
        )

        # dynamic attributes
        self._attr_native_unit_of_measurement: Optional[str] = None
        self._attr_device_class: Optional[SensorDeviceClass] = None
        self._attr_state_class: Optional[SensorStateClass] = None
        self._attr_icon: Optional[str] = None
        self._attr_native_value: Optional[StateType] = None
        self._attr_extra_state_attributes: Dict[str, Any] = {}

    @property
    def suggested_object_id(self) -> str | None:
        return self._suggested_object_id

    async def async_added_to_hass(self) -> None:
        # Initial
        self._pull_from_source()

        # If source not loaded at start, pull again after HA start
        if not self.hass.states.get(self._source):
            @callback
            def _late_initial_pull(_):
                self._pull_from_source()
                # listen_once has fired -> prevent further manual unsubscribe
                self._late_unsub = None

            # IMPORTANT: do NOT register unsubscribe via async_on_remove
            self._late_unsub = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, _late_initial_pull
            )

        # Live updates from source
        @callback
        def _handle(evt):
            if evt.data.get("entity_id") != self._source:
                return
            self._pull_from_source()

        self._remove_listener = async_track_state_change_event(self.hass, [self._source], _handle)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            try:
                self._remove_listener()
            except Exception:
                pass
            self._remove_listener = None

        # Only unsubscribe late-once listener if it still exists
        if self._late_unsub is not None:
            try:
                self._late_unsub()
            except Exception:
                pass
            self._late_unsub = None

    @callback
    def _pull_from_source(self) -> None:
        st = self.hass.states.get(self._source)
        if not st:
            # Source temporarily gone:
            # - Keep metadata consistent while source is unavailable
            # - Clear metadata
            # - Do NOT forcibly set last value to None (more stable tile)
            self._attr_device_class = None
            self._attr_state_class = None
            self._attr_icon = None
            self._attr_extra_state_attributes = {}
            self.async_write_ha_state()
            return

        # Take name from source
        src_name = st.name or self._source
        friendly_name = st.attributes.get("friendly_name") or src_name
        if self._source_attr:
            field_name = self._source_attr.replace("_", " ").title()
            friendly_name = f"{friendly_name} {field_name}"
        self._attr_name = f"(BACnet AV-{self._instance}) {friendly_name}"

        # Mirror unit exactly
        attr_name = self._read_attr or self._source_attr
        source_value = st.attributes.get(attr_name) if attr_name else st.state
        unit = st.attributes.get("unit_of_measurement") or self._configured_unit
        if self._source_attr in ("current_temperature", "temperature", "set_temperature") and not unit:
            unit = st.attributes.get("temperature_unit")
        self._attr_native_unit_of_measurement = unit

        # Take device_class/state_class exactly if present/valid
        self._attr_device_class = None
        self._attr_state_class = None
        src_dc = st.attributes.get("device_class")
        src_sc = st.attributes.get("state_class")

        if isinstance(src_dc, str) and src_dc:
            try:
                self._attr_device_class = SensorDeviceClass(src_dc)
            except ValueError:
                self._attr_device_class = None

        if isinstance(src_sc, str) and src_sc:
            try:
                self._attr_state_class = SensorStateClass(src_sc)
            except ValueError:
                self._attr_state_class = None

        # Mirror icon exactly if explicitly set
        self._attr_icon = st.attributes.get("icon") or None
        mirrored_attrs = mirrored_state_attributes(dict(st.attributes or {}))
        mirrored_attrs["source_entity_id"] = self._source
        self._attr_extra_state_attributes = mirrored_attrs

        # Take value:
        # - unknown/unavailable → None
        # - If unit present or numeric device_class → try float
        # - otherwise take raw value (StateType)
        state = source_value
        if state in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
            native_value: StateType = None
        else:
            try:
                if unit or (self._attr_device_class in {
                    SensorDeviceClass.TEMPERATURE,
                    SensorDeviceClass.POWER,
                    SensorDeviceClass.ENERGY,
                    SensorDeviceClass.VOLTAGE,
                    SensorDeviceClass.CURRENT,
                    SensorDeviceClass.FREQUENCY,
                    SensorDeviceClass.ILLUMINANCE,
                    SensorDeviceClass.PRESSURE,
                    SensorDeviceClass.IRRADIANCE,
                }):
                    native_value = float(state)  # type: ignore[assignment]
                else:
                    native_value = state  # can be str/int/float
            except Exception:
                native_value = None

        self._attr_native_value = native_value
        self.async_write_ha_state()


class BacnetHubDetailSensor(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:information-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        merged: Dict[str, Any],
        key: str,
        label: str,
    ) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._merged = dict(merged or {})
        self._key = key
        self._attr_name = label
        self._attr_unique_id = f"{entry_id}-hub-diagnostic-{key}"
        self.entity_id = _doi_entity_id(
            _to_int(self._merged.get(CONF_INSTANCE)),
            key,
            network=(key in NETWORK_DIAGNOSTIC_KEYS),
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=str(self._merged.get(CONF_DEVICE_NAME) or DEFAULT_BACNET_OBJECT_NAME),
            manufacturer="magliaral",
            model="BACnet Hub",
        )
        self._unsub_dispatcher: Callable[[], None] | None = None

    def _server(self) -> Any:
        return (self.hass.data.get(DOMAIN, {}).get("servers", {}) or {}).get(self._entry_id)

    async def async_added_to_hass(self) -> None:
        signal = _hub_diag_signal(self._entry_id)
        self._unsub_dispatcher = async_dispatcher_connect(
            self.hass,
            signal,
            self._handle_hub_update,
        )
        self._handle_hub_update()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_dispatcher is not None:
            self._unsub_dispatcher()
            self._unsub_dispatcher = None

    @callback
    def _handle_hub_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> StateType:
        diagnostics = _hub_diagnostics(self._server(), self._merged)
        return _to_state(diagnostics.get(self._key))


class BacnetClientDetailSensor(SensorEntity):
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_icon = "mdi:information-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        key: str,
        label: str,
        source: str,
    ) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._client_id = client_id
        self._client_instance = int(client_instance)
        self._key = key
        self._source = "network" if source == "network" else "device"
        self._attr_name = label
        self._attr_unique_id = f"{entry_id}-{client_id}-{self._source}-{key}"
        self.entity_id = _doi_entity_id(
            self._client_instance,
            key,
            network=(self._source == "network"),
        )
        self._attr_native_value: StateType = None
        self._unsub_dispatcher: Callable[[], None] | None = None
        self._device_info_cache = DeviceInfo(
            identifiers={(DOMAIN, client_id)},
            via_device=(DOMAIN, entry_id),
            name=f"BACnet Client",
        )

    @property
    def device_info(self) -> DeviceInfo:
        return self._device_info_cache

    async def async_added_to_hass(self) -> None:
        signal = _client_diag_signal(self._entry_id, self._client_id)
        self._unsub_dispatcher = async_dispatcher_connect(
            self.hass,
            signal,
            self._handle_client_update,
        )
        self._handle_client_update()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_dispatcher is not None:
            self._unsub_dispatcher()
            self._unsub_dispatcher = None

    @callback
    def _handle_client_update(self) -> None:
        cache = _client_cache_get(self.hass, self._entry_id, self._client_id)
        data = dict(cache.get(self._source, {}) or {})
        self._attr_native_value = _to_state(data.get(self._key))

        self._device_info_cache = DeviceInfo(
            identifiers={(DOMAIN, self._client_id)},
            via_device=(DOMAIN, self._entry_id),
            name=str(cache.get("name") or "BACnet Client"),
            manufacturer=_safe_text(data.get("vendor_name")),
            model=_safe_text(data.get("model_name")),
            sw_version=_safe_text(data.get("firmware_revision")),
            hw_version=_safe_text(data.get("hardware_revision")),
            serial_number=_safe_text(data.get("serial_number")),
        )
        self.async_write_ha_state()
