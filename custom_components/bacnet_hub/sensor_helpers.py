from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import StateType

from .const import DOMAIN

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
CLIENT_POINT_REFRESH_TIMEOUT_SECONDS = 6.0
CLIENT_POINT_SCAN_LIMIT = 128
CLIENT_REDISCOVERY_INTERVAL = timedelta(seconds=60)
CLIENT_SCAN_INTERVAL = timedelta(seconds=60)
CLIENT_REFRESH_MIN_SECONDS = 55.0
CLIENT_COV_LEASE_SECONDS = 300

CLIENT_DIAGNOSTIC_FIELDS: list[tuple[str, str]] = list(HUB_DIAGNOSTIC_FIELDS)
NETWORK_DIAGNOSTIC_KEYS = {"ip_address", "ip_subnet_mask", "mac_address_raw"}
CLIENT_POINT_SUPPORTED_TYPES: dict[str, tuple[str, str]] = {
    "analoginput": ("ai", "analog-input"),
    "analogvalue": ("av", "analog-value"),
    "binaryvalue": ("bv", "binary-value"),
    "multistatevalue": ("mv", "multi-state-value"),
    "characterstringvalue": ("csv", "characterstring-value"),
}


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


def _client_points_signal(entry_id: str, client_id: str) -> str:
    return f"{DOMAIN}_client_points_{entry_id}_{client_id}"


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


def _point_entity_id(client_instance: int, type_slug: str, object_instance: int) -> str:
    return f"sensor.bacnet_doi_{int(client_instance)}_{type_slug}_{int(object_instance)}"


def _point_unique_id(entry_id: str, client_id: str, type_slug: str, object_instance: int) -> str:
    return f"{entry_id}-{client_id}-point-{type_slug}-{int(object_instance)}"


def _cov_process_identifier(entry_id: str, client_id: str, point_key: str) -> int:
    seed = f"{entry_id}:{client_id}:{point_key}"
    return (abs(hash(seed)) % 4194303) + 1


def _client_cache_root(hass: HomeAssistant) -> dict[str, dict[str, dict[str, Any]]]:
    root = hass.data.setdefault(DOMAIN, {})
    return root.setdefault("client_diag_cache", {})


def _client_cache_get(hass: HomeAssistant, entry_id: str, client_id: str) -> dict[str, Any]:
    per_entry = _client_cache_root(hass).setdefault(entry_id, {})
    return per_entry.setdefault(client_id, {})


def _client_cache_set(hass: HomeAssistant, entry_id: str, client_id: str, payload: dict[str, Any]) -> None:
    cache = _client_cache_get(hass, entry_id, client_id)
    cache.update(payload or {})


def _client_points_root(hass: HomeAssistant) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    root = hass.data.setdefault(DOMAIN, {})
    return root.setdefault("client_point_cache", {})


def _client_points_get(hass: HomeAssistant, entry_id: str, client_id: str) -> dict[str, dict[str, Any]]:
    per_entry = _client_points_root(hass).setdefault(entry_id, {})
    return per_entry.setdefault(client_id, {})


def _client_points_set(
    hass: HomeAssistant,
    entry_id: str,
    client_id: str,
    payload: dict[str, dict[str, Any]],
) -> None:
    cache = _client_points_get(hass, entry_id, client_id)
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


def _normalize_object_type_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def _supported_point_type(value: Any) -> tuple[str, str] | None:
    if isinstance(value, tuple) and len(value) == 2:
        return _supported_point_type(value[0])
    raw = str(value or "").strip()
    key = _normalize_object_type_key(raw)
    if key in CLIENT_POINT_SUPPORTED_TYPES:
        return CLIENT_POINT_SUPPORTED_TYPES[key]

    for prefix in ("objecttype", "object", "enum", "bacnetobjecttype"):
        if key.startswith(prefix):
            trimmed = key[len(prefix):]
            if trimmed in CLIENT_POINT_SUPPORTED_TYPES:
                return CLIENT_POINT_SUPPORTED_TYPES[trimmed]

    low = raw.lower()
    if "analog" in low and "input" in low:
        return CLIENT_POINT_SUPPORTED_TYPES.get("analoginput")
    if "analog" in low and "value" in low:
        return CLIENT_POINT_SUPPORTED_TYPES.get("analogvalue")
    if "binary" in low and "value" in low:
        return CLIENT_POINT_SUPPORTED_TYPES.get("binaryvalue")
    if "multistate" in low and "value" in low:
        return CLIENT_POINT_SUPPORTED_TYPES.get("multistatevalue")
    if "characterstring" in low and "value" in low:
        return CLIENT_POINT_SUPPORTED_TYPES.get("characterstringvalue")
    return None


