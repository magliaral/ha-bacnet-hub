# custom_components/bacnet_hub/publisher.py
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from bacpypes3.app import Application
from bacpypes3.basetypes import BinaryPV, EngineeringUnits
from bacpypes3.local.analog import AnalogValueObject
from bacpypes3.local.binary import BinaryValueObject
from bacpypes3.local.multistate import MultiStateValueObject

from .discovery import mapping_friendly_name, mapping_source_key

_LOGGER = logging.getLogger(__name__)

SUPPORTED_TYPES = {"binaryValue", "analogValue", "multiStateValue"}

# ---------- Units Mapping ----------------------------------------------------

HA_UOM_TO_BACNET_ENUM_NAME: Dict[str, str] = {
    "\u00b0c": "degreesCelsius",
    "\u00b0f": "degreesFahrenheit",
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
    "m3/h": "cubicMetersPerHour",
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
    return k.replace(" c", "\u00b0c").replace("\u00b0 c", "\u00b0c").replace(" f", "\u00b0f").replace("\u00b0 f", "\u00b0f")


def _resolve_units(value: Optional[str]) -> Optional[Any]:
    if not value:
        return None

    try:
        return getattr(EngineeringUnits, value)
    except Exception:
        pass

    enum_name = HA_UOM_TO_BACNET_ENUM_NAME.get(_norm_uom_key(value))
    if not enum_name:
        return None

    try:
        return getattr(EngineeringUnits, enum_name)
    except Exception:
        return None


def _determine_cov_increment(unit: Optional[str]) -> float:
    if not unit:
        return 0.5

    unit_lower = _norm_uom_key(unit)
    if unit_lower in ("\u00b0c", "\u00b0f", "k"):
        return 0.2
    if unit_lower == "%":
        return 2.0
    if unit_lower in ("w", "kw"):
        return 5.0 if unit_lower == "w" else 0.1
    if unit_lower in ("v", "mv", "kv"):
        return 0.5 if unit_lower == "v" else (5.0 if unit_lower == "mv" else 0.01)
    if unit_lower in ("a", "ma", "ka"):
        return 0.1 if unit_lower == "a" else (1.0 if unit_lower == "ma" else 0.01)
    if unit_lower in ("pa", "kpa", "mbar", "bar"):
        return 10.0 if unit_lower == "pa" else 0.1
    if unit_lower == "lx":
        return 10.0
    if unit_lower == "ppm":
        return 50.0
    if unit_lower in ("wh", "kwh"):
        return 100.0 if unit_lower == "wh" else 0.1

    return 0.5


# ---------- Small Helpers ----------------------------------------------------


def _entity_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _object_name(entity_id: str, source_attr: Any) -> str:
    src = str(source_attr or "").strip()
    return entity_id if not src else f"{entity_id}.{src}"


def _truthy(x: Any) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return x != 0

    s = str(x).strip().lower()
    if not s:
        return False

    if "inactive" in s:
        return False
    if "active" in s:
        return True

    if s in ("0", "false", "off", "closed"):
        return False
    return s in ("1", "true", "on", "open", "heat", "cool", "heating", "cooling")


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _as_int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except Exception:
        return default


# ---------- Publisher --------------------------------------------------------


class BacnetPublisher:
    """
    Lightweight Publisher:
      - HA -> BACnet: initial + on-change via direct assignment (COV-friendly)
      - BACnet -> HA: Forwarding is called by HubApp (WP/WPM)
    """

    def __init__(self, hass: HomeAssistant, app: Application, mappings: List[Dict[str, Any]]):
        self.hass = hass
        self.app = app
        self._cfg = [
            m for m in (mappings or [])
            if isinstance(m, dict) and m.get("object_type") in SUPPORTED_TYPES
        ]

        self.by_source: Dict[str, Any] = {}
        self.map_by_source: Dict[str, Dict[str, Any]] = {}
        self.sources_by_entity: Dict[str, List[str]] = {}

        self.by_oid: Dict[Tuple[str, int], Any] = {}
        self.map_by_oid: Dict[Tuple[str, int], Dict[str, Any]] = {}

        self._ha_unsub: Optional[Callable[[], None]] = None

    # --- lifecycle ---

    async def start(self) -> None:
        for m in self._cfg:
            ent = str(m.get("entity_id") or "")
            if not ent:
                continue

            source_attr = m.get("source_attr")
            source_key = mapping_source_key(ent, source_attr)
            if source_key in self.by_source:
                _LOGGER.debug("Skipping duplicate source mapping for %s", source_key)
                continue

            friendly = str(m.get("friendly_name") or mapping_friendly_name(self.hass, m) or ent)
            obj_type = str(m.get("object_type") or "")
            inst = int(m.get("instance", 0))
            obj_name = _object_name(ent, source_attr)

            if obj_type == "binaryValue":
                obj = BinaryValueObject(
                    objectIdentifier=("binaryValue", inst),
                    objectName=obj_name,
                    presentValue=False,
                    description=friendly,
                )
                _LOGGER.debug("BinaryValue created for %s (COV enabled)", source_key)
            elif obj_type == "multiStateValue":
                states = [str(s).strip() for s in (m.get("mv_states") or []) if str(s).strip()]
                if len(states) < 2:
                    states = ["off", "on"]
                obj = MultiStateValueObject(
                    objectIdentifier=("multiStateValue", inst),
                    objectName=obj_name,
                    presentValue=1,
                    numberOfStates=len(states),
                    stateText=states,
                    description=friendly,
                )
                _LOGGER.debug("MultiStateValue created for %s with states=%s", source_key, states)
            else:
                obj = AnalogValueObject(
                    objectIdentifier=("analogValue", inst),
                    objectName=obj_name,
                    presentValue=0.0,
                    description=friendly,
                )

                unit = m.get("units")
                if not unit:
                    st = self.hass.states.get(ent)
                    if st:
                        unit = st.attributes.get("unit_of_measurement")

                eu = _resolve_units(unit) if unit else None
                if eu is not None:
                    try:
                        obj.units = eu  # type: ignore[attr-defined]
                    except Exception:
                        _LOGGER.debug("Failed to set units for %s (%r)", source_key, eu, exc_info=True)

                try:
                    cov_increment = m.get("cov_increment")
                    if cov_increment is not None:
                        obj.covIncrement = float(cov_increment)  # type: ignore[attr-defined]
                    else:
                        smart_increment = _determine_cov_increment(unit)
                        obj.covIncrement = smart_increment  # type: ignore[attr-defined]
                        _LOGGER.debug("covIncrement for %s: %s (unit=%s)", source_key, smart_increment, unit)
                except Exception:
                    _LOGGER.debug("Failed to set covIncrement for %s", source_key, exc_info=True)

            self.app.add_object(obj)
            oid = getattr(obj, "objectIdentifier", None)
            if not isinstance(oid, tuple) or len(oid) != 2:
                _LOGGER.warning("Unexpected objectIdentifier for %s: %r", source_key, oid)
                continue

            self.by_source[source_key] = obj
            self.map_by_source[source_key] = m
            self.sources_by_entity.setdefault(ent, []).append(source_key)
            self.by_oid[oid] = obj
            self.map_by_oid[oid] = m

            _LOGGER.info(
                "Published %s:%s <= %s (name=%r, desc=%r, units=%s)",
                oid[0],
                oid[1],
                source_key,
                getattr(obj, "objectName", None),
                getattr(obj, "description", None),
                getattr(obj, "units", None) if hasattr(obj, "units") else None,
            )

        await self._initial_sync()

        self._ha_unsub = async_track_state_change_event(
            self.hass,
            list(self.sources_by_entity.keys()),
            self._on_state_changed,
        )

        _LOGGER.info("BacnetPublisher running (%d mappings).", len(self.by_source))

    async def stop(self) -> None:
        if self._ha_unsub:
            try:
                self._ha_unsub()
            except Exception:
                pass
            self._ha_unsub = None

        self.by_source.clear()
        self.map_by_source.clear()
        self.sources_by_entity.clear()
        self.by_oid.clear()
        self.map_by_oid.clear()
        _LOGGER.info("BacnetPublisher stopped")

    async def update_descriptions(self) -> None:
        """Update BACnet object descriptions from current entity names."""
        for source_key, obj in self.by_source.items():
            mapping = self.map_by_source.get(source_key)
            if not mapping:
                continue

            new_friendly = mapping_friendly_name(self.hass, mapping)
            current_desc = getattr(obj, "description", None)
            if new_friendly == current_desc:
                continue

            try:
                obj.description = new_friendly
                _LOGGER.debug("Description updated for %s: %r -> %r", source_key, current_desc, new_friendly)
            except Exception as err:
                _LOGGER.debug("Could not update description for %s: %s", source_key, err)

    # --- HA -> BACnet --------------------------------------------------------

    def _source_value(self, state_obj: Any, mapping: Dict[str, Any]) -> Any:
        read_attr = str(mapping.get("read_attr") or "").strip()
        source_attr = str(mapping.get("source_attr") or "").strip()
        attr_name = read_attr or source_attr
        if attr_name and attr_name != "__state__":
            attrs = getattr(state_obj, "attributes", {}) or {}
            val = attrs.get(attr_name)
            # Backward compatibility: hvac_mode is typically the climate state, not an attribute.
            if val is None and attr_name == "hvac_mode":
                return getattr(state_obj, "state", None)
            return val
        return getattr(state_obj, "state", None)

    async def _initial_sync(self) -> None:
        for source_key, obj in self.by_source.items():
            mapping = self.map_by_source.get(source_key)
            if not mapping:
                continue

            ent = str(mapping.get("entity_id") or "")
            st = self.hass.states.get(ent)
            if not st:
                continue

            value = self._source_value(st, mapping)
            await self._apply_from_ha(obj, value, mapping)

    @callback
    async def _on_state_changed(self, event) -> None:
        if event.event_type != EVENT_STATE_CHANGED:
            return

        data = event.data or {}
        ent = data.get("entity_id")
        ns = data.get("new_state")
        if not ent or not ns:
            return

        for source_key in self.sources_by_entity.get(ent, []):
            obj = self.by_source.get(source_key)
            mapping = self.map_by_source.get(source_key)
            if not obj or not mapping:
                continue

            value = self._source_value(ns, mapping)
            asyncio.create_task(self._apply_from_ha(obj, value, mapping))

    async def _apply_from_ha(self, obj: Any, value: Any, mapping: Optional[Dict[str, Any]] = None) -> None:
        """Write source state/attribute to BACnet object presentValue."""
        oid = getattr(obj, "objectIdentifier", None)
        if not isinstance(oid, tuple) or len(oid) != 2:
            return

        object_type = str((mapping or {}).get("object_type") or "")

        if isinstance(obj, AnalogValueObject):
            if value is None:
                return
            desired: Any = _as_float(value)
        elif isinstance(obj, MultiStateValueObject) or object_type == "multiStateValue":
            if value is None:
                return
            states = [str(s).strip().lower() for s in ((mapping or {}).get("mv_states") or []) if str(s).strip()]
            if len(states) < 2:
                states = ["off", "on"]
            mode = str(value).strip().lower()
            desired = (states.index(mode) + 1) if mode in states else 1
        else:
            if (mapping or {}).get("write_action") == "climate_hvac_mode":
                on_mode = str((mapping or {}).get("hvac_on_mode") or "heat").strip().lower()
                on = str(value or "").strip().lower() == on_mode
            else:
                on = _truthy(value)
            desired = BinaryPV("active" if on else "inactive")

        _LOGGER.debug("HA->BACnet: %r source=%r -> desired=%r (%s)", oid, value, desired, type(desired).__name__)

        current = None
        try:
            current = getattr(obj, "presentValue", None)
            if isinstance(obj, AnalogValueObject):
                if current is not None and float(current) == float(desired):
                    return
            elif isinstance(obj, MultiStateValueObject) or object_type == "multiStateValue":
                if current is not None and int(current) == int(desired):
                    return
            else:
                if current == desired or str(current) == str(desired):
                    return
        except Exception:
            pass

        object.__setattr__(obj, "_ha_guard", True)
        try:
            if isinstance(obj, MultiStateValueObject) or object_type == "multiStateValue":
                obj.presentValue = int(desired)
            else:
                obj.presentValue = desired
            _LOGGER.debug("HA->BACnet(direct): %r PV=%r -> %r", oid, current, desired)
        except Exception as err:
            _LOGGER.error("HA->BACnet assignment failed %r: %s", oid, err, exc_info=True)
            raise
        finally:
            object.__setattr__(obj, "_ha_guard", False)

    # --- BACnet -> HA --------------------------------------------------------

    def _is_mapping_auto_writable(self, mapping: Dict[str, Any]) -> bool:
        """Slim write guard: only mapping intent + required HA service availability."""
        action = str(mapping.get("write_action") or "").strip()
        if action == "climate_hvac_mode":
            return self.hass.services.has_service("climate", "set_hvac_mode")
        if action == "climate_temperature":
            return self.hass.services.has_service("climate", "set_temperature")

        ent = str(mapping.get("entity_id") or "")
        domain = _entity_domain(ent)

        if domain in ("light", "switch", "fan", "group"):
            return self.hass.services.has_service(domain, "turn_on") and self.hass.services.has_service(domain, "turn_off")

        if domain == "cover":
            return self.hass.services.has_service("cover", "open_cover") and self.hass.services.has_service("cover", "close_cover")

        if domain in ("number", "input_number"):
            return self.hass.services.has_service(domain, "set_value")

        return False

    def is_mapping_writable(self, mapping: Dict[str, Any]) -> bool:
        """Public guard used by BACnet write handler before local PV updates."""
        return self._is_mapping_auto_writable(mapping)

    async def forward_to_ha_from_bacnet(self, mapping: Dict[str, Any], value: Any) -> None:
        """Called by HubApp after successful WriteProperty(/Multiple)."""
        ent = str(mapping.get("entity_id") or "")
        if not ent:
            return

        if not self._is_mapping_auto_writable(mapping):
            _LOGGER.debug("BACnet->HA write skipped for %s: auto-writable check failed", ent)
            return

        action = str(mapping.get("write_action") or "").strip()
        if action == "climate_hvac_mode":
            object_type = str(mapping.get("object_type") or "")
            if object_type == "multiStateValue":
                states = [str(s).strip().lower() for s in (mapping.get("mv_states") or []) if str(s).strip()]
                idx = _as_int(value, 0)
                if idx < 1 or idx > len(states):
                    _LOGGER.warning("BACnet->HA climate hvac mode index out of range for %s: %r", ent, value)
                    return
                hvac_mode = states[idx - 1]
            else:
                hvac_mode = str(mapping.get("hvac_on_mode") or "heat") if _truthy(value) else str(mapping.get("hvac_off_mode") or "off")

            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": ent, "hvac_mode": hvac_mode},
                blocking=False,
            )
            _LOGGER.info("BACnet->HA climate.set_hvac_mode %s", {"entity_id": ent, "hvac_mode": hvac_mode})
            return

        if action == "climate_temperature":
            temperature = _as_float(value)
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": ent, "temperature": temperature},
                blocking=False,
            )
            _LOGGER.info(
                "BACnet->HA climate.set_temperature %s",
                {"entity_id": ent, "temperature": temperature},
            )
            return

        domain = _entity_domain(ent)

        if domain in ("light", "switch", "fan", "group"):
            on = _truthy(value)
            await self.hass.services.async_call(
                domain,
                f"turn_{'on' if on else 'off'}",
                {"entity_id": ent},
                blocking=False,
            )
            _LOGGER.info("BACnet->HA %s.turn_%s %s", domain, "on" if on else "off", {"entity_id": ent})
            return

        if domain == "cover":
            service = "open_cover" if _truthy(value) else "close_cover"
            await self.hass.services.async_call("cover", service, {"entity_id": ent}, blocking=False)
            _LOGGER.info("BACnet->HA cover.%s %s", service, {"entity_id": ent})
            return

        if domain in ("number", "input_number"):
            val = _as_float(value)
            await self.hass.services.async_call(
                domain,
                "set_value",
                {"entity_id": ent, "value": val},
                blocking=False,
            )
            _LOGGER.info("BACnet->HA %s.set_value %s", domain, {"entity_id": ent, "value": val})
            return

        _LOGGER.debug("BACnet->HA write ignored for unsupported domain %s (%s)", domain, ent)
