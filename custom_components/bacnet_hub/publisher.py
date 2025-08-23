# custom_components/bacnet_hub/publisher.py
from __future__ import annotations

import asyncio
import logging
from types import MethodType
from typing import Any, Dict, List, Optional, Tuple, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.helpers.event import async_track_state_change_event

from bacpypes3.app import Application
from bacpypes3.local.binary import BinaryValueObject
from bacpypes3.local.analog import AnalogValueObject
from bacpypes3.basetypes import EngineeringUnits

_LOGGER = logging.getLogger(__name__)

SUPPORTED_TYPES = {"binaryValue", "analogValue"}
WATCH_INTERVAL_SEC = 0.5  # Fallback (kann man unten easy deaktivieren)

# -------- Units-Mapping -----------------------------------------------------

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
    return (k.replace(" c", "°c").replace("° c", "°c")
             .replace(" f", "°f").replace("° f", "°f"))

def _resolve_units(value: Optional[str]) -> Optional[Any]:
    if not value or EngineeringUnits is None:
        return None
    # direkter Enumname?
    try:
        return getattr(EngineeringUnits, value)
    except Exception:
        pass
    # Kurzform/HA-UoM
    enum_name = HA_UOM_TO_BACNET_ENUM_NAME.get(_norm_uom_key(value))
    if not enum_name:
        return None
    try:
        return getattr(EngineeringUnits, enum_name)
    except Exception:
        return None

# -------- kleine Helfer -----------------------------------------------------

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

# -------- Publisher ---------------------------------------------------------

