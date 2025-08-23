# custom_components/bacnet_hub/binary_sensor.py
from __future__ import annotations

from typing import Any, Dict, List

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    data = hass.data[DOMAIN]
    published: List[Dict[str, Any]] = data.get("published", {}).get(entry.entry_id, []) or []

    entities: List[BacnetPublishedBinarySensor] = []
    for m in published:
        if (m or {}).get("object_type") != "binaryValue":
            continue
        ent_id = m.get("entity_id")
        if not ent_id:
            continue
        instance = int(m.get("instance", 0))
        name = f"BACnet BV{instance}: {ent_id}"
        entities.append(
            BacnetPublishedBinarySensor(
                hass=hass,
                entry_id=entry.entry_id,
                source_entity_id=ent_id,
                instance=instance,
                name=name,
            )
        )
    if entities:
        async_add_entities(entities)

class BacnetPublishedBinarySensor(BinarySensorEntity):
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.POWER  # generisch; HA zeigt hübsches Icon

    def __init__(self, hass: HomeAssistant, entry_id: str, source_entity_id: str, instance: int, name: str):
        self.hass = hass
        self._entry_id = entry_id
        self._source = source_entity_id
        self._instance = instance
        self._attr_name = name
        self._remove_listener = None
        self._attr_unique_id = f"{DOMAIN}:{entry_id}:bv:{instance}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="BACnet Hub (Local Device)",
            manufacturer="Home Assistant",
            model="BACpypes 3",
        )

    async def async_added_to_hass(self) -> None:
        self._pull_from_source()
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
            self._attr_is_on = None
            self.async_write_ha_state()
            return
        src_name = st.name or self._source
        self._attr_name = f"{src_name} (BACnet BV{self._instance})"
        self._attr_is_on = str(st.state).lower() in ("on", "true", "1", "open", "heat", "cool")
        self.async_write_ha_state()
