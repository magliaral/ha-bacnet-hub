from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, Optional

from bacpypes3.pdu import Address
from bacpypes3.primitivedata import ObjectIdentifier
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.typing import StateType

from .const import (
    CONF_INSTANCE,
    DOMAIN,
    client_display_name,
    hub_display_name,
    mirrored_state_attributes,
    published_entity_id,
    published_suggested_object_id,
    published_unique_id,
)
from .sensor_helpers import (
    CLIENT_COV_LEASE_SECONDS,
    NETWORK_DIAGNOSTIC_KEYS,
    _client_cache_get,
    _client_diag_signal,
    _client_points_get,
    _client_points_set,
    _client_points_signal,
    _cov_process_identifier,
    _doi_entity_id,
    _hub_diag_signal,
    _point_entity_id,
    _point_native_value_from_payload,
    _point_unique_id,
    _property_slug,
    _safe_text,
    _sensor_device_class_from_unit,
    _to_int,
    _to_state,
)
from .sensor_runtime import _hub_diagnostics

_LOGGER = logging.getLogger(__name__)


class BacnetPublishedSensor(SensorEntity):
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
            name=hub_name,
            manufacturer="magliaral",
            model="BACnet Hub",
        )

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
        self._pull_from_source()

        if not self.hass.states.get(self._source):

            @callback
            def _late_initial_pull(_):
                self._pull_from_source()
                self._late_unsub = None

            self._late_unsub = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, _late_initial_pull
            )

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
            self._attr_device_class = None
            self._attr_state_class = None
            self._attr_icon = None
            self._attr_extra_state_attributes = {}
            self.async_write_ha_state()
            return

        src_name = st.name or self._source
        friendly_name = st.attributes.get("friendly_name") or src_name
        if self._source_attr:
            field_name = self._source_attr.replace("_", " ").title()
            friendly_name = f"{friendly_name} {field_name}"
        self._attr_name = f"(BACnet AV-{self._instance}) {friendly_name}"

        attr_name = self._read_attr or self._source_attr
        source_value = st.attributes.get(attr_name) if attr_name else st.state
        unit = st.attributes.get("unit_of_measurement") or self._configured_unit
        if self._source_attr in ("current_temperature", "temperature", "set_temperature") and not unit:
            unit = st.attributes.get("temperature_unit")
        self._attr_native_unit_of_measurement = unit

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

        self._attr_icon = st.attributes.get("icon") or None
        mirrored_attrs = mirrored_state_attributes(dict(st.attributes or {}))
        mirrored_attrs["source_entity_id"] = self._source
        self._attr_extra_state_attributes = mirrored_attrs

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
                    native_value = state
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
            name=hub_display_name(self._merged.get(CONF_INSTANCE)),
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
            name=client_display_name(self._client_instance),
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
        device_data = dict(cache.get("device", {}) or {})
        self._attr_native_value = _to_state(data.get(self._key))

        self._device_info_cache = DeviceInfo(
            identifiers={(DOMAIN, self._client_id)},
            via_device=(DOMAIN, self._entry_id),
            name=str(cache.get("name") or client_display_name(self._client_instance)),
            manufacturer=_safe_text(device_data.get("vendor_name")),
            model=_safe_text(device_data.get("model_name")),
            sw_version=_safe_text(device_data.get("firmware_revision")),
            hw_version=_safe_text(device_data.get("hardware_revision")),
            serial_number=_safe_text(device_data.get("serial_number")),
        )
        self.async_write_ha_state()


