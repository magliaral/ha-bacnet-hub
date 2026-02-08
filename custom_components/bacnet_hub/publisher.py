# custom_components/bacnet_hub/publisher.py
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.helpers.event import async_track_state_change_event

from bacpypes3.app import Application
from bacpypes3.local.binary import BinaryValueObject
from bacpypes3.local.analog import AnalogValueObject
from bacpypes3.basetypes import EngineeringUnits, BinaryPV
from bacpypes3.apdu import WritePropertyRequest
from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.primitivedata import Real

_LOGGER = logging.getLogger(__name__)

SUPPORTED_TYPES = {"binaryValue", "analogValue"}

# ---------- Units-Mapping ----------------------------------------------------

HA_UOM_TO_BACNET_ENUM_NAME: Dict[str, str] = {
    "°c": "degreesCelsius",
    "°f": "degreesFahrenheit",
    "k": "kelvin",
    "%": "percent",
    "w": "watts",
    "kw": "kilowatts",
    "v": "volts",
    "mv": "millivolts",
    "kv": "kilovolts",
    "a": "amperes",
    "ma": "milliamperes",
    "ka": "kiloamperes",
    "hz": "hertz",
    "khz": "kilohertz",
    "mhz": "megahertz",
    "pa": "pascals",
    "kpa": "kilopascals",
    "mbar": "millibars",
    "bar": "bars",
    "ppm": "partsPerMillion",
    "lx": "luxes",
    "m³/h": "cubicMetersPerHour",
    "m3/h": "cubicMetersPerHour",
    "m³/s": "cubicMetersPerSecond",
    "m3/s": "cubicMetersPerSecond",
    "l/min": "litersPerMinute",
    "l/h": "litersPerHour",
    "wh": "wattHours",
    "kwh": "kilowattHours",
    "m": "meters",
    "cm": "centimeters",
    "mm": "millimeters",
    "m/s": "metersPerSecond",
    "km/h": "kilometersPerHour",
}

def _norm_uom_key(s: str) -> str:
    k = s.strip().lower()
    return (
        k.replace(" c", "°c").replace("° c", "°c")
         .replace(" f", "°f").replace("° f", "°f")
    )

def _resolve_units(value: Optional[str]) -> Optional[Any]:
    if not value:
        return None
    # Direct enum name?
    try:
        return getattr(EngineeringUnits, value)
    except Exception:
        pass
    # Short form/HA UoM
    enum_name = HA_UOM_TO_BACNET_ENUM_NAME.get(_norm_uom_key(value))
    if not enum_name:
        return None
    try:
        return getattr(EngineeringUnits, enum_name)
    except Exception:
        return None

def _determine_cov_increment(unit: Optional[str]) -> float:
    """Determines appropriate covIncrement based on the unit."""
    if not unit:
        return 0.5  # Default for unknown units

    unit_lower = _norm_uom_key(unit)

    # Temperature: 0.2 degrees (finer than default)
    if unit_lower in ("°c", "°f", "k"):
        return 0.2

    # Relative humidity: 2%
    if unit_lower == "%":
        return 2.0

    # Power: higher thresholds
    if unit_lower in ("w", "kw"):
        return 5.0 if unit_lower == "w" else 0.1

    # Voltage: 0.5V
    if unit_lower in ("v", "mv", "kv"):
        return 0.5 if unit_lower == "v" else (5.0 if unit_lower == "mv" else 0.01)

    # Current: 0.1A
    if unit_lower in ("a", "ma", "ka"):
        return 0.1 if unit_lower == "a" else (1.0 if unit_lower == "ma" else 0.01)

    # Pressure: 10 Pa / 0.1 kPa / 0.1 mbar
    if unit_lower in ("pa", "kpa", "mbar", "bar"):
        return 10.0 if unit_lower == "pa" else 0.1

    # Illuminance: 10 lux
    if unit_lower == "lx":
        return 10.0

    # CO2/PPM: 50 ppm
    if unit_lower == "ppm":
        return 50.0

    # Energy: 0.1 kWh
    if unit_lower in ("wh", "kwh"):
        return 100.0 if unit_lower == "wh" else 0.1

    # Default for other units
    return 0.5

# ---------- Small Helpers ----------------------------------------------------

def _entity_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""

def _truthy(x: Any) -> bool:
    s = str(x).strip().lower()
    return s in ("1", "true", "on", "active", "open", "heat", "cool")

