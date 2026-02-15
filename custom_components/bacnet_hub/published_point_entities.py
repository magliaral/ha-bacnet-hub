from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    DOMAIN,
    mirrored_state_attributes,
    published_entity_id,
    published_observer_unique_id,
    published_suggested_object_id,
)


def _source_value(state: Any, read_attr: str, source_attr: str) -> Any:
    attr_name = read_attr or source_attr
    if attr_name and attr_name != "__state__":
        return state.attributes.get(attr_name)
    return state.state


class BacnetPublishedNumberObserver(NumberEntity):
    _attr_should_poll = False
    _attr_mode = NumberMode.BOX

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
        is_config: bool = False,
    ) -> None:
        self.hass = hass
        self._source = source_entity_id
        self._source_attr = str(source_attr or "").strip()
        self._read_attr = str(read_attr or "").strip()
        self._configured_unit = configured_unit
        self._instance = int(instance)
        self._attr_name = name
        self._remove_listener: Callable[[], None] | None = None
        self._late_unsub: Optional[Callable[[], None]] = None
        self._attr_unique_id = published_observer_unique_id(
            hub_instance=hub_instance,
            hub_address=hub_address,
            object_type="analogValue",
            object_instance=self._instance,
            entity_domain="number",
        )
        self._suggested_object_id = published_suggested_object_id(
            "analogValue",
            self._instance,
            hub_instance,
        )
        self.entity_id = published_entity_id(
            "number",
            "analogValue",
            self._instance,
            hub_instance,
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=hub_name,
            manufacturer="magliaral",
            model="BACnet Hub",
        )
        self._attr_native_unit_of_measurement = configured_unit
        self._attr_native_value: float | None = None
        self._attr_extra_state_attributes: Dict[str, Any] = {}
        if is_config:
            self._attr_entity_category = EntityCategory.CONFIG

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
            self._remove_listener()
            self._remove_listener = None
        if self._late_unsub is not None:
            self._late_unsub()
            self._late_unsub = None

    @callback
    def _pull_from_source(self) -> None:
        st = self.hass.states.get(self._source)
        if not st:
            self._attr_extra_state_attributes = {}
            self.async_write_ha_state()
            return

        src_name = st.name or self._source
        if self._source_attr:
            src_name = f"{src_name} {self._source_attr.replace('_', ' ').title()}"
        self._attr_name = f"(BACnet AV-{self._instance}) {src_name}"

        raw = _source_value(st, self._read_attr, self._source_attr)
        try:
            self._attr_native_value = float(raw) if raw not in (None, "", "unknown", "unavailable") else None
        except Exception:
            self._attr_native_value = None

        self._attr_native_unit_of_measurement = (
            st.attributes.get("unit_of_measurement") or self._configured_unit
        )
        self._attr_icon = st.attributes.get("icon") or None
        mirrored_attrs = mirrored_state_attributes(dict(st.attributes or {}))
        mirrored_attrs["source_entity_id"] = self._source
        self._attr_extra_state_attributes = mirrored_attrs
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        raise HomeAssistantError("Read-only BACnet observer entity")


