from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from bacpypes3.basetypes import EngineeringUnits
from bacpypes3.local.analog import AnalogValueObject
from bacpypes3.local.binary import BinaryValueObject
from bacpypes3.local.multistate import MultiStateValueObject
from homeassistant.core import HomeAssistant

from .publisher_common import object_name

_LOGGER = logging.getLogger(__name__)

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


def _norm_uom_key(value: str) -> str:
    key = value.strip().lower()
    return key.replace(" c", "\u00b0c").replace("\u00b0 c", "\u00b0c").replace(" f", "\u00b0f").replace("\u00b0 f", "\u00b0f")


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


def create_object(
    hass: HomeAssistant,
    mapping: Dict[str, Any],
    *,
    entity_id: str,
    source_attr: Any,
    friendly: str,
) -> Any:
    obj_type = str(mapping.get("object_type") or "")
    inst = int(mapping.get("instance", 0))
    obj_name = object_name(entity_id, source_attr)

    if obj_type == "binaryValue":
        obj = BinaryValueObject(
            objectIdentifier=("binaryValue", inst),
            objectName=obj_name,
            presentValue=False,
            description=friendly,
        )
        return obj

    if obj_type == "multiStateValue":
        states = [str(s).strip() for s in (mapping.get("mv_states") or []) if str(s).strip()]
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
        return obj

    obj = AnalogValueObject(
        objectIdentifier=("analogValue", inst),
        objectName=obj_name,
        presentValue=0.0,
        description=friendly,
    )

    unit = mapping.get("units")
    if not unit:
        st = hass.states.get(entity_id)
        if st:
            unit = st.attributes.get("unit_of_measurement")

    eu = _resolve_units(unit) if unit else None
    if eu is not None:
        try:
            obj.units = eu  # type: ignore[attr-defined]
        except Exception:
            _LOGGER.debug("Failed to set units for %s (%r)", entity_id, eu, exc_info=True)

    try:
        cov_increment = mapping.get("cov_increment")
        if cov_increment is not None:
            obj.covIncrement = float(cov_increment)  # type: ignore[attr-defined]
        else:
            obj.covIncrement = _determine_cov_increment(unit)  # type: ignore[attr-defined]
    except Exception:
        _LOGGER.debug("Failed to set covIncrement for %s", entity_id, exc_info=True)

    return obj

