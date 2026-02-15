from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    DOMAIN,
    mirrored_state_attributes,
    published_entity_id,
    published_observer_unique_id,
    published_suggested_object_id,
)


class BacnetPublishedBinarySensor(BinarySensorEntity):
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
        hvac_on_mode: str | None,
        is_config: bool = False,
    ):
        self.hass = hass
        self._entry_id = entry_id
        self._source = source_entity_id
        self._source_attr = str(source_attr or "").strip()
        self._read_attr = str(read_attr or "").strip()
        self._hvac_on_mode = str(hvac_on_mode or "heat").strip().lower()
        self._instance = instance
        self._attr_name = name
        self._remove_listener = None
        self._late_unsub: Optional[Callable[[], None]] = None
        self._attr_unique_id = published_observer_unique_id(
            hub_instance=hub_instance,
            hub_address=hub_address,
            object_type="binaryValue",
            object_instance=instance,
            entity_domain="binary_sensor",
        )
        self._suggested_object_id = published_suggested_object_id(
            "binaryValue",
            instance,
            hub_instance,
        )
        self.entity_id = published_entity_id(
            "binary_sensor",
            "binaryValue",
            instance,
            hub_instance,
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=hub_name,
            manufacturer="magliaral",
            model="BACnet Hub",
        )
        self._attr_device_class: Optional[BinarySensorDeviceClass] = None
        self._attr_icon: Optional[str] = None
        self._attr_is_on: Optional[bool] = None
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
            self._attr_icon = None
            self._attr_extra_state_attributes = {}
            self.async_write_ha_state()
            return

        domain = st.entity_id.split(".", 1)[0]
        src_name = st.name or self._source
        if self._source_attr:
            field_name = self._source_attr.replace("_", " ").title()
            src_name = f"{src_name} {field_name}"
        self._attr_name = f"(BACnet BV-{self._instance}) {src_name}"

        attr_name = self._read_attr or self._source_attr
        if attr_name and attr_name != "__state__":
            source_state = st.attributes.get(attr_name)
            if source_state is None and attr_name == "hvac_mode":
                source_state = st.state
        else:
            source_state = st.state
        source_text = str(source_state or "").strip().lower()
        if self._source_attr == "hvac_mode":
            self._attr_is_on = source_text == self._hvac_on_mode
        elif self._source_attr == "hvac_action":
            self._attr_is_on = bool(source_text) and source_text not in ("idle", "off")
        else:
            self._attr_is_on = source_text in (
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

        self._attr_device_class = None
        src_dc = st.attributes.get("device_class")
        if isinstance(src_dc, str) and src_dc:
            try:
                self._attr_device_class = BinarySensorDeviceClass(src_dc)
            except ValueError:
                self._attr_device_class = None

        if not self._attr_device_class and domain in ("switch", "input_boolean"):
            self._attr_device_class = BinarySensorDeviceClass.POWER

        src_icon = st.attributes.get("icon")
        if src_icon:
            self._attr_icon = src_icon
        else:
            if domain == "light":
                self._attr_icon = "mdi:lightbulb" if self._attr_is_on else "mdi:lightbulb-outline"
            else:
                self._attr_icon = None
        mirrored_attrs = mirrored_state_attributes(dict(st.attributes or {}))
        mirrored_attrs["source_entity_id"] = self._source
        self._attr_extra_state_attributes = mirrored_attrs
        self.async_write_ha_state()
