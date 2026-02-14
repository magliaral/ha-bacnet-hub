# custom_components/bacnet_hub/sensor.py
from __future__ import annotations

import asyncio
import re
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
from homeassistant.helpers.event import async_track_state_change_event

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

HUB_DIAGNOSTIC_FIELDS: list[tuple[str, str]] = [
    ("description", "Description"),
    ("firmware_revision", "Firmware revision"),
    ("model_name", "Model name"),
    ("object_identifier", "Object identifier"),
    ("object_name", "Object name"),
    ("system_status", "System status"),
    ("system_status_code", "System status code"),
    ("vendor_identifier", "Vendor identifier"),
    ("vendor_name", "Vendor name"),
    ("ip_address", "IP address"),
    ("ip_subnet_mask", "IP subnet mask"),
    ("mac_address_raw", "MAC address"),
]
DIAGNOSTIC_REFRESH_DELAY_SECONDS = 2.0


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


def _normalize_system_status(value: Any) -> tuple[int | None, str | None]:
    labels = {
        0: "operational",
        1: "operational_read_only",
        2: "download_required",
        3: "download_in_progress",
        4: "non_operational",
        5: "backup_in_progress",
    }
    text = str(value or "").strip()
    if not text:
        return None, None

    m = re.search(r"\b([0-5])\b", text)
    if m:
        code = int(m.group(1))
        return code, labels.get(code)

    token = text.split(".")[-1].strip().lower()
    token = re.sub(r"[^a-z0-9]+", "_", token).strip("_")
    if token.isdigit():
        code = int(token)
        return code, labels.get(code)

    for code, label in labels.items():
        if token == label:
            return code, label
    return None, token or None


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