class BacnetPublisher:
    """
    Minimal & robust:
      - HA → BACnet: Initialsync + state_changed
      - BACnet → HA: __setattr__ Hook auf presentValue (+ optionaler Watcher)
    """

    def __init__(self, hass: HomeAssistant, app: Application, mappings: List[Dict[str, Any]]):
        self.hass = hass
        self.app = app
        self._cfg = [m for m in (mappings or []) if isinstance(m, dict) and m.get("object_type") in SUPPORTED_TYPES]

        self.by_entity: Dict[str, Any] = {}
        self.by_oid: Dict[Tuple[str, int], Any] = {}
        self.map_by_oid: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self._last: Dict[Tuple[str, int], Any] = {}

        self._ha_unsub: Optional[Callable[[], None]] = None
        self._watch_task: Optional[asyncio.Task] = None
        self._enable_watcher: bool = True   # bei Bedarf auf False setzen

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
                    objectName=ent,              # Objektname = Entity ID
                    presentValue=False,
                    description=friendly,        # Beschreibung = Friendly Name
                )
            else:
                obj = AnalogValueObject(
                    objectIdentifier=("analogValue", inst),
                    objectName=ent,
                    presentValue=0.0,
                    description=friendly,
                )
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
                        _LOGGER.debug("units setzen fehlgeschlagen für %s (%r)", ent, eu, exc_info=True)

            # BACnet → HA: presentValue-Hook
            self._install_pv_hook(obj, m)

            # registrieren
            self.app.add_object(obj)
            oid = getattr(obj, "objectIdentifier", None)
            if not isinstance(oid, tuple) or len(oid) != 2:
                _LOGGER.warning("unerwartetes objectIdentifier für %s: %r", ent, oid)
                continue

            self.by_entity[ent] = obj
            self.by_oid[oid] = obj
            self.map_by_oid[oid] = m
            self._last[oid] = getattr(obj, "presentValue", None)

            _LOGGER.info("Published %s:%s ⇐ %s (desc=%s units=%s)",
                         oid[0], oid[1], ent, friendly,
                         getattr(obj, "units", None) if hasattr(obj, "units") else None)

        # Initial HA → BACnet
        await self._initial_sync()

        # Live-Events HA → BACnet
        self._ha_unsub = async_track_state_change_event(
            self.hass, list(self.by_entity.keys()), self._on_state_changed
        )

        # Fallback-Watcher
        if self._enable_watcher:
            self._watch_task = asyncio.create_task(self._watch_loop())

        _LOGGER.info("BacnetPublisher running (%d mappings).", len(self.by_entity))

    async def stop(self) -> None:
        if self._ha_unsub:
            try: self._ha_unsub()
            except Exception: pass
            self._ha_unsub = None

        if self._watch_task:
            self._watch_task.cancel()
            try: await self._watch_task
            except Exception: pass
            self._watch_task = None

        self.by_entity.clear()
        self.by_oid.clear()
        self.map_by_oid.clear()
        self._last.clear()
        _LOGGER.info("BacnetPublisher gestoppt")

    # --- HA → BACnet ---

    async def _initial_sync(self) -> None:
        for ent, obj in self.by_entity.items():
            st = self.hass.states.get(ent)
            if not st:
                continue
            self._apply_from_ha(obj, st.state)

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
        self._apply_from_ha(obj, ns.state)

    def _apply_from_ha(self, obj: Any, value: Any) -> None:
        oid = getattr(obj, "objectIdentifier", ("?", "?"))
        try:
            object.__setattr__(obj, "_ha_guard", True)
            if isinstance(obj, AnalogValueObject):
                obj.presentValue = _as_float(value)
            else:
                obj.presentValue = _truthy(value)
        finally:
            object.__setattr__(obj, "_ha_guard", False)
            self._last[oid] = getattr(obj, "presentValue", None)
            _LOGGER.debug("HA->BACnet: %s:%s PV -> %r", oid[0], oid[1], self._last[oid])

    # --- BACnet → HA ---

    def _install_pv_hook(self, obj: Any, mapping: Dict[str, Any]) -> None:
        orig_setattr = obj.__setattr__

        def hooked(self_obj, name, value):
            orig_setattr(name, value)
            if name != "presentValue":
                return
            if getattr(self_obj, "_ha_guard", False):
                return
            try:
                self._dispatch_bacnet_change(self_obj, value, mapping)
            except Exception:
                _LOGGER.debug("PV change handling failed", exc_info=True)

        object.__setattr__(obj, "__setattr__", MethodType(hooked, obj))

    def _dispatch_bacnet_change(self, obj: Any, value: Any, mapping: Dict[str, Any]) -> None:
        if not mapping.get("writable", False):
            return

        ent = mapping["entity_id"]
        domain = _entity_domain(ent)

        if isinstance(obj, BinaryValueObject):
            on = bool(value) if isinstance(value, bool) else _truthy(value)
            if domain in ("light", "switch", "fan"):
                asyncio.create_task(self._ha_call(domain, f"turn_{'on' if on else 'off'}", {"entity_id": ent}))
                _LOGGER.info("BACnet->HA %s.turn_%s %s", domain, "on" if on else "off", {"entity_id": ent})
                return
            if domain == "cover":
                svc = "open_cover" if on else "close_cover"
                asyncio.create_task(self._ha_call("cover", svc, {"entity_id": ent}))
                _LOGGER.info("BACnet->HA cover.%s %s", svc, {"entity_id": ent})
                return

        if isinstance(obj, AnalogValueObject) and domain in ("number", "input_number"):
            try:
                val = float(value)
            except Exception:
                val = 0.0
            asyncio.create_task(self._ha_call(domain, "set_value", {"entity_id": ent, "value": val}))
            _LOGGER.info("BACnet->HA %s.set_value %s", domain, {"entity_id": ent, "value": val})

    # --- Watcher (Fallback) ---

    async def _watch_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(WATCH_INTERVAL_SEC)
                for oid, obj in tuple(self.by_oid.items()):
                    try:
                        cur = getattr(obj, "presentValue", None)
                    except Exception:
                        continue
                    last = self._last.get(oid, object())
                    if cur != last:
                        if getattr(obj, "_ha_guard", False):
                            self._last[oid] = cur
                            continue
                        self._last[oid] = cur
                        mapping = self.map_by_oid.get(oid)
                        if mapping:
                            self._dispatch_bacnet_change(obj, cur, mapping)
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.debug("watch loop failed", exc_info=True)

    # --- Service-Caller ---

    async def _ha_call(self, domain: str, service: str, data: Dict[str, Any]) -> None:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=False)
        except Exception:
            _LOGGER.debug("Service call failed: %s.%s %s", domain, service, data, exc_info=True)