def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

# ---------- Publisher --------------------------------------------------------

class BacnetPublisher:
    """
    Lightweight Publisher:
      - HA → BACnet: initial + on-change via direct assignment (COV-friendly)
      - BACnet → HA: Forwarding is called by HubApp (WP/WPM)
    """

    def __init__(self, hass: HomeAssistant, app: Application, mappings: List[Dict[str, Any]]):
        self.hass = hass
        self.app = app
        self._cfg = [
            m for m in (mappings or [])
            if isinstance(m, dict) and m.get("object_type") in SUPPORTED_TYPES
        ]

        self.by_entity: Dict[str, Any] = {}
        self.by_oid: Dict[Tuple[str, int], Any] = {}
        self.map_by_oid: Dict[Tuple[str, int], Dict[str, Any]] = {}

        self._ha_unsub: Optional[Callable[[], None]] = None

    # --- lifecycle ---

    async def start(self) -> None:
        for m in self._cfg:
            ent = m["entity_id"]
            friendly = m.get("friendly_name") or ent
            obj_type = m["object_type"]
            inst = int(m.get("instance", 0))

            if obj_type == "binaryValue":
                obj = BinaryValueObject(
                    objectIdentifier=("binaryValue", inst),
                    objectName=ent,              # Object name = Entity ID
                    presentValue=False,
                    description=friendly,        # Description = Friendly Name
                )
                # BinaryValue explicitly marked as COV-capable
                # (no covIncrement needed, every change triggers COV)
                _LOGGER.debug("BinaryValue created for %s (COV enabled)", ent)
            else:
                obj = AnalogValueObject(
                    objectIdentifier=("analogValue", inst),
                    objectName=ent,
                    presentValue=0.0,
                    description=friendly,
                )
                # Units aus Mapping oder HA-State ziehen
                u = m.get("units")
                if not u:
                    st = self.hass.states.get(ent)
                    if st:
                        u = st.attributes.get("unit_of_measurement")
                eu = _resolve_units(u) if u else None
                if eu is not None:
                    try:
                        obj.units = eu  # type: ignore[attr-defined]
                    except Exception:
                        _LOGGER.debug("Failed to set units for %s (%r)", ent, eu, exc_info=True)
                # Set COV increment (makes AV COV-capable)
                try:
                    ci = m.get("cov_increment")
                    if ci is not None:
                        # Explicitly set value from mapping
                        obj.covIncrement = float(ci)  # type: ignore[attr-defined]
                    else:
                        # Unit-based smart default
                        smart_increment = _determine_cov_increment(u)
                        obj.covIncrement = smart_increment  # type: ignore[attr-defined]
                        _LOGGER.debug("covIncrement for %s: %s (unit=%s)", ent, smart_increment, u)
                except Exception:
                    _LOGGER.debug("Failed to set covIncrement for %s", ent, exc_info=True)

            # Register
            self.app.add_object(obj)
            oid = getattr(obj, "objectIdentifier", None)
            if not isinstance(oid, tuple) or len(oid) != 2:
                _LOGGER.warning("Unexpected objectIdentifier for %s: %r", ent, oid)
                continue

            self.by_entity[ent] = obj
            self.by_oid[oid] = obj
            self.map_by_oid[oid] = m

            _LOGGER.info(
                "Published %s:%s ⇐ %s (name=%r, desc=%r, units=%s)",
                oid[0], oid[1], ent, getattr(obj, "objectName", None), getattr(obj, "description", None),
                getattr(obj, "units", None) if hasattr(obj, "units") else None,
            )

        # Initial HA → BACnet (COV-safe via WP-Service)
        await self._initial_sync()

        # Live-Events HA → BACnet
        self._ha_unsub = async_track_state_change_event(
            self.hass, list(self.by_entity.keys()), self._on_state_changed
        )

        _LOGGER.info("BacnetPublisher running (%d mappings).", len(self.by_entity))

    async def stop(self) -> None:
        if self._ha_unsub:
            try:
                self._ha_unsub()
            except Exception:
                pass
            self._ha_unsub = None

        self.by_entity.clear()
        self.by_oid.clear()
        self.map_by_oid.clear()
        _LOGGER.info("BacnetPublisher stopped")

    async def update_descriptions(self) -> None:
        """Updates the description of all BACnet objects with current friendly_names."""
        for ent, obj in self.by_entity.items():
            st = self.hass.states.get(ent)
            if not st:
                continue

            # Get current friendly_name
            new_friendly = st.name or st.attributes.get("friendly_name") or ent

            # Check current description
            current_desc = getattr(obj, "description", None)

            if new_friendly != current_desc:
                try:
                    obj.description = new_friendly
                    _LOGGER.debug("Description updated for %s: %r -> %r",
                                ent, current_desc, new_friendly)
                except Exception as e:
                    _LOGGER.debug("Could not update description for %s: %s", ent, e)

    # --- HA → BACnet (via direct assignment) ---

    async def _initial_sync(self) -> None:
        for ent, obj in self.by_entity.items():
            st = self.hass.states.get(ent)
            if not st:
                continue
            await self._apply_from_ha(obj, st.state)

    @callback
    async def _on_state_changed(self, event) -> None:
        if event.event_type != EVENT_STATE_CHANGED:
            return
        data = event.data or {}
        ent = data.get("entity_id")
        obj = self.by_entity.get(ent)
        ns = data.get("new_state")
        if not obj or not ns:
            return
        asyncio.create_task(self._apply_from_ha(obj, ns.state))

    async def _apply_from_ha(self, obj: Any, value: Any) -> None:
        """
        Writes presentValue directly to the local object.
        Direct assignment automatically triggers the COV mechanism in bacpypes3.
        Avoids unnecessary writes when the value would not change.
        """
        oid = getattr(obj, "objectIdentifier", None)
        if not isinstance(oid, tuple) or len(oid) != 2:
            return

        # Determine target value
        if isinstance(obj, AnalogValueObject):
            v = _as_float(value)
            desired = v
        else:
            on = _truthy(value)
            desired = BinaryPV("active" if on else "inactive")

        # Debug logging: Show desired value
        _LOGGER.debug("HA->BACnet: %r State=%r -> desired=%r (type=%s)",
                      oid, value, desired, type(desired).__name__)

        # ✅ Early exit if no change
        try:
            current = getattr(obj, "presentValue", None)
            if isinstance(obj, AnalogValueObject):
                if current is not None and float(current) == float(desired):
                    _LOGGER.debug("HA->BACnet: %r Value unchanged (%r), skipping write", oid, current)
                    return
            else:
                if current == desired or str(current) == str(desired):
                    _LOGGER.debug("HA->BACnet: %r Value unchanged (%r), skipping write", oid, current)
                    return
        except Exception:
            pass

        # Echo-Guard (in case our own write loops back via App->HA later)
        object.__setattr__(obj, "_ha_guard", True)
        try:
            # Direct assignment triggers COV automatically (bacpypes3 standard)
            obj.presentValue = desired
            _LOGGER.debug("HA->BACnet(direct): %r PV=%r -> %r", oid, current, desired)
        except Exception as e:
            _LOGGER.error("HA->BACnet(assignment failed): %r Error: %s", oid, e, exc_info=True)
            raise
        finally:
            object.__setattr__(obj, "_ha_guard", False)

    # --- BACnet → HA (called by HubApp) ---

    async def forward_to_ha_from_bacnet(self, mapping: Dict[str, Any], value: Any) -> None:
        """
        Called by HubApp after successful WriteProperty(/Multiple).
        Executes the appropriate HA service operation.
        """
        if not mapping.get("writable", False):
            return

        ent = mapping["entity_id"]
        domain = _entity_domain(ent)

        if domain in ("light", "switch", "fan"):
            on = _truthy(value)
            await self.hass.services.async_call(
                domain, f"turn_{'on' if on else 'off'}", {"entity_id": ent}, blocking=False
            )
            _LOGGER.info("BACnet->HA %s.turn_%s %s", domain, "on" if on else "off", {"entity_id": ent})
            return

        if domain == "cover":
            svc = "open_cover" if _truthy(value) else "close_cover"
            await self.hass.services.async_call("cover", svc, {"entity_id": ent}, blocking=False)
            _LOGGER.info("BACnet->HA cover.%s %s", svc, {"entity_id": ent})
            return

        if domain in ("number", "input_number"):
            try:
                val = float(value)
            except Exception:
                val = 0.0
            await self.hass.services.async_call(
                domain, "set_value", {"entity_id": ent, "value": val}, blocking=False
            )
            _LOGGER.info("BACnet->HA %s.set_value %s", domain, {"entity_id": ent, "value": val})
