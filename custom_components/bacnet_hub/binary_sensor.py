# custom_components/bacnet_hub/binary_sensor.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
    # STATE_ON nutzen wir für saubere on/off-Erkennung
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
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
        friendly = m.get("friendly_name")
        name = f"(BV-{instance}) {friendly}"
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
    """BACnet veröffentlichter Binary-Sensor, der Metadaten 1:1 von der Quelle spiegelt."""

    _attr_should_poll = False

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
            manufacturer="magliaral",
            model="BACpypes 3",
        )
        # werden dynamisch gefüllt
        self._attr_device_class: Optional[BinarySensorDeviceClass] = None
        self._attr_icon: Optional[str] = None
        self._attr_entity_category: Optional[EntityCategory] = None

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
            self._attr_device_class = None
            self._attr_icon = None
            self._attr_entity_category = None
            self.async_write_ha_state()
            return

        # Domain der Quelle (z.B. "light", "switch", ...)
        domain = st.entity_id.split(".", 1)[0]

        # Name anpassen
        src_name = st.name or self._source
        self._attr_name = f"(BACnet BV-{self._instance}) {src_name}"

        # Zustand exakt übernehmen (on/off)
        self._attr_is_on = (st.state == STATE_ON)

        # device_class exakt spiegeln, sofern vorhanden und gültig
        self._attr_device_class = None
        src_dc = st.attributes.get("device_class")
        if isinstance(src_dc, str) and src_dc:
            try:
                self._attr_device_class = BinarySensorDeviceClass(src_dc)
            except ValueError:
                self._attr_device_class = None

        # Fallback für einfache bool-Domains, wenn keine device_class existiert
        if not self._attr_device_class:
            if domain in ("switch", "input_boolean"):
                self._attr_device_class = BinarySensorDeviceClass.POWER

        # ---- ICON-LOGIK ------------------------------------------------------
        # 1) Icon der Quelle 1:1 übernehmen (falls explizit gesetzt)
        src_icon = st.attributes.get("icon")
        if src_icon:
            self._attr_icon = src_icon
        else:
            # 2) Fallback: Quelle ist light -> Glühbirne je nach Zustand
            if domain == "light":
                self._attr_icon = "mdi:lightbulb" if self._attr_is_on else "mdi:lightbulb-outline"
            else:
                self._attr_icon = None
        # ---------------------------------------------------------------------

        # entity_category (optional) spiegeln
        src_cat = st.attributes.get("entity_category")
        if src_cat in ("config", "diagnostic"):
            self._attr_entity_category = EntityCategory(src_cat)
        else:
            self._attr_entity_category = None

        self.async_write_ha_state()