class BacnetPublishedSwitchObserver(SwitchEntity):
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
        hvac_on_mode: str | None = None,
        is_config: bool = False,
    ) -> None:
        self.hass = hass
        self._source = source_entity_id
        self._source_attr = str(source_attr or "").strip()
        self._read_attr = str(read_attr or "").strip()
        self._hvac_on_mode = str(hvac_on_mode or "heat").strip().lower()
        self._instance = int(instance)
        self._attr_name = name
        self._remove_listener: Callable[[], None] | None = None
        self._late_unsub: Optional[Callable[[], None]] = None
        self._attr_unique_id = published_observer_unique_id(
            hub_instance=hub_instance,
            hub_address=hub_address,
            object_type="binaryValue",
            object_instance=self._instance,
            entity_domain="switch",
        )
        self._suggested_object_id = published_suggested_object_id(
            "binaryValue",
            self._instance,
            hub_instance,
        )
        self.entity_id = published_entity_id(
            "switch",
            "binaryValue",
            self._instance,
            hub_instance,
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=hub_name,
            manufacturer="magliaral",
            model="BACnet Hub",
        )
        self._attr_is_on: bool | None = None
        self._attr_extra_state_attributes: Dict[str, Any] = {}
        if is_config:
            self._attr_entity_category = EntityCategory.CONFIG

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
            self._remove_listener()
            self._remove_listener = None
        if self._late_unsub is not None:
            self._late_unsub()
            self._late_unsub = None

    @callback
    def _pull_from_source(self) -> None:
        st = self.hass.states.get(self._source)
        if not st:
            self._attr_extra_state_attributes = {}
            self.async_write_ha_state()
            return

        src_name = st.name or self._source
        if self._source_attr:
            src_name = f"{src_name} {self._source_attr.replace('_', ' ').title()}"
        self._attr_name = f"(BACnet BV-{self._instance}) {src_name}"

        raw = _source_value(st, self._read_attr, self._source_attr)
        txt = str(raw or "").strip().lower()
        if self._source_attr == "hvac_mode":
            self._attr_is_on = txt == self._hvac_on_mode
        elif self._source_attr == "hvac_action":
            self._attr_is_on = bool(txt) and txt not in ("idle", "off")
        else:
            self._attr_is_on = txt in (
                STATE_ON,
                "on",
                "open",
                "true",
                "active",
                "heat",
                "cool",
                "heating",
                "cooling",
            )
        self._attr_icon = st.attributes.get("icon") or None
        mirrored_attrs = mirrored_state_attributes(dict(st.attributes or {}))
        mirrored_attrs["source_entity_id"] = self._source
        self._attr_extra_state_attributes = mirrored_attrs
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        raise HomeAssistantError("Read-only BACnet observer entity")

    async def async_turn_off(self, **kwargs: Any) -> None:
        raise HomeAssistantError("Read-only BACnet observer entity")


class BacnetPublishedSelectObserver(SelectEntity):
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
        options: list[str] | None,
        is_config: bool = False,
    ) -> None:
        self.hass = hass
        self._source = source_entity_id
        self._source_attr = str(source_attr or "").strip()
        self._read_attr = str(read_attr or "").strip()
        self._instance = int(instance)
        self._attr_name = name
        self._remove_listener: Callable[[], None] | None = None
        self._late_unsub: Optional[Callable[[], None]] = None
        self._attr_unique_id = published_observer_unique_id(
            hub_instance=hub_instance,
            hub_address=hub_address,
            object_type="multiStateValue",
            object_instance=self._instance,
            entity_domain="select",
        )
        self._suggested_object_id = published_suggested_object_id(
            "multiStateValue",
            self._instance,
            hub_instance,
        )
        self.entity_id = published_entity_id(
            "select",
            "multiStateValue",
            self._instance,
            hub_instance,
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=hub_name,
            manufacturer="magliaral",
            model="BACnet Hub",
        )
        self._attr_options = [str(item).strip() for item in (options or []) if str(item).strip()]
        self._attr_current_option: str | None = None
        self._attr_extra_state_attributes: Dict[str, Any] = {}
        if is_config:
            self._attr_entity_category = EntityCategory.CONFIG

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
            self._remove_listener()
            self._remove_listener = None
        if self._late_unsub is not None:
            self._late_unsub()
            self._late_unsub = None

    @callback
    def _pull_from_source(self) -> None:
        st = self.hass.states.get(self._source)
        if not st:
            self._attr_extra_state_attributes = {}
            self.async_write_ha_state()
            return

        src_name = st.name or self._source
        if self._source_attr:
            src_name = f"{src_name} {self._source_attr.replace('_', ' ').title()}"
        self._attr_name = f"(BACnet MV-{self._instance}) {src_name}"

        if not self._attr_options and self._source_attr == "hvac_mode":
            raw_modes = list(st.attributes.get("hvac_modes") or [])
            modes = [str(item).strip() for item in raw_modes if str(item).strip()]
            if modes:
                self._attr_options = modes

        raw = _source_value(st, self._read_attr, self._source_attr)
        current = None if raw is None else str(raw).strip()
        if current and self._attr_options:
            for opt in self._attr_options:
                if current.lower() == str(opt).strip().lower():
                    current = opt
                    break
        if current and self._attr_options and current not in self._attr_options:
            try:
                idx = int(current)
                if 1 <= idx <= len(self._attr_options):
                    current = self._attr_options[idx - 1]
            except Exception:
                current = None
        self._attr_current_option = current if current in self._attr_options else None

        self._attr_icon = st.attributes.get("icon") or None
        mirrored_attrs = mirrored_state_attributes(dict(st.attributes or {}))
        mirrored_attrs["source_entity_id"] = self._source
        self._attr_extra_state_attributes = mirrored_attrs
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        raise HomeAssistantError("Read-only BACnet observer entity")