def _object_instance(value: Any) -> int | None:
    if isinstance(value, tuple) and len(value) == 2:
        return _to_int(value[1])
    text = str(value or "").strip()
    m = re.search(r"(\d+)\s*$", text)
    if m:
        return _to_int(m.group(1))
    return None


def _parse_object_list_item(value: Any) -> tuple[str, int] | None:
    if isinstance(value, tuple) and len(value) == 2:
        inst = _to_int(value[1])
        if inst is not None:
            return str(value[0]), int(inst)

    object_type = (
        getattr(value, "objectType", None)
        or getattr(value, "object_type", None)
        or getattr(value, "type", None)
    )
    object_instance = (
        getattr(value, "instance", None)
        or getattr(value, "objectInstance", None)
        or getattr(value, "object_instance", None)
        or getattr(value, "instanceNumber", None)
    )
    inst = _to_int(object_instance)
    if object_type is not None and inst is not None:
        return str(object_type), int(inst)

    text = str(value or "").strip()
    if not text:
        return None

    m = re.search(r"([A-Za-z0-9_\- ]+)\s*[:,]\s*(\d+)\s*$", text)
    if m:
        return m.group(1), int(m.group(2))
    m = re.search(r"([A-Za-z0-9_\- ]+)\s+(\d+)\s*$", text)
    if m:
        return m.group(1), int(m.group(2))
    return None


def _object_identifier_compact(value: Any, fallback_type: str, fallback_instance: int) -> str:
    if isinstance(value, tuple) and len(value) == 2:
        return f"{value[0]},{int(value[1])}"
    text = str(value or "").strip()
    return text or f"{fallback_type},{int(fallback_instance)}"


def _normalize_bacnet_unit(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    norm = re.sub(r"[^a-z0-9]+", "", raw.lower())
    mapping = {
        "degreescelsius": "Â°C",
        "degreescelsius": "Â°C",
        "degreesfahrenheit": "Â°F",
        "percent": "%",
        "partspermillion": "ppm",
        "pascals": "Pa",
        "kilopascals": "kPa",
        "watts": "W",
        "kilowatts": "kW",
        "wattshour": "Wh",
        "watthours": "Wh",
        "kilowatthours": "kWh",
        "volts": "V",
        "amperes": "A",
        "hertz": "Hz",
    }
    return mapping.get(norm, raw)


def _sensor_device_class_from_unit(unit: str | None) -> SensorDeviceClass | None:
    u = str(unit or "").strip().lower()
    if u in {"Â°c", "Â°f"}:
        return SensorDeviceClass.TEMPERATURE
    if u in {"w", "kw"}:
        return SensorDeviceClass.POWER
    if u in {"wh", "kwh"}:
        return SensorDeviceClass.ENERGY
    if u == "v":
        return SensorDeviceClass.VOLTAGE
    if u == "a":
        return SensorDeviceClass.CURRENT
    if u == "hz":
        return SensorDeviceClass.FREQUENCY
    return None


def _point_native_value_from_payload(point: dict[str, Any]) -> StateType:
    value = point.get("present_value")
    if value is None:
        return None

    type_slug = str(point.get("type_slug") or "")
    if type_slug in {"ai", "av"}:
        try:
            return float(value)
        except Exception:
            pass
    if type_slug == "bv":
        text = str(value).strip().lower()
        if text in ("active", "on", "true", "1"):
            active_text = _safe_text(point.get("active_text"))
            return active_text or "active"
        if text in ("inactive", "off", "false", "0"):
            inactive_text = _safe_text(point.get("inactive_text"))
            return inactive_text or "inactive"
        return str(value)

    if type_slug == "mv":
        idx = _to_int(value)
        texts = point.get("state_text")
        if idx is not None and isinstance(texts, (list, tuple)):
            pos = idx - 1
            if 0 <= pos < len(texts):
                label = _safe_text(texts[pos])
                if label:
                    return label

    if isinstance(value, (str, int, float)):
        return value
    return str(value)


def _property_slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "." in text:
        text = text.split(".")[-1]
    return re.sub(r"[^a-z0-9]+", "", text)
