from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from bacpypes3.basetypes import BinaryPV
from bacpypes3.local.analog import AnalogValueObject
from bacpypes3.local.multistate import MultiStateValueObject

from .publisher_common import as_float, truthy

_LOGGER = logging.getLogger(__name__)


def source_value(state_obj: Any, mapping: Dict[str, Any]) -> Any:
    read_attr = str(mapping.get("read_attr") or "").strip()
    source_attr = str(mapping.get("source_attr") or "").strip()
    attr_name = read_attr or source_attr
    if attr_name and attr_name != "__state__":
        attrs = getattr(state_obj, "attributes", {}) or {}
        value = attrs.get(attr_name)
        if value is None and attr_name == "hvac_mode":
            return getattr(state_obj, "state", None)
        return value
    return getattr(state_obj, "state", None)


async def apply_from_ha(obj: Any, value: Any, mapping: Optional[Dict[str, Any]] = None) -> None:
    """Write source state/attribute to BACnet object presentValue."""
    oid = getattr(obj, "objectIdentifier", None)
    if not isinstance(oid, tuple) or len(oid) != 2:
        return

    object_type = str((mapping or {}).get("object_type") or "")

    if isinstance(obj, AnalogValueObject):
        if value is None:
            return
        desired: Any = as_float(value)
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
            on = truthy(value)
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

