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
    from bacpypes3.local.object import AnalogValueObject, BinaryValueObject
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


class BacnetServer:
    def __init__(self, hass: HomeAssistant, entry):
        self.hass = hass
        self.entry = entry
        self._running = False
        self._app: Application | None = None
        self._device: DeviceObject | None = None
        self._objects: Dict[Tuple[str, int], Mapping] = {}
        self._whois_task: asyncio.Task | None = None
        self._ha_unsub = None

    async def start(self):
        opts = self.entry.data | (self.entry.options or {})
        if Application is None:
            _LOGGER.error("bacpypes3 is not available. Check installation and manifest requirements.")
            return

        address = f"{opts.get('address','0.0.0.0')}:{opts.get('port', 47808)}"
        device_id = int(opts.get('device_id', 500000))
        device_name = "BACnet Hub"

        # Create DeviceObject and Application
        self._device = DeviceObject(
            objectIdentifier=("device", device_id),
            objectName=device_name,
            maxAPDULengthAccepted=1024,
            segmentationSupported="noSegmentation",
            vendorIdentifier=999,
        )
        self._app = Application(self._device, Address(address))
        _LOGGER.info("BACnet Hub bound to %s (device-id=%s)", address, device_id)

        # Optional BBMD
        bbmd_ip = opts.get("bbmd_ip")
        if bbmd_ip:
            try:
                ttl = int(opts.get("bbmd_ttl", 600))
                await self._app.register_bbmd(Address(bbmd_ip), ttl)  # type: ignore[attr-defined]
                _LOGGER.info("Registered BBMD %s TTL=%s", bbmd_ip, ttl)
            except Exception as exc:
                _LOGGER.warning("BBMD registration failed: %s", exc)

        # Build objects from options
        for raw in opts.get(CONF_OBJECTS, []) or []:
            try:
                m = Mapping(**raw)
            except TypeError as exc:
                _LOGGER.warning("Invalid mapping %s: %s", raw, exc)
                continue
            if m.object_type not in SUPPORTED_TYPES:
                _LOGGER.warning("Unsupported object_type '%s' (MVP supports: %s)", m.object_type, SUPPORTED_TYPES)
                continue
            await self._add_object(m)

        # Hook property handlers (API may vary; using Application callbacks if available)
        # In some bacpypes3 versions, you can assign callbacks or subclass.
        # Here we monkey-patch by wrapping 'read_property'/'write_property' if present.
        if hasattr(self._app, "set_property_handlers"):
            # hypothetical helper if available in your version
            self._app.set_property_handlers(self._on_read_property, self._on_write_property)  # type: ignore[attr-defined]
        else:
            # Fallback: store for later use from subclassed objects (we added lambdas in _add_object).
            pass

        # Optional: subscribe to HA state changes for future COV (not sending COV yet)
        self._ha_unsub = async_track_state_change_event(self.hass, [], self._on_state_changed)  # empty list = all

        self._running = True

    async def _add_object(self, m: Mapping):
        assert self._app is not None
        obj_id = (m.object_type, m.instance)
        name = m.name or m.entity_id
        units = ENGINEERING_UNITS_ENUM.get(m.units) if m.units else None

        if m.object_type == "analogValue":
            # Create an AnalogValue with a dynamic presentValue
            av = AnalogValueObject(
                objectIdentifier=obj_id,
                objectName=name,
                presentValue=0.0,
            )
            if units is not None and hasattr(av, "units"):
                av.units = units  # type: ignore[attr-defined]

            # Attach dynamic handlers if the object supports them
            if hasattr(av, "ReadProperty"):
                orig_read = av.ReadProperty  # type: ignore[attr-defined]

                async def dynamic_read(prop, arrayIndex=None):
                    if getattr(prop, "propertyIdentifier", str(prop)) == "presentValue":
                        val = await m.read_from_ha(self.hass)
                        try:
                            av.presentValue = float(val or 0.0)  # type: ignore[attr-defined]
                        except Exception:
                            av.presentValue = 0.0  # type: ignore[attr-defined]
                    return await orig_read(prop, arrayIndex)  # type: ignore[attr-defined]

                av.ReadProperty = dynamic_read  # type: ignore[assignment]

            if hasattr(av, "WriteProperty") and m.writable:
                orig_write = av.WriteProperty  # type: ignore[attr-defined]

                async def dynamic_write(prop, value, arrayIndex=None, priority=None, direct=False):
                    if getattr(prop, "propertyIdentifier", str(prop)) == "presentValue":
                        await m.write_to_ha(self.hass, value, priority)
                    return await orig_write(prop, value, arrayIndex, priority, direct)  # type: ignore[attr-defined]

                av.WriteProperty = dynamic_write  # type: ignore[assignment]

            await self._app.add_object(av)  # type: ignore[arg-type]

        elif m.object_type == "binaryValue":
            bv = BinaryValueObject(
                objectIdentifier=obj_id,
                objectName=name,
                presentValue=False,
            )
            if hasattr(bv, "ReadProperty"):
                orig_read = bv.ReadProperty  # type: ignore[attr-defined]

                async def dynamic_read(prop, arrayIndex=None):
                    if getattr(prop, "propertyIdentifier", str(prop)) == "presentValue":
                        val = await m.read_from_ha(self.hass)
                        bv.presentValue = bool(val)  # type: ignore[attr-defined]
                    return await orig_read(prop, arrayIndex)  # type: ignore[attr-defined]

                bv.ReadProperty = dynamic_read  # type: ignore[assignment]

            if hasattr(bv, "WriteProperty") and m.writable:
                orig_write = bv.WriteProperty  # type: ignore[attr-defined]

                async def dynamic_write(prop, value, arrayIndex=None, priority=None, direct=False):
                    if getattr(prop, "propertyIdentifier", str(prop)) == "presentValue":
                        await m.write_to_ha(self.hass, value, priority)
                    return await orig_write(prop, value, arrayIndex, priority, direct)  # type: ignore[attr-defined]

                bv.WriteProperty = dynamic_write  # type: ignore[assignment]

            await self._app.add_object(bv)  # type: ignore[arg-type]

        self._objects[obj_id] = m
        _LOGGER.info("Added %s:%s mapped to %s", *obj_id, m.entity_id)

    async def _on_read_property(self, obj, prop, array_index=None):
        # Generic handler (used only if bacpypes3 exposes a central hook)
        key = (obj.objectType, obj.objectIdentifier[1])
        m = self._objects.get(key)
        if not m:
            return None
        if getattr(prop, "propertyIdentifier", str(prop)) != "presentValue":
            return None
        return await m.read_from_ha(self.hass)

    async def _on_write_property(self, obj, prop, value, priority=None):
        key = (obj.objectType, obj.objectIdentifier[1])
        m = self._objects.get(key)
        if not m or not m.writable:
            return
        if getattr(prop, "propertyIdentifier", str(prop)) != "presentValue":
            return
        await m.write_to_ha(self.hass, value, priority)

    async def reload(self, options: dict[str, Any]):
        await self.stop()
        # HA updates entry.options; we just rebuild using self.entry
        await self.start()

    async def stop(self):
        if self._ha_unsub:
            self._ha_unsub()
            self._ha_unsub = None

        if self._app:
            try:
                await self._app.close()  # type: ignore[attr-defined]
            except Exception as exc:
                _LOGGER.debug("Error closing app: %s", exc)

        self._app = None
        self._device = None
        self._objects.clear()
        self._running = False

    @callback
    def _on_state_changed(self, event):
        # Placeholder for future COV notifications
        pass