def _hub_diagnostics(server: Any, merged: Dict[str, Any]) -> Dict[str, Any]:
    device_obj = getattr(server, "device_object", None) if server is not None else None
    network_obj = getattr(server, "network_port_object", None) if server is not None else None

    instance = _to_int(getattr(server, "instance", None)) if server is not None else None
    if instance is None:
        instance = _to_int(merged.get(CONF_INSTANCE))

    object_name = getattr(device_obj, "objectName", None) or (
        getattr(server, "name", None) if server is not None else merged.get(CONF_DEVICE_NAME)
    )
    description = getattr(device_obj, "description", None) or (
        getattr(server, "description", None)
        if server is not None
        else merged.get(CONF_DEVICE_DESCRIPTION, DEFAULT_BACNET_DEVICE_DESCRIPTION)
    )
    model_name = getattr(device_obj, "modelName", None) or (
        getattr(server, "model_name", None) if server is not None else None
    )
    vendor_name = getattr(device_obj, "vendorName", None) or (
        getattr(server, "vendor_name", None) if server is not None else None
    )
    vendor_identifier = _to_int(getattr(device_obj, "vendorIdentifier", None))
    if vendor_identifier is None and server is not None:
        vendor_identifier = _to_int(getattr(server, "vendor_identifier", None))

    firmware_revision = getattr(device_obj, "firmwareRevision", None) or (
        getattr(server, "firmware_revision", None) if server is not None else None
    )
    integration_version = (
        getattr(server, "application_software_version", None) if server is not None else None
    )
    hardware_revision = getattr(server, "hardware_revision", None) if server is not None else None

    status_code, status_label = _normalize_system_status(getattr(device_obj, "systemStatus", None))
    if status_code is None and server is not None:
        status_code = _to_int(getattr(server, "system_status_code", None))
    if status_label is None and server is not None:
        status_label = str(getattr(server, "system_status", "")).strip() or None
    system_status = status_label
    system_status_code = status_code

    object_identifier = _object_identifier_text(getattr(device_obj, "objectIdentifier", None))
    if not object_identifier and server is not None:
        object_identifier = str(getattr(server, "device_object_identifier", "")).strip() or None

    network_port_object_identifier = _object_identifier_text(
        getattr(network_obj, "objectIdentifier", None)
    )
    if not network_port_object_identifier and server is not None:
        network_port_object_identifier = (
            str(getattr(server, "network_port_object_identifier", "")).strip() or None
        )

    ip_address = getattr(network_obj, "ipAddress", None) or (
        getattr(server, "ip_address", None) if server is not None else None
    )
    subnet_mask = getattr(network_obj, "ipSubnetMask", None) or (
        getattr(server, "subnet_mask", None) if server is not None else None
    )
    udp_port = _to_int(getattr(network_obj, "bacnetIPUDPPort", None))
    if udp_port is None and server is not None:
        udp_port = _to_int(getattr(server, "udp_port", None))
    network_prefix = getattr(server, "network_prefix", None) if server is not None else None
    network_port_instance = _to_int(getattr(server, "network_port_instance", None)) if server else None
    network_number = _to_int(getattr(network_obj, "networkNumber", None))
    if network_number is None and server is not None:
        network_number = _to_int(getattr(server, "network_number", None))

    mac_address_raw = _mac_hex(getattr(network_obj, "macAddress", None))
    if not mac_address_raw:
        mac_address_raw = _mac_hex(getattr(server, "mac_address", None)) if server else None
    if not mac_address_raw:
        mac_address_raw = _bacnet_mac_from_ip_port(ip_address, udp_port)

    interface = getattr(server, "network_interface", None) if server is not None else None
    address = (
        getattr(server, "address_str", None)
        if server is not None
        else merged.get(CONF_ADDRESS)
    )

    return {
        "device_object_instance": instance,
        "object_identifier": object_identifier,
        "object_name": object_name,
        "description": description,
        "model_name": model_name,
        "vendor_identifier": vendor_identifier,
        "vendor_name": vendor_name,
        "system_status": system_status,
        "system_status_code": system_status_code,
        "firmware_revision": firmware_revision,
        "integration_version": integration_version,
        "firmware": integration_version,
        "application_software_version": integration_version,
        "hardware_revision": hardware_revision,
        "address": address,
        "ip_address": ip_address,
        "ip_subnet_mask": subnet_mask,
        "network_prefix": network_prefix,
        "subnet_mask": subnet_mask,
        "mac_address": mac_address_raw,
        "mac_address_raw": mac_address_raw,
        "network_interface": interface,
        "udp_port": udp_port,
        "network_number": network_number,
        "network_port_instance": network_port_instance,
        "network_port_object_identifier": network_port_object_identifier,
    }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    data = hass.data[DOMAIN]
    published: List[Dict[str, Any]] = data.get("published", {}).get(entry.entry_id, []) or []
    merged = {**(entry.data or {}), **(entry.options or {})}
    hub_instance = merged.get(CONF_INSTANCE, 0)
    hub_address = merged.get(CONF_ADDRESS, "")
    hub_name = str(merged.get(CONF_DEVICE_NAME) or DEFAULT_BACNET_OBJECT_NAME)

    entities: List[SensorEntity] = []
    entities.append(
        BacnetHubInfoSensor(
            hass=hass,
            entry_id=entry.entry_id,
            merged=merged,
        )
    )
    for key, label in HUB_DIAGNOSTIC_FIELDS:
        entities.append(
            BacnetHubDetailSensor(
                hass=hass,
                entry_id=entry.entry_id,
                merged=merged,
                key=key,
                label=label,
            )
        )
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
        entities.append(
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
    async_add_entities(entities)


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
        self._suggested_object_id = published_suggested_object_id("analogValue", instance)
        self.entity_id = published_entity_id("sensor", "analogValue", instance)
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


class BacnetHubInfoSensor(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Hub information"
    _attr_icon = "mdi:server-network"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        merged: Dict[str, Any],
    ) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._merged = dict(merged or {})
        self._attr_unique_id = f"{entry_id}-hub-information"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=str(self._merged.get(CONF_DEVICE_NAME) or DEFAULT_BACNET_OBJECT_NAME),
            manufacturer="magliaral",
            model="BACnet Hub",
        )

    def _server(self) -> Any:
        return (self.hass.data.get(DOMAIN, {}).get("servers", {}) or {}).get(self._entry_id)

    async def async_added_to_hass(self) -> None:
        # Ensure diagnostics are refreshed once after startup/reload.
        self.async_write_ha_state()

        async def _late_refresh() -> None:
            await asyncio.sleep(DIAGNOSTIC_REFRESH_DELAY_SECONDS)
            self.async_write_ha_state()

        self.hass.async_create_task(_late_refresh())

    @property
    def native_value(self) -> StateType:
        server = self._server()
        return "running" if server and getattr(server, "app", None) else "stopped"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        server = self._server()
        return _hub_diagnostics(server, self._merged)


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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=str(self._merged.get(CONF_DEVICE_NAME) or DEFAULT_BACNET_OBJECT_NAME),
            manufacturer="magliaral",
            model="BACnet Hub",
        )

    def _server(self) -> Any:
        return (self.hass.data.get(DOMAIN, {}).get("servers", {}) or {}).get(self._entry_id)

    async def async_added_to_hass(self) -> None:
        self.async_write_ha_state()

        async def _late_refresh() -> None:
            await asyncio.sleep(DIAGNOSTIC_REFRESH_DELAY_SECONDS)
            self.async_write_ha_state()

        self.hass.async_create_task(_late_refresh())

    @property
    def native_value(self) -> StateType:
        diagnostics = _hub_diagnostics(self._server(), self._merged)
        return _to_state(diagnostics.get(self._key))
