from __future__ import annotations

import asyncio
import logging
import re
import socket
from argparse import Namespace
from typing import Any, Dict, Optional

from bacpypes3.app import Application
from bacpypes3.vendor import get_vendor_info
from bacpypes3.apdu import IAmRequest
from bacpypes3.pdu import Address

from .publisher import BacnetPublisher

_LOGGER = logging.getLogger(__name__)
_DEFAULT_PREFIX = 24
_DEFAULT_PORT = 47808
_BIND_RETRIES = 10
_BIND_INITIAL_DELAY = 0.2
_BIND_BACKOFF = 1.5

_ADDR_RE = re.compile(
    r"^\s*(?P<ip>(\d{1,3}\.){3}\d{1,3})"
    r"(?:/(?P<prefix>\d{1,2}))?"
    r"(?::(?P<port>\d{1,5}))?\s*$"
)


def _detect_local_ip() -> Optional[str]:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _normalize_address(addr: Optional[str]) -> str:
    if addr:
        m = _ADDR_RE.match(addr)
        if m:
            ip = m.group("ip")
            prefix = m.group("prefix")
            port = m.group("port")
            try:
                parts = [int(x) for x in ip.split(".")]
                if any(p < 0 or p > 255 for p in parts):
                    raise ValueError
            except Exception:
                _LOGGER.warning("Adresse hat keine gültige IPv4 (%s). Fallback wird verwendet.", addr)
            else:
                if ip in ("0.0.0.0", "127.0.0.1"):
                    prt = int(port) if port is not None else _DEFAULT_PORT
                    norm = f"{ip}:{prt}"
                    if norm != addr:
                        _LOGGER.debug("Adresse normalisiert: '%s' -> '%s'", addr, norm)
                    return norm

                pfx = int(prefix) if prefix is not None else _DEFAULT_PREFIX
                prt = int(port) if port is not None else _DEFAULT_PORT
                norm = f"{ip}/{pfx}:{prt}"
                if norm != addr:
                    _LOGGER.debug("Adresse normalisiert: '%s' -> '%s'", addr, norm)
                return norm

    ip = _detect_local_ip() or "192.168.0.2"
    norm = f"{ip}/{_DEFAULT_PREFIX}:{_DEFAULT_PORT}"
    _LOGGER.debug("Adresse Fallback verwendet: %s", norm)
    return norm


def _split_ip_port(address_str: str) -> tuple[str, int]:
    m = _ADDR_RE.match(address_str)
    if not m:
        raise ValueError(f"Ungültige Adresse: {address_str!r}")
    ip = m.group("ip")
    port = int(m.group("port") or _DEFAULT_PORT)
    return ip, port


def _preflight_bind(address_str: str) -> None:
    ip, port = _split_ip_port(address_str)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind((ip, port))
    finally:
        s.close()


async def _wait_port_free(address_str: str) -> None:
    delay = _BIND_INITIAL_DELAY
    last_err = None
    for attempt in range(1, _BIND_RETRIES + 1):
        try:
            _preflight_bind(address_str)
            _LOGGER.debug("Preflight-Bind ok (Versuch %d): %s", attempt, address_str)
            return
        except OSError as e:
            last_err = e
            if e.errno not in (98, 10048):  # EADDRINUSE
                _LOGGER.error("Preflight-Bind Fehler: %s", e, exc_info=True)
                raise
            _LOGGER.debug("Port belegt (V%d/%d): %s – warte %.2fs",
                          attempt, _BIND_RETRIES, address_str, delay)
            await asyncio.sleep(delay)
            delay *= _BIND_BACKOFF
    raise last_err or OSError("Preflight-Bind fehlgeschlagen")


