# custom_components/bacnet_hub/binary_sensor.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Callable

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
# We use STATE_ON for clean on/off detection
from homeassistant.const import STATE_ON, EVENT_HOMEASSISTANT_STARTED
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
    """BACnet published binary sensor.
    - Mirrors state 1:1 from source entity (STATE_ON -> is_on).
    - device_class is mirrored if possible (fallback POWER for switch/input_boolean).
    - Icon is sensibly set for lights if not predefined.
    - entity_category is ALWAYS 'diagnostic'.
    """

    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry_id: str, source_entity_id: str, instance: int, name: str):
        self.hass = hass
        self._entry_id = entry_id
        self._source = source_entity_id
        self._instance = instance
        self._attr_name = name
        self._remove_listener = None
        self._late_unsub: Optional[Callable[[], None]] = None
        self._attr_unique_id = f"{DOMAIN}:{entry_id}:bv:{instance}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="BACnet Hub",
            manufacturer="magliaral",
            model="BACnet Hub",
        )
        # Will be filled dynamically
        self._attr_device_class: Optional[BinarySensorDeviceClass] = None
        self._attr_entity_category: Optional[EntityCategory] = EntityCategory.DIAGNOSTIC
        self._attr_icon: Optional[str] = None
        self._attr_is_on: Optional[bool] = None  # initially unknown

    async def async_added_to_hass(self) -> None:
        # Try immediately …
        self._pull_from_source()

        # … and if source doesn't exist at start, pull again after HA start
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

        @callback
        def _handle(evt):
            if evt.data.get("entity_id") != self._source:
                return
            self._pull_from_source()

        self._remove_listener = async_track_state_change_event(self.hass, [self._source], _handle)

    async def async_will_remove_from_hass(self) -> None:
        # state-change listener
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
            # Source (not yet) available: category stays diagnostic,
            # clear icon/DeviceClass, do NOT reset existing bool state to None.
            self._attr_device_class = None
            self._attr_icon = None
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self.async_write_ha_state()
            return

        # Domain of source (e.g. "light", "switch", ...)
        domain = st.entity_id.split(".", 1)[0]

        # Adjust name
        src_name = st.name or self._source
        self._attr_name = f"(BACnet BV-{self._instance}) {src_name}"

        # Mirror state exactly (on/off)
        self._attr_is_on = (st.state == STATE_ON)

        # Mirror device_class exactly if present and valid
        self._attr_device_class = None
        src_dc = st.attributes.get("device_class")
        if isinstance(src_dc, str) and src_dc:
            try:
                self._attr_device_class = BinarySensorDeviceClass(src_dc)
            except ValueError:
                self._attr_device_class = None

        # Fallback for simple bool domains when no device_class exists
        if not self._attr_device_class:
            if domain in ("switch", "input_boolean"):
                self._attr_device_class = BinarySensorDeviceClass.POWER

        # ---- ICON LOGIC ------------------------------------------------------
        # 1) Mirror icon from source 1:1 (if explicitly set)
        src_icon = st.attributes.get("icon")
        if src_icon:
            self._attr_icon = src_icon
        else:
            # 2) Fallback: source is light -> lightbulb depending on state
            if domain == "light":
                self._attr_icon = "mdi:lightbulb" if self._attr_is_on else "mdi:lightbulb-outline"
            else:
                self._attr_icon = None
        # ---------------------------------------------------------------------

        # Do NOT mirror entity_category anymore – always stays diagnostic
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

        self.async_write_ha_state()
