from __future__ import annotations
import asyncio
import logging
import types as _types
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import yaml
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.const import EVENT_STATE_CHANGED

_LOGGER = logging.getLogger(__name__)

ENGINEERING_UNITS_ENUM = {"degreesCelsius": 62, "percent": 98, "noUnits": 95}
SUPPORTED_TYPES = {"analogValue", "binaryValue"}


@dataclass
class Mapping:
    entity_id: str
    object_type: str
    instance: int
    units: Optional[str] = None
    writable: bool = False
    mode: str = "state"       # state | attr
    attr: Optional[str] = None
    name: Optional[str] = None
    write: Optional[Dict[str, Any]] = None
    read_value_map: Optional[Dict[Any, Any]] = None
    write_value_map: Optional[Dict[Any, Any]] = None


class BacnetHubServer:
    """BACnet-Server, vollständig per ConfigEntry konfiguriert."""

    def __init__(self, hass: HomeAssistant, config: Dict[str, Any]):
        self.hass = hass
        self.config = config
        self.mappings: List[Mapping] = []
        self.app = None
        self.device = None
        self._state_unsub = None
        self._stop_event: asyncio.Event | None = None
        self.entity_index: Dict[str, Any] = {}

    # -------- Config einlesen/normalisieren --------
    def _parse_config(self) -> Dict[str, Any]:
        """Normalisiert das ConfigEntry in die frühere YAML-Struktur."""
        cfg: Dict[str, Any] = {
            "options": {
                "instance": int(self.config.get("instance") or 500000),
                "name": self.config.get("device_name") or "BACnet Hub",
            },
            "bacpypes": {"options": {}},
            "objects": [],
        }
        bp = cfg["bacpypes"]["options"]

        def _norm_addr(addr: str | None) -> Optional[str]:
            if not addr:
                return None
            addr = addr.strip()
            # bacpypes erwartet i.d.R. <ip>/<prefix>
            if "/" not in addr and addr != "0.0.0.0":
                return f"{addr}/24"        # sane default
            return addr

        if self.config.get("address"):
            bp["address"] = _norm_addr(self.config.get("address"))
        if self.config.get("port") is not None:
            bp["port"] = int(self.config["port"])
        if self.config.get("broadcastAddress"):
            bp["broadcastAddress"] = self.config["broadcastAddress"]

        # Objects als YAML oder Liste
        if isinstance(self.config.get("objects"), list):
            cfg["objects"] = self.config["objects"]
        else:
            text = (self.config.get("objects_yaml") or "").strip()
            if text:
                try:
                    loaded = yaml.safe_load(text)
                    if isinstance(loaded, list):
                        cfg["objects"] = loaded
                    elif isinstance(loaded, dict) and "objects" in loaded:
                        cfg["objects"] = loaded.get("objects") or []
                except Exception as e:
                    _LOGGER.error("Konnte objects_yaml nicht parsen: %r", e)

        return cfg

    def _load_mappings(self):
        cfg = self._parse_config()
        objs = cfg.get("objects", []) or []
        self.mappings = [Mapping(**o) for o in objs if isinstance(o, dict)]

    # -------- HA helpers --------
    def _ha_get_value(self, entity_id: str