class BacnetHubServer:
    """BACnet Lokales Gerät + Publisher."""

    def __init__(self, hass, merged_config: Dict[str, Any]) -> None:
        self.hass = hass
        self.cfg = merged_config or {}
        self.address_str: str = _normalize_address(str(self.cfg.get("address") or ""))
        self.network_number: Optional[int] = int(self.cfg.get("network_number", 1))
        self.name: str = str(self.cfg.get("name", "BACnetHub"))
        self.instance: int = int(self.cfg.get("instance", 1234))
        self.vendoridentifier: int = int(self.cfg.get("vendoridentifier", 999))
        self.vendorname: str = str(self.cfg.get("vendorname", "Home Assistant BACnet Hub"))
        self.description: str = str(self.cfg.get("description", "BACnet Hub for Home Assistant"))
        self.model_name: str = str(self.cfg.get("model_name", "Home Assistant"))
        self.application_software_version: str = str(self.cfg.get("application_software_version", "0.1.1"))
        self.firmwareRevision: Optional[str] = self.cfg.get("firmwareRevision", "0.1.1")
        self.foreign: Optional[str] = self.cfg.get("foreign")
        self.ttl: int = int(self.cfg.get("ttl", 30))
        self.bbmd: Optional[str] = self.cfg.get("bbmd")
        self.broadcast: Optional[str] = self.cfg.get("broadcast")
        self.debug_bacpypes: bool = bool(self.cfg.get("debug_bacpypes", True))
        self.trace_bacpypes: bool = bool(self.cfg.get("trace_bacpypes", True))
        self.kick_iam: bool = bool(self.cfg.get("kick_iam", True))
        self.published = list(self.cfg.get("published") or [])
        self.publisher: Optional[BacnetPublisher] = None
        self.app: Optional[Application] = None

    async def start(self) -> None:
        if self.debug_bacpypes:
            for name in (
                "bacpypes3",
                "bacpypes3.app",
                "bacpypes3.apdu",
                "bacpypes3.pdu",
            ):
                logging.getLogger(name).setLevel(logging.DEBUG)

        args = Namespace()
        args.address = self.address_str
        args.network = self.network_number
        args.instance = self.instance
        args.name = self.name
        args.vendoridentifier = self.vendoridentifier
        args.vendorname = self.vendorname
        args.description = self.description
        args.modelName = self.model_name
        args.applicationSoftwareVersion = self.application_software_version
        args.firmwareRevision = self.firmwareRevision
        args.foreign = self.foreign
        args.ttl = self.ttl
        args.bbmd = self.bbmd
        args.broadcast = self.broadcast
        args.debug = self.debug_bacpypes
        args.trace = self.trace_bacpypes

        await _wait_port_free(self.address_str)
        _ = get_vendor_info(self.vendoridentifier)
        self.app = Application.from_args(args)

        _LOGGER.info("BACnet Hub gestartet auf %s", self.address_str)

        if self.published:
            self.publisher = BacnetPublisher(self.hass, self.app, self.published)
            await self.publisher.start()

        if self.kick_iam and self.app:
            async def _kick(app: Application):
                await asyncio.sleep(0.2)
            
                dev = getattr(app, "device", None) or getattr(app, "local_device", None)
                if not dev:
                    raise RuntimeError("Lokales Device nicht gefunden")
            
                device_identifier = dev.objectIdentifier
                max_apdu = int(dev.maxApduLengthAccepted)
                seg_supported = dev.segmentationSupported
                vendor_id = int(dev.vendorIdentifier)
            
                req = IAmRequest(
                    iAmDeviceIdentifier=device_identifier,
                    maxAPDULengthAccepted=max_apdu,
                    segmentationSupported=seg_supported,
                    vendorID=vendor_id,
                )
                req.pduDestination = Address("*:*")
                await app.request(req)
                _LOGGER.debug("I-Am Test gesendet (Broadcast *:*)")
            asyncio.create_task(_kick(self.app))

    async def stop(self) -> None:
        if self.publisher:
            try:
                await self.publisher.stop()
            except Exception:
                _LOGGER.debug("Publisher stop failed", exc_info=True)
            self.publisher = None

        if self.app:
            try:
                await self.app.close()
            except Exception:
                pass
            self.app = None

        _LOGGER.info("BACnet Hub gestoppt")
        try:
            await asyncio.sleep(0.1)
        except Exception:
            pass
