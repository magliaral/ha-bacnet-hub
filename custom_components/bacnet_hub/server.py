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
    """BACnet-Server, vollständig aus dem ConfigEntry konfiguriert."""

    def __init__(self, hass: HomeAssistant, config: Dict[str, Any]):
        self.hass = hass
        self.config = config
        self.mappings: List[Mapping] = []
        self.app = None
        self.device = None
        self._state_unsub = None
        self._stop_event: asyncio.Event | None = None
        self.entity_index: Dict[str, Any] = {}

    # -------- Config normalisieren --------
    def _parse_config(self) -> Dict[str, Any]:
        cfg: Dict[str, Any] = {
            "options": {
                "instance": int(self.config.get("instance") or 500000),
                "name": self.config.get("device_name") or "BACnet Hub",
            },
            "bacpypes": {"options": {}},
            "objects": [],
        }

        def _norm_addr(addr: str | None) -> Optional[str]:
            if not addr:
                return None
            addr = addr.strip()
            # bacpypes erwartet <ip>/<prefix>; ergänze /24, wenn nicht vorhanden
            if "/" not in addr and addr != "0.0.0.0":
                return f"{addr}/24"
            return addr

        bp = cfg["bacpypes"]["options"]
        if self.config.get("address"):
            bp["address"] = _norm_addr(self.config.get("address"))
        if self.config.get("port") is not None:
            bp["port"] = int(self.config["port"])
        if self.config.get("broadcastAddress"):
            bp["broadcastAddress"] = self.config["broadcastAddress"]

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
    def _ha_get_value(self, entity_id: str, mode="state", attr: Optional[str] = None, analog=False):
        st: State | None = self.hass.states.get(entity_id)
        if not st:
            return 0.0 if analog else False
        val = st.attributes.get(attr) if mode == "attr" and attr else st.state
        if analog:
            try:
                return float(val)
            except Exception:
                return 0.0
        s = str(val).lower()
        if s in ("on", "true", "1", "open", "heat", "cool"):
            return True
        if s in ("off", "false", "0", "closed"):
            return False
        return False

    async def _ha_call_service(self, domain: str, service: str, data: Dict[str, Any]):
        await self.hass.services.async_call(domain, service, data, blocking=False)

    # -------- bacpypes --------
    def _import_bacpypes(self):
        from importlib import import_module
        _Application = import_module("bacpypes3.app").Application
        _SimpleArgumentParser = import_module("bacpypes3.argparse").SimpleArgumentParser
        _DeviceObject = import_module("bacpypes3.local.device").DeviceObject
        AV = BV = None
        for mod, av, bv in [
            ("bacpypes3.local.object", "AnalogValueObject", "BinaryValueObject"),
            ("bacpypes3.local.objects.value", "AnalogValueObject", None),
            ("bacpypes3.local.objects.binary", None, "BinaryValueObject"),
            ("bacpypes3.local.analog", "AnalogValueObject", None),
            ("bacpypes3.local.binary", None, "BinaryValueObject"),
        ]:
            try:
                m = import_module(mod)
                if av and hasattr(m, av):
                    AV = getattr(m, av)
                if bv and hasattr(m, bv):
                    BV = getattr(m, bv)
            except Exception:
                pass
        if not AV or not BV:
            raise ImportError("AnalogValueObject/BinaryValueObject not found")
        return _Application, _SimpleArgumentParser, _DeviceObject, AV, BV

    def _build_bacpypes_args(self, SimpleArgumentParser):
        cfg = self._parse_config()
        argv: List[str] = []
        opts = (cfg.get("bacpypes", {}) or {}).get("options", {}) or {}
        for k, v in opts.items():
            if v is None:
                continue
            flag = f"--{str(k).replace('_','-')}"
            if isinstance(v, bool):
                if v:
                    argv.append(flag)
            elif isinstance(v, (list, tuple)):
                for item in v:
                    argv.extend([flag, str(item)])
            else:
                argv.extend([flag, str(v)])

        dev = cfg.get("options", {}) or {}
        if dev.get("instance"):
            argv.extend(["--instance", str(int(dev["instance"]))])

        parser = SimpleArgumentParser()
        try:
            _LOGGER.debug("bacpypes argv: %r", argv)
            return parser.parse_args(argv)
        except SystemExit as e:
            _LOGGER.warning("bacpypes Parser-Abbruch (%s) → Fallback-Namespace", e)
            address = opts.get("address")
            port = int(opts.get("port")) if opts.get("port") is not None else None
            instance = int(dev.get("instance")) if dev.get("instance") is not None else None
            return _types.SimpleNamespace(address=address, port=port, instance=instance)

    # -------- Lifecycle --------
    async def start(self):
        self._load_mappings()
        (Application, SimpleArgumentParser, DeviceObject, AV, BV) = self._import_bacpypes()
        args = self._build_bacpypes_args(SimpleArgumentParser)
        self.app = Application.from_args(args)

        bind_addr = getattr(args, "address", None) or "0.0.0.0"
        bind_port = getattr(args, "port", None) or 47808
        bind_instance = getattr(args, "instance", None) or 47808
        _LOGGER.info("BACnet bound to %s:%s device-id=%s", bind_addr, bind_port, bind_instance)

        for m in self.mappings:
            if m.object_type not in SUPPORTED_TYPES:
                _LOGGER.warning("Unsupported type %s", m.object_type)
                continue
            await self._add_object(self.app, m, AV, BV)

        await self._initial_sync()

        if self._state_unsub:
            self._state_unsub()
        self._state_unsub = self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state_changed)

        self._stop_event = asyncio.Event()
        self.hass.loop.create_task(self._hold_open())

    async def _hold_open(self):
        if self._stop_event:
            await self._stop_event.wait()
        try:
            if self.app:
                close = getattr(self.app, "close", None)
                if callable(close):
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res
        except Exception as e:
            _LOGGER.debug("bacpypes close failed: %r", e)

    async def stop(self):
        if self._state_unsub:
            self._state_unsub()
            self._state_unsub = None
        if self._stop_event and not self._stop_event.is_set():
            self._stop_event.set()

    # -------- Objekte (MVP) --------
    async def _add_object(self, app, m: Mapping, AV, BV):
        key = (m.object_type, m.instance)
        name = m.name or m.entity_id
        if m.object_type == "analogValue":
            obj = AV(objectIdentifier=key, objectName=name, presentValue=0.0)
            if m.units and hasattr(obj, "units"):
                units = ENGINEERING_UNITS_ENUM.get(m.units)
                if units is not None:
                    obj.units = units  # type: ignore
        else:
            obj = BV(objectIdentifier=key, objectName=name, presentValue=False)
        await app.add_object(obj)
        self.entity_index[m.entity_id] = obj

    async def _initial_sync(self):
        for m in self.mappings:
            obj = self.entity_index.get(m.entity_id)
            if not obj:
                continue
            if m.object_type == "analogValue":
                val = self._ha_get_value(m.entity_id, m.mode, m.attr, analog=True)
                try:
                    obj.presentValue = float(val)  # type: ignore
                except Exception:
                    obj.presentValue = 0.0  # type: ignore
            else:
                val = self._ha_get_value(m.entity_id, m.mode, m.attr, analog=False)
                obj.presentValue = bool(val)  # type: ignore

    @callback
    def _on_state_changed(self, event):
        data = event.data
        ent_id = data.get("entity_id")
        obj = self.entity_index.get(ent_id)
        if not obj:
            return
        m = next((mm for mm in self.mappings if mm.entity_id == ent_id), None)
        if not m:
            return
        new_state: State | None = data.get("new_state")
        if not new_state:
            return
        val = new_state.attributes.get(m.attr) if (m.mode == "attr" and m.attr) else new_state.state
        if m.object_type == "analogValue":
            try:
                obj.presentValue = float(val)  # type: ignore
            except Exception:
                obj.presentValue = 0.0  # type: ignore
        else:
            obj.presentValue = str(val).lower() in ("on", "true", "1")  # type: ignore
