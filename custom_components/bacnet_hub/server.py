from __future__ import annotations

import ipaddress
import logging
from argparse import Namespace
from typing import Any, Dict, Optional

import yaml
from bacpypes3.app import Application
from bacpypes3.vendor import get_vendor_info

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_LOGGER.addHandler(logging.NullHandler())


def _coerce_network_port_kwargs(val: Any) -> Dict[str, Any]:
    """Erlaubt dict, YAML/JSON-String oder leer für network_port_object."""
    if not val:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            data = yaml.safe_load(val) or {}
        except yaml.YAMLError as e:
            _LOGGER.warning("network_port_object konnte nicht geparst werden: %s", e)
            return {}
        return data if isinstance(data, dict) else {}
    _LOGGER.debug("Ignoriere network_port_object Typ %s", type(val).__name__)
    return {}


def _address_looks_ok(addr: str) -> bool:
    """Akzeptiere 0.0.0.0/127.0.0.1 oder gültige IP."""
    if addr in ("0.0.0.0", "127.0.0.1"):
        return True
    try:
        ipaddress.ip_address(addr)
        return True
    except Exception:
        return False


def _safe_load_objects_source(source: Any) -> Dict[str, Any]:
    """
    Quelle für BACnet-Objekte laden:
      - None / ""  -> leere Liste
      - Pfad zu YAML-Datei
      - Inline-YAML (String)
      - bereits strukturiert: dict oder list
    Rückgabe-Form: {"objects": [ ... ]}
    """
    if source is None:
        _LOGGER.debug("objects_yaml ist None -> verwende leere Liste.")
        return {"objects": []}
    if isinstance(source, str) and source.strip() == "":
        _LOGGER.debug("objects_yaml ist leerer String -> verwende leere Liste.")
        return {"objects": []}

    if isinstance(source, dict):
        objs = source.get("objects")
        if objs is None:
            return {"objects": []}
        if isinstance(objs, list):
            return {"objects": objs}
        if isinstance(objs, dict):
            return {"objects": [objs]}
        raise TypeError(f"'objects' muss Liste/Mapping sein, erhalten: {type(objs).__name__}")

    if isinstance(source, list):
        return {"objects": source}

    if isinstance(source, str):
        text = source.strip()
        # Inline-Heuristik
        if "\n" in text or text.lstrip().startswith(("objects", "-")) or ":" in text:
            _LOGGER.debug("objects_yaml: Inline-YAML erkannt, parse direkt")
            try:
                data = yaml.safe_load(text) or {}
            except yaml.YAMLError as e:
                raise TypeError(f"objects_yaml konnte nicht geparst werden: {e}") from e

            if isinstance(data, list):
                return {"objects": data}
            if isinstance(data, dict):
                if "objects" not in data or data["objects"] is None:
                    return {"objects": []}
                if isinstance(data["objects"], list):
                    return {"objects": data["objects"]}
                if isinstance(data["objects"], dict):
                    return {"objects": [data["objects"]]}
                raise TypeError(f"'objects' muss Liste/Mapping sein, erhalten: {type(data['objects']).__name__}")
            if isinstance(data, (str, int, float)) or data is None:
                return {"objects": []}
            raise TypeError(f"Inline-YAML muss Mapping/Liste sein, erhalten: {type(data).__name__}")
        else:
            # Pfad zur Datei
            try:
                with open(text, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            except FileNotFoundError:
                _LOGGER.warning("objects_yaml nicht gefunden unter: %s – verwende leere Liste.", text)
                return {"objects": []}
            except yaml.YAMLError as e:
                raise TypeError(f"objects_yaml konnte nicht geparst werden: {e}") from e

            if isinstance(data, list):
                return {"objects": data}
            if isinstance(data, dict):
                objs = data.get("objects")
                if objs is None:
                    return {"objects": []}
                if isinstance(objs, list):
                    return {"objects": objs}
                if isinstance(objs, dict):
                    return {"objects": [objs]}
                raise TypeError(f"'objects' muss Liste/Mapping sein, erhalten: {type(objs).__name__}")
            return {"objects": []}

    raise TypeError(f"Ungültiger Typ für objects_yaml: {type(source).__name__}")


class BacnetHubServer:
    """Kapselt die bacpypes3 Application und die Konvertierung der Konfiguration."""

    def __init__(self, hass, merged_config: Dict[str, Any]) -> None:
        self.hass = hass
        self.cfg = merged_config or {}

        self.objects_source: Any = (
            self.cfg.get("objects_yaml")
            or self.cfg.get("objects_yaml_path")
            or ""
        )

        self.bind: str = str(self.cfg.get("bind", "0.0.0.0"))
        self.port: int = int(self.cfg.get("port", 47808))

        nn = self.cfg.get("network_number", None)
        self.network_number: Optional[int] = int(nn) if nn not in (None, "") else None

        self.network_port_object: Dict[str, Any] = _coerce_network_port_kwargs(
            self.cfg.get("network_port_object")
        )

        # Device/Hersteller
        self.name: str = str(self.cfg.get("name", "BACnetHub"))
        self.instance: int = int(self.cfg.get("instance", 1234))
        self.vendoridentifier: int = int(self.cfg.get("vendoridentifier", 999))
        self.vendorname: str = str(self.cfg.get("vendorname", "Home Assistant BACnet Hub"))

        # Sichtbare Felder
        self.description: str = str(self.cfg.get("description", "BACnet Hub for Home Assistant"))
        self.model_name: str = str(self.cfg.get("model_name", "Home Assistant"))
        self.application_software_version: str = str(self.cfg.get("application_software_version", "0.1.0"))
        self.firmware_revision: Optional[str] = self.cfg.get("firmware_revision") or None

        # Optional: Foreign/BBMD/Broadcast
        self.foreign: Optional[str] = (self.cfg.get("foreign") or None)
        self.ttl: int = int(self.cfg.get("ttl", 30))
        self.bbmd: Optional[str] = (self.cfg.get("bbmd") or None)
        self.broadcast: Optional[str] = (self.cfg.get("broadcast") or None)

        self.app: Optional[Application] = None
        self.objects_def: Dict[str, Any] = {}

    async def start(self) -> None:
        """Erzeuge und starte die bacpypes3-Application."""
        self.objects_def = _safe_load_objects_source(self.objects_source)

        if not _address_looks_ok(self.bind):
            _LOGGER.warning("Bind-Adresse ungültig (%s), fallback auf 0.0.0.0", self.bind)
            self.bind = "0.0.0.0"

        args = Namespace()
        setattr(args, "address", f"{self.bind}:{self.port}")

        if self.network_number is not None:
            setattr(args, "network", int(self.network_number))
            setattr(args, "network_number_quality", "configured")
        else:
            setattr(args, "network", 1)
            setattr(args, "network_number_quality", "unknown")

        setattr(args, "network_port_object", self.network_port_object)

        setattr(args, "instance", int(self.instance))
        setattr(args, "name", self.name)
        setattr(args, "vendoridentifier", int(self.vendoridentifier))
        setattr(args, "vendorname", self.vendorname)

        setattr(args, "description", self.description)
        setattr(args, "modelName", self.model_name)
        setattr(args, "applicationSoftwareVersion", self.application_software_version)
        setattr(args, "firmwareRevision", self.firmware_revision)

        setattr(args, "foreign", self.foreign if self.foreign else None)
        setattr(args, "ttl", int(self.ttl))
        setattr(args, "bbmd", self.bbmd if self.bbmd else None)
        setattr(args, "broadcast", self.broadcast if self.broadcast else None)

        _LOGGER.debug(
            "Starte BACnet Application mit args: address=%s, network=%s, quality=%s, "
            "vendoridentifier=%s, vendorname=%s, instance=%s, name=%s, extra=%s",
            getattr(args, "address", None),
            getattr(args, "network", None),
            getattr(args, "network_number_quality", None),
            getattr(args, "vendoridentifier", None),
            getattr(args, "vendorname", None),
            getattr(args, "instance", None),
            getattr(args, "name", None),
            self.network_port_object,
        )

        try:
            _ = get_vendor_info(int(self.vendoridentifier))
            self.app = Application.from_args(args)
        except Exception as err:
            _LOGGER.error("Fehler beim Erzeugen der BACnet Application: %s", err, exc_info=True)
            raise

        _LOGGER.info("BACnet Hub gestartet auf %s", getattr(args, "address", None))

    async def stop(self) -> None:
        """Application sauber schließen."""
        if self.app is not None:
            try:
                await self.app.close()  # type: ignore[func-returns-value]
            except Exception:
                pass
            self.app = None
        _LOGGER.info("BACnet Hub gestoppt")
