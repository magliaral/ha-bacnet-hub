# custom_components/bacnet_hub/sensor.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
    # StateType ist der zulässige Typ für native_value (str|int|float|None)
from homeassistant.helpers.typing import StateType
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.event import async_track_state_change_event

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
        friendly = m.get("friendly_name")
        name = f"(AV-{instance}) {friendly}"
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
    """BACnet veröffentlichter Sensor, der Metadaten 1:1 von der Quelle spiegelt."""

    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry_id: str, source_entity_id: str, instance: int, name: str, writable: bool):
        self.hass = hass
        self._entry_id = entry_id
        self._source = source_entity_id
        self._instance = instance
        self._attr_name = name
        self._remove_listener = None
        self._attr_unique_id = f"{DOMAIN}:{entry_id}:av:{instance}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="BACnet Hub (Local Device)",
            manufacturer="magliaral",
            model="BACpypes 3 - JoelBender",
        )

        # dynamische Attribute
        self._attr_native_unit_of_measurement: Optional[str] = None
        self._attr_device_class: Optional[SensorDeviceClass] = None
        self._attr_state_class: Optional[SensorStateClass] = None
        self._attr_icon: Optional[str] = None
        self._attr_entity_category: Optional[EntityCategory] = None
        # Hinweis: writable wird hier nicht genutzt; beschreibbare AVs wären als NumberEntity sinnvoll.

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
            self._attr_device_class = None
            self._attr_state_class = None
            self._attr_icon = None
            self._attr_entity_category = None
            self.async_write_ha_state()
            return

        # Name/Description aus Quelle übernehmen
        src_name = st.name or self._source
        friendly_name = str(st.attributes.get("friendly_name"))
        self._attr_name = f"(BACnet AV-{self._instance}) {friendly_name}"

        # Unit exakt spiegeln
        unit = st.attributes.get("unit_of_measurement")
        self._attr_native_unit_of_measurement = unit

        # device_class/state_class exakt übernehmen, wenn vorhanden/valide
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

        # Icon exakt übernehmen, falls explizit gesetzt
        self._attr_icon = st.attributes.get("icon") or None

        # entity_category (optional) spiegeln
        src_cat = st.attributes.get("entity_category")
        if src_cat in ("config", "diagnostic"):
            self._attr_entity_category = EntityCategory(src_cat)
        else:
            self._attr_entity_category = None

        # Wert übernehmen:
        # - Wenn Einheit vorhanden oder device_class numerisch → float versuchen
        # - sonst Rohwert (StateType) übernehmen
        native_value: StateType
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
                native_value = float(st.state)  # type: ignore[assignment]
            else:
                native_value = st.state  # kann str/int/float sein
        except Exception:
            native_value = None

        self._attr_native_value = native_value
        self.async_write_ha_state()
