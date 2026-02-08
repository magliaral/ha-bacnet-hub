# custom_components/bacnet_hub/sensor.py
from __future__ import annotations

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
    """BACnet published sensor that mirrors metadata 1:1 from the source.

    - State/Value is taken from the source.
    - device_class/state_class are mirrored if valid.
    - Icon is mirrored if set.
    - entity_category is ALWAYS 'diagnostic'.
    """

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        source_entity_id: str,
        instance: int,
        name: str,
        writable: bool,
    ):
        self.hass = hass
        self._entry_id = entry_id
        self._source = source_entity_id
        self._instance = instance
        self._attr_name = name
        self._remove_listener = None
        self._late_unsub: Optional[Callable[[], None]] = None
        self._attr_unique_id = f"{DOMAIN}:{entry_id}:av:{instance}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="BACnet Hub",
            manufacturer="magliaral",
            model="BACnet Hub",
        )

        # dynamic attributes
        self._attr_native_unit_of_measurement: Optional[str] = None
        self._attr_device_class: Optional[SensorDeviceClass] = None
        self._attr_state_class: Optional[SensorStateClass] = None
        self._attr_icon: Optional[str] = None
        self._attr_entity_category: Optional[EntityCategory] = EntityCategory.DIAGNOSTIC
        self._attr_native_value: Optional[StateType] = None

        # Keep for possible future write function (NumberEntity)
        self._writable = writable

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
            # - Category stays diagnostic
            # - Clear metadata
            # - Do NOT forcibly set last value to None (more stable tile)
            self._attr_device_class = None
            self._attr_state_class = None
            self._attr_icon = None
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self.async_write_ha_state()
            return

        # Take name from source
        src_name = st.name or self._source
        friendly_name = st.attributes.get("friendly_name") or src_name
        self._attr_name = f"(BACnet AV-{self._instance}) {friendly_name}"

        # Mirror unit exactly
        unit = st.attributes.get("unit_of_measurement")
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

        # Do NOT mirror entity_category anymore – always stays diagnostic
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

        # Take value:
        # - unknown/unavailable → None
        # - If unit present or numeric device_class → try float
        # - otherwise take raw value (StateType)
        state = st.state
        if state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
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
