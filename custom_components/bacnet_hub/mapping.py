from __future__ import annotations
from dataclasses import dataclass
from homeassistant.core import HomeAssistant

@dataclass
class Mapping:
    entity_id: str
    object_type: str
    instance: int
    units: str | None = None
    writable: bool = False
    mode: str = "state"  # "state" | "attr"
    attr: str | None = None
    name: str | None = None
    write: dict | None = None

    @property
    def is_analog(self) -> bool:
        return self.object_type in ("analogInput","analogValue","analogOutput")

    async def read_from_ha(self, hass: HomeAssistant):
        state = hass.states.get(self.entity_id)
        if not state:
            return None
        val = state.state if self.mode == "state" else state.attributes.get(self.attr)
        if self.is_analog:
            try:
                return float(val)
            except Exception:
                return 0.0
        return str(val).lower() in ("on","true","1")

    async def write_to_ha(self, hass: HomeAssistant, value, priority=None):
        if not self.writable:
            return
        # Default write mapping
        service = (self.write or {}).get("service")

        if service == "switch.turn_on_off":
            domain = "switch"
            name = "turn_on" if str(value).lower() in ("1","true","on","active") else "turn_off"
            await hass.services.async_call(domain, name, {"entity_id": self.entity_id}, blocking=True)
            return

        if service == "climate.set_temperature":
            field = (self.write or {}).get("field", "temperature")
            try:
                temp = float(value)
            except Exception:
                return
            await hass.services.async_call("climate", "set_temperature",
                                           {"entity_id": self.entity_id, field: temp},
                                           blocking=True)
            return

        # Generic "domain.service" passthrough
        if service and "." in service:
            domain, svc = service.split(".", 1)
            data = {"entity_id": self.entity_id}
            payload_key = (self.write or {}).get("payload_key")
            if payload_key is not None:
                data[payload_key] = value
            await hass.services.async_call(domain, svc, data, blocking=True)
