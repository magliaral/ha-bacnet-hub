from __future__ import annotations
import asyncio
import logging
from typing import Any, Dict, Tuple

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_OBJECTS
from .mapping import Mapping

_LOGGER = logging.getLogger(__name__)

"""
NOTE ABOUT bacpypes3:
---------------------
This file wires Home Assistant to a BACnet/IP device using bacpypes3.
bacpypes3 API has evolved across versions. This code aims to be close to
0.0.92+ and may require small name tweaks depending on the installed version.

Strategy:
- Start bacpypes3 Application (async) bound to address:port
- Create a DeviceObject with configured device_id/name
- Dynamically add AV/BV objects for each mapping in options["objects"]
- Hook ReadProperty/WriteProperty to HA state/services
- (Optional later) COV subscriptions by listening to HA state changes and
  notifying subscribers

If something fails in bacpypes3 init, we log an error but keep HA running.
"""

# Soft imports with runtime checks so HA doesn't crash on import time
try:
    # Common bacpypes3 imports (names may vary slightly by version)
    from bacpypes3.app import Application
    from bacpypes3.local.device import DeviceObject
    from bacpypes3.local.analog import AnalogValueObject
    from bacpypes3.local.binary import BinaryValueObject
    from bacpypes3.primitivedata import Real, Boolean, Null
    from bacpypes3.pdu import Address
except Exception as exc:  # pragma: no cover - import guard
    Application = None            # type: ignore[assignment]
    DeviceObject = None           # type: ignore[assignment]
    AnalogValueObject = None      # type: ignore[assignment]
    BinaryValueObject = None      # type: ignore[assignment]
    Real = float                  # type: ignore[assignment]
    Boolean = bool                # type: ignore[assignment]
    Null = type(None)             # type: ignore[assignment]
    Address = str                 # type: ignore[assignment]
    _LOGGER.warning("bacpypes3 not importable at load time: %s", exc)


ENGINEERING_UNITS_ENUM: Dict[str, int] = {
    # Populate as needed (BACnetEngineeringUnits)
    "degreesCelsius": 62,
    "percent": 98,
    "noUnits": 95,
}

SUPPORTED_TYPES = {"analogValue", "binaryValue"}


# ... imports bleiben ähnlich; entferne DEFAULT_* Imports

@dataclass
class Mapping:
    entity_id: str
    object_type: str
    instance: int
    units: Optional[str] = None
    writable: bool = False
    mode: str = "state"
    attr: Optional[str] = None
    name: Optional[str] = None
    write: Optional[Dict[str, Any]] = None
    read_value_map: Optional[Dict[Any, Any]] = None
    write_value_map: Optional[Dict[Any, Any]] = None


class BacnetHubServer:
    """Server, der vollständig aus einem ConfigEntry (Dict) konfiguriert wird."""

    def __init__(self, hass: HomeAssistant, config: Dict[str, Any]):
        self.hass = hass
        self.config = config  # <-- aus dem Config Flow
        self.mappings: List[Mapping] = []
        self.app = None
        self.device = None
        self._state_unsub = None
        self._stop_event: asyncio.Event | None = None
        self.entity_index: Dict[str, Any] = {}

    # ---------- config ----------
    def _parse_config(self) -> Dict[str, Any]:
        """Normalisiert das ConfigEntry in das frühere YAML-Strukturformat."""
        cfg: Dict[str, Any] = {
            "options": {
                "instance": int(self.config.get("instance") or 500000),
                "name": self.config.get("device_name") or "BACnet Hub",
            },
            "bacpypes": {"options": {}},
            "objects": [],
        }
        bp = cfg["bacpypes"]["options"]
        if self.config.get("address"):           bp["address"] = self.config["address"]
        if self.config.get("port") is not None:  bp["port"] = int(self.config["port"])
        if self.config.get("broadcastAddress"):  bp["broadcastAddress"] = self.config["broadcastAddress"]

        # objects: entweder als Liste in entry.data["objects"] oder als YAML/Text
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

    def _build_bacpypes_args(self, YAMLArgumentParser, SimpleArgumentParser):
        """Erzeugt args ausschließlich aus dem ConfigEntry – keine Dateien."""
        cfg = self._parse_config()
        # bevorzugt bacpypes.options → argv
        argv: List[str] = []
        opts = (cfg.get("bacpypes", {}) or {}).get("options", {}) or {}
        for k, v in opts.items():
            flag = f"--{str(k).replace('_','-')}"
            if isinstance(v, bool):
                if v:
                    argv.append(flag)
            elif isinstance(v, (list, tuple)):
                for item in v:
                    argv.extend([flag, str(item)])
            elif v is not None:
                argv.extend([flag, str(v)])
        # Device-Fallbacks
        dev = cfg.get("options", {}) or {}
        if dev.get("instance"):
            argv.extend(["--instance", str(int(dev["instance"]))])
        if dev.get("name"):
            # bacpypes braucht keinen Namen als arg, aber behalten für spätere Nutzung
            pass

        parser = SimpleArgumentParser()
        return parser.parse_args(argv)

    def _load_mappings(self):
        cfg = self._parse_config()
        objs = cfg.get("objects", []) or []
        self.mappings = [Mapping(**o) for o in objs if isinstance(o, dict)]
