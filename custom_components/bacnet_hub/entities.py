# custom_components/bacnet_hub/entities.py
from __future__ import annotations

from typing import Any, Dict, List
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.components.sensor import SensorEntity

from .const import DOMAIN

class PublishedMappingsSensor(SensorEntity):
    _attr_icon = "mdi:format-list-bulleted"
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry_id: str, mappings: List[Dict[str, Any]]) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._mappings = list(mappings or [])
        self._attr_name = "Published mappings"
        self._attr_unique_id = f"{entry_id}-published-mappings"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
        )

    @property
    def native_value(self) -> int:
        return len(self._mappings)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        # Compact list for device page
        items = []
        for m in self._mappings:
            items.append({
                "object_type": m.get("object_type"),
                "instance": m.get("instance"),
                "entity_id": m.get("entity_id"),
                "name": self.hass.states.get(str(m.get("entity_id") or "")) and
                        self.hass.states.get(str(m.get("entity_id") or "")).attributes.get("friendly_name"),
                "units": m.get("units"),
            })
        return {"items": items}
