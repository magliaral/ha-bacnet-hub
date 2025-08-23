# custom_components/bacnet_hub/sensor.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.typing import StateType

from .const import DOMAIN

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    data = hass.data[DOMAIN]
    published: List[Dict[str, Any]] = data.get("published", {}).get(entry.entry_id, []) or []

    entities: List[BacnetPublishedSensor] = []
    for m in published:
        if (m or {}).get("object_type") != "analogValue":
            continue
        ent_id = m.get("entity_id")
        if not ent_id:
            continue
        instance = int(m.get("instance", 0))
        writable = bool(m.get("writable", False))
        name = f"BACnet AV{instance}: {ent_id}"
        entities.append(
            BacnetPublishedSensor(
                hass=hass,
                entry_id=entry.entry_id,
                source_entity_id=ent_id,
                instance=instance,
                name=name,
                writable=writable,
            )
        )
    if entities:
        async_add_entities(entities)

class BacnetPublishedSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry_id: str, source_entity_id: str, instance: int, name: str, writable: bool):
        self.hass = hass
        self._entry_id = entry_id
        self._source = source_entity_id
        self._instance = instance
        self._attr_name = name
        self._attr_native_unit_of_measurement = None
        self._remove_listener = None
        self._attr_unique_id = f"{DOMAIN}:{entry_id}:av:{instance}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="BACnet Hub (Local Device)",
            manufacturer="magliaral",
            model="BACpypes 3 - JoelBender",
        )

    async def async_added_to_hass(self) -> None:
        # initial
        self._pull_from_source()
        # live updates
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

    @callback
    def _pull_from_source(self) -> None:
        st = self.hass.states.get(self._source)
        if not st:
            self._attr_native_value = None
            self._attr_native_unit_of_measurement = None
            self.async_write_ha_state()
            return
        # Name/Description aus Quelle übernehmen
        src_name = st.name or self._source
        self._attr_name = f"{src_name} (BACnet AV{self._instance})"
        # Einheit übernehmen
        unit = st.attributes.get("unit_of_measurement")
        self._attr_native_unit_of_measurement = unit
        # Wert robust nach float
        try:
            self._attr_native_value = float(st.state)
        except Exception:
            self._attr_native_value = None
        self.async_write_ha_state()
