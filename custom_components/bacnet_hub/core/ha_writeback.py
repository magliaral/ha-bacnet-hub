from __future__ import annotations

import logging
from typing import Any, Dict

from homeassistant.core import HomeAssistant

from .publisher_common import as_float, as_int, entity_domain, truthy

_LOGGER = logging.getLogger(__name__)


def is_mapping_auto_writable(hass: HomeAssistant, mapping: Dict[str, Any]) -> bool:
    """Slim write guard: only mapping intent + required HA service availability."""
    action = str(mapping.get("write_action") or "").strip()
    if action == "climate_hvac_mode":
        return hass.services.has_service("climate", "set_hvac_mode")
    if action == "climate_temperature":
        return hass.services.has_service("climate", "set_temperature")

    ent = str(mapping.get("entity_id") or "")
    domain = entity_domain(ent)

    if domain in ("light", "switch", "fan", "group"):
        return hass.services.has_service(domain, "turn_on") and hass.services.has_service(domain, "turn_off")

    if domain == "cover":
        return hass.services.has_service("cover", "open_cover") and hass.services.has_service("cover", "close_cover")

    if domain in ("number", "input_number"):
        return hass.services.has_service(domain, "set_value")

    return False


async def forward_to_ha_from_bacnet(hass: HomeAssistant, mapping: Dict[str, Any], value: Any) -> None:
    ent = str(mapping.get("entity_id") or "")
    if not ent:
        return

    if not is_mapping_auto_writable(hass, mapping):
        _LOGGER.debug("BACnet->HA write skipped for %s: auto-writable check failed", ent)
        return

    action = str(mapping.get("write_action") or "").strip()
    if action == "climate_hvac_mode":
        object_type = str(mapping.get("object_type") or "")
        if object_type == "multiStateValue":
            states = [str(s).strip().lower() for s in (mapping.get("mv_states") or []) if str(s).strip()]
            idx = as_int(value, 0)
            if idx < 1 or idx > len(states):
                _LOGGER.warning("BACnet->HA climate hvac mode index out of range for %s: %r", ent, value)
                return
            hvac_mode = states[idx - 1]
        else:
            hvac_mode = str(mapping.get("hvac_on_mode") or "heat") if truthy(value) else str(mapping.get("hvac_off_mode") or "off")

        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": ent, "hvac_mode": hvac_mode},
            blocking=False,
        )
        _LOGGER.info("BACnet->HA climate.set_hvac_mode %s", {"entity_id": ent, "hvac_mode": hvac_mode})
        return

    if action == "climate_temperature":
        temperature = as_float(value)
        await hass.services.async_call(
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

    domain = entity_domain(ent)

    if domain in ("light", "switch", "fan", "group"):
        on = truthy(value)
        await hass.services.async_call(
            domain,
            f"turn_{'on' if on else 'off'}",
            {"entity_id": ent},
            blocking=False,
        )
        _LOGGER.info("BACnet->HA %s.turn_%s %s", domain, "on" if on else "off", {"entity_id": ent})
        return

    if domain == "cover":
        service = "open_cover" if truthy(value) else "close_cover"
        await hass.services.async_call("cover", service, {"entity_id": ent}, blocking=False)
        _LOGGER.info("BACnet->HA cover.%s %s", service, {"entity_id": ent})
        return

    if domain in ("number", "input_number"):
        val = as_float(value)
        await hass.services.async_call(
            domain,
            "set_value",
            {"entity_id": ent, "value": val},
            blocking=False,
        )
        _LOGGER.info("BACnet->HA %s.set_value %s", domain, {"entity_id": ent, "value": val})
        return

    _LOGGER.debug("BACnet->HA write ignored for unsupported domain %s (%s)", domain, ent)