class BacnetClientPointSensor(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._client_id = client_id
        self._client_instance = int(client_instance)
        self._point_key = str(point_key)
        self._unsub_dispatcher: Callable[[], None] | None = None
        self._cov_context: Any | None = None
        self._cov_task: asyncio.Task | None = None
        self._cov_registered = False
        self._attr_native_value: StateType = None
        self._attr_native_unit_of_measurement: str | None = None
        self._attr_device_class: SensorDeviceClass | None = None
        self._attr_state_class: SensorStateClass | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

        cache = _client_points_get(hass, entry_id, client_id).get(self._point_key, {})
        type_slug = str(cache.get("type_slug") or "point")
        object_instance = _to_int(cache.get("object_instance")) or 0
        self._attr_unique_id = _point_unique_id(entry_id, client_id, type_slug, object_instance)
        self.entity_id = _point_entity_id(self._client_instance, type_slug, object_instance)
        description = _safe_text(cache.get("description"))
        object_name = _safe_text(cache.get("object_name"))
        self._attr_name = str(description or object_name or f"{type_slug.upper()} {object_instance}")

    @property
    def device_info(self) -> DeviceInfo:
        diag_cache = _client_cache_get(self.hass, self._entry_id, self._client_id)
        device_data = dict(diag_cache.get("device", {}) or {})
        return DeviceInfo(
            identifiers={(DOMAIN, self._client_id)},
            via_device=(DOMAIN, self._entry_id),
            name=str(diag_cache.get("name") or client_display_name(self._client_instance)),
            manufacturer=_safe_text(device_data.get("vendor_name")),
            model=_safe_text(device_data.get("model_name")),
            sw_version=_safe_text(device_data.get("firmware_revision")),
            hw_version=_safe_text(device_data.get("hardware_revision")),
            serial_number=_safe_text(device_data.get("serial_number")),
        )

    async def async_added_to_hass(self) -> None:
        signal = _client_points_signal(self._entry_id, self._client_id)
        self._unsub_dispatcher = async_dispatcher_connect(
            self.hass,
            signal,
            self._handle_points_update,
        )
        self._handle_points_update()
        await self._async_register_cov()
        self._handle_points_update()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_dispatcher is not None:
            self._unsub_dispatcher()
            self._unsub_dispatcher = None
        if self._cov_task is not None and not self._cov_task.done():
            self._cov_task.cancel()
            try:
                await self._cov_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._cov_task = None
        if self._cov_context is not None:
            try:
                await self._cov_context.__aexit__(None, None, None)
            except Exception:
                pass
        self._cov_context = None
        self._cov_registered = False

    async def _async_register_cov(self) -> None:
        point = _client_points_get(self.hass, self._entry_id, self._client_id).get(self._point_key, {})
        object_identifier = _safe_text(point.get("object_identifier"))
        address = _safe_text(point.get("client_address"))
        if not object_identifier or not address:
            return

        server = self.hass.data.get(DOMAIN, {}).get("servers", {}).get(self._entry_id)
        app = getattr(server, "app", None) if server is not None else None
        if app is None:
            return

        cov_factory = getattr(app, "change_of_value", None)
        if not callable(cov_factory):
            self._cov_registered = False
            return

        process_id = _cov_process_identifier(self._entry_id, self._client_id, self._point_key)
        if self._cov_context is not None:
            try:
                await self._cov_context.__aexit__(None, None, None)
            except Exception:
                pass
            self._cov_context = None

        try:
            context = cov_factory(
                Address(address),
                ObjectIdentifier(object_identifier),
                subscriber_process_identifier=process_id,
                issue_confirmed_notifications=False,
                lifetime=CLIENT_COV_LEASE_SECONDS,
            )
            self._cov_context = await context.__aenter__()
        except Exception:
            self._cov_context = None
            self._cov_registered = False
            _LOGGER.debug(
                "COV subscribe failed for %s (%s)",
                object_identifier,
                address,
                exc_info=True,
            )
            return

        self._cov_registered = True
        if self._cov_task is None or self._cov_task.done():
            self._cov_task = self.hass.async_create_task(self._async_cov_receive_loop())

    async def _async_cov_receive_loop(self) -> None:
        while True:
            context = self._cov_context
            if context is None:
                return
            try:
                prop, value = await context.get_value()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._cov_registered = False
                self._handle_points_update()
                _LOGGER.debug(
                    "COV receive loop failed for %s",
                    self._point_key,
                    exc_info=True,
                )
                return

            key = _property_slug(prop)
            if not key:
                continue
            if key not in {
                "presentvalue",
                "statusflags",
                "outofservice",
                "reliability",
                "description",
                "objectname",
                "statetext",
                "activetext",
                "inactivetext",
            }:
                continue

            point = dict(
                _client_points_get(self.hass, self._entry_id, self._client_id).get(self._point_key, {}) or {}
            )
            if not point:
                continue

            if key == "presentvalue":
                point["present_value"] = value
            elif key == "statusflags":
                point["status_flags"] = _safe_text(value)
            elif key == "outofservice":
                point["out_of_service"] = value
            elif key == "reliability":
                point["reliability"] = _safe_text(value)
            elif key == "description":
                point["description"] = _safe_text(value)
            elif key == "objectname":
                point["object_name"] = _safe_text(value)
            elif key == "statetext":
                if isinstance(value, (list, tuple)):
                    point["state_text"] = [str(item) for item in value]
                else:
                    try:
                        point["state_text"] = [str(item) for item in list(value)]
                    except Exception:
                        pass
            elif key == "activetext":
                point["active_text"] = _safe_text(value)
            elif key == "inactivetext":
                point["inactive_text"] = _safe_text(value)

            _client_points_set(
                self.hass,
                self._entry_id,
                self._client_id,
                {self._point_key: point},
            )
            async_dispatcher_send(
                self.hass,
                _client_points_signal(self._entry_id, self._client_id),
            )

    @callback
    def _handle_points_update(self) -> None:
        point = dict(_client_points_get(self.hass, self._entry_id, self._client_id).get(self._point_key, {}) or {})
        if not point:
            return

        description = _safe_text(point.get("description"))
        object_name = _safe_text(point.get("object_name"))
        if description:
            self._attr_name = description
        elif object_name:
            self._attr_name = object_name
        self._attr_native_unit_of_measurement = _safe_text(point.get("unit"))
        self._attr_device_class = _sensor_device_class_from_unit(self._attr_native_unit_of_measurement)
        native_value = _point_native_value_from_payload(point)
        self._attr_state_class = None
        if str(point.get("type_slug") or "") in {"ai", "av"} and isinstance(
            native_value,
            (int, float),
        ):
            self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_value = native_value
        self._attr_extra_state_attributes = {}
        self.async_write_ha_state()
