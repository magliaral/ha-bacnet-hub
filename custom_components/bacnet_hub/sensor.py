# custom_components/bacnet_hub/sensor.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Callable

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
# StateType ist der zulässige Typ für native_value (str|int|float|None)
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
    """BACnet veröffentlichter Sensor, der Metadaten 1:1 von der Quelle spiegelt.

    - Zustand/Value wird aus der Quelle übernommen.
    - device_class/state_class werden, sofern gültig, gespiegelt.
    - Icon wird, falls gesetzt, gespiegelt.
    - entity_category ist IMMER 'diagnostic'.
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
            name="BACnet Hub (Local Device)",
            manufacturer="magliaral",
            model="BACnet Hub",
        )

        # dynamische Attribute
        self._attr_native_unit_of_measurement: Optional[str] = None
        self._attr_device_class: Optional[SensorDeviceClass] = None
        self._attr_state_class: Optional[SensorStateClass] = None
        self._attr_icon: Optional[str] = None
        self._attr_entity_category: Optional[EntityCategory] = EntityCategory.DIAGNOSTIC
        self._attr_native_value: Optional[StateType] = None

        # für evtl. spätere Schreibfunktion (NumberEntity) schon behalten
        self._writable = writable

    async def async_added_to_hass(self) -> None:
        # initial
        self._pull_from_source()

        # Falls Quelle beim Start noch nicht geladen ist, nach HA-Start erneut ziehen
        if not self.hass.states.get(self._source):
            @callback
            def _late_initial_pull(_):
                self._pull_from_source()
                # listen_once hat gefeuert -> weitere manuelle Abmeldung verhindern
                self._late_unsub = None

            # WICHTIG: unsubscribe NICHT über async_on_remove registrieren
            self._late_unsub = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, _late_initial_pull
            )

        # Live-Updates aus der Quelle
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

        # late-once listener nur abmelden, wenn er noch existiert
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
            # Quelle vorübergehend weg:
            # - Kategorie bleibt diagnostic
            # - Metadaten leeren
            # - letzten Wert NICHT zwangsweise auf None setzen (stabilere Kachel)
            self._attr_device_class = None
            self._attr_state_class = None
            self._attr_icon = None
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self.async_write_ha_state()
            return

        # Name aus Quelle übernehmen
        src_name = st.name or self._source
        friendly_name = st.attributes.get("friendly_name") or src_name
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

        # entity_category NICHT mehr spiegeln – bleibt immer diagnostic
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

        # Wert übernehmen:
        # - unknown/unavailable → None
        # - Wenn Einheit vorhanden oder numerische device_class → float versuchen
        # - sonst Rohwert (StateType) übernehmen
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
                    native_value = state  # kann str/int/float sein
            except Exception:
                native_value = None

        self._attr_native_value = native_value
        self.async_write_ha_state()
