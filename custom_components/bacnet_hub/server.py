# custom_components/bacnet_hub/server.py
from __future__ import annotations

import asyncio
import logging
import re
import socket
from typing import Any, Dict, Iterable, Optional

from bacpypes3.app import Application
from bacpypes3.vendor import get_vendor_info
from bacpypes3.object import ObjectType, PropertyIdentifier
from bacpypes3.basetypes import IPMode, HostNPort, BDTEntry
from bacpypes3.apdu import IAmRequest, WritePropertyRequest
from bacpypes3.pdu import Address

# Service-Mixins (aktivieren Standardservices inkl. COV)
from bacpypes3.service.device import WhoIsIAmServices
from bacpypes3.service.object import ReadWritePropertyServices
from bacpypes3.service.cov import ChangeOfValueServices

from .helpers.versions import get_integration_version, get_bacpypes3_version
from .publisher import BacnetPublisher
from .const import DOMAIN

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

# ---------------------- Netzwerk/Adresse Helpers ----------------------------

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
            if e.errno not in (98, 10048):
                _LOGGER.error("Preflight-Bind Fehler: %s", e, exc_info=True)
                raise
            _LOGGER.debug("Port belegt (V%d/%d): %s – warte %.2fs", attempt, _BIND_RETRIES, address_str, delay)
            await asyncio.sleep(delay)
            delay *= _BIND_BACKOFF
    raise last_err or OSError("Preflight-Bind fehlgeschlagen")

# ----------------------------- HubApp ---------------------------------------

class HubApp(
    Application,
    WhoIsIAmServices,            # I-Am / Who-Is
    ReadWritePropertyServices,   # Read/WriteProperty(+Multiple)
    ChangeOfValueServices,       # SubscribeCOV + COV Notifications
):
    """
    Anwendung mit aktivierten Standardservices.
    Spiegelt WriteProperty(presentValue) von BACnet nach HA, sofern ein Mapping existiert.
    """
    def __init__(self, *args, publisher: Optional[BacnetPublisher] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.publisher: Optional[BacnetPublisher] = publisher

    async def do_WritePropertyRequest(self, apdu: WritePropertyRequest):
        """
        1) Standard-Handling via Superklasse: schreibt in Local Object, triggert COV.
        2) Wenn presentValue betroffen war: an Publisher (BACnet -> HA) weitergeben,
           es sei denn, es war ein Echo aus HA (Echo-Guard).
        """
        await super().do_WritePropertyRequest(apdu)

        try:
            if apdu.propertyIdentifier not in ("presentValue", PropertyIdentifier.presentValue):
                return

            if not self.publisher:
                return

            oid = apdu.objectIdentifier

            # 👇 statt self.get_object(...) direkt über Publisher-Mapping
            obj = self.publisher.by_oid.get(oid)
            if not obj:
                return

            # Echo-Guard: wurde diese Änderung von HA ausgelöst?
            if getattr(obj, "_ha_guard", False):
                return

            mapping = self.publisher.map_by_oid.get(oid)
            if not mapping:
                return

            # finaler Wert aus dem lokalen Objekt
            val = getattr(obj, "presentValue", None)

            # Nicht blockieren: HA-Servicecall als Task
            asyncio.create_task(self.publisher.forward_to_ha_from_bacnet(mapping, val))
        except Exception:
            _LOGGER.debug("Hook do_WritePropertyRequest: Forwarding nach HA fehlgeschlagen.", exc_info=True)

# -------------------------- Server (HA Wrapper) -----------------------------

class BacnetHubServer:
    """Startet HubApp + Publisher. Nutzt Standard-Services (inkl. COV)."""

    def __init__(self, hass, merged_config: Dict[str, Any]) -> None:
        self.hass = hass
        self.cfg = merged_config or {}

        self.address_str: str = _normalize_address(str(self.cfg.get("address") or ""))
        self.network_number: Optional[int] = int(self.cfg.get("network_number", 1))
        self.name: str = str(self.cfg.get("name", "BACnetHub"))
        self.instance: int = int(self.cfg.get("instance", 8123))
        self.foreign: Optional[str] = self.cfg.get("foreign")
        self.ttl: int = int(self.cfg.get("ttl", 30))
        self.bbmd = self.cfg.get("bbmd")

        # Geräte-Metadaten
        self.vendor_identifier: int = 999
        self.vendor_name: str = "BACpypes3"
        self.model_name: str = "Home Assistant"
        self.description: str = "BACnet Hub (Custom Integration via BACpypes3)"
        self.application_software_version: Optional[str] = None
        self.firmware_revision: Optional[str] = None

        # Debug
        self.debug_bacpypes: bool = bool(self.cfg.get("debug_bacpypes", True))
        self.kick_iam: bool = bool(self.cfg.get("kick_iam", True))

        # Mapping-Liste
        self.published = list(self.cfg.get("published") or [])

        # Laufzeit
        self.publisher: Optional[BacnetPublisher] = None
        self.app: Optional[HubApp] = None

    async def start(self) -> None:
        if self.debug_bacpypes:
            for name in (
                "bacpypes3", "bacpypes3.app", "bacpypes3.apdu",
                "bacpypes3.pdu", "bacpypes3.service.cov"
            ):
                logging.getLogger(name).setLevel(logging.DEBUG)

        # Versionsinfos aus Manifest/Installation
        try:
            v = await get_integration_version(self.hass, DOMAIN)
            if v:
                self.application_software_version = f"{v}"
        except Exception:
            _LOGGER.debug("Konnte application_software_version nicht aus Manifest lesen.", exc_info=True)

        try:
            bpv = await get_bacpypes3_version(self.hass)
            if bpv:
                self.firmware_revision = f"bacpypes3 v{bpv}"
        except Exception:
            _LOGGER.debug("Konnte bacpypes3-Version nicht ermitteln.", exc_info=True)

        # UDP-Port vorab prüfen (vermeidet race mit HA-/Addon-Neustarts)
        await _wait_port_free(self.address_str)

        # Vendor Info & Objektklassen
        vendor_info = get_vendor_info(self.vendor_identifier)
        device_object_class = vendor_info.get_object_class(ObjectType.device)
        if not device_object_class:
            raise RuntimeError("vendor identifier {self.vendor_identifier} missing device object class")
        network_port_object_class = vendor_info.get_object_class(ObjectType.networkPort)
        if not network_port_object_class:
            raise RuntimeError("vendor identifier {self.vendor_identifier} missing network port object class")

        # Adresse für NetworkPort
        address = self.address_str or ("host:0" if self.foreign else "host")

        # Device-Objekt
        device_object = device_object_class(
            objectIdentifier=("device", int(self.instance)),
            objectName=str(self.name),
            vendorName=str(self.vendor_name),
            vendorIdentifier=int(self.vendor_identifier),
            modelName=str(self.model_name),
            description=str(self.description),
            firmwareRevision=str(self.firmware_revision) if self.firmware_revision else None,
            applicationSoftwareVersion=str(self.application_software_version) if self.application_software_version else None,
        )

        # Network-Port-Objekt
        network_port_object = network_port_object_class(
            address,
            objectIdentifier=("network-port", 1),
            objectName="NetworkPort-1",
            networkNumber=int(self.network_number),
            networkNumberQuality="configured" if self.network_number else "unknown",
        )

        # Foreign Device?
        if self.foreign is not None:
            network_port_object.bacnetIPMode = IPMode.foreign
            network_port_object.fdBBMDAddress = HostNPort(self.foreign)
            network_port_object.fdSubscriptionLifetime = int(self.ttl)

        # BBMD?
        if self.bbmd is not None:
            network_port_object.bacnetIPMode = IPMode.bbmd
            network_port_object.bbmdAcceptFDRegistrations = True
            bbmd_list: Iterable[str] = self.bbmd if isinstance(self.bbmd, (list, tuple)) else [self.bbmd]
            network_port_object.bbmdBroadcastDistributionTable = [BDTEntry(addr) for addr in bbmd_list]

        # --- Application erstellen (mit Services inkl. COV) ---
        self.app = HubApp.from_object_list([device_object, network_port_object])

        _LOGGER.info("BACnet Hub gestartet auf %s", self.address_str)

        # Publisher starten (schreibt/liest ausschließlich via Services)
        if self.published:
            self.publisher = BacnetPublisher(self.hass, self.app, self.published)
            # der App den Publisher geben (für BACnet→HA)
            self.app.publisher = self.publisher
            await self.publisher.start()

        # Optional: I-Am beim Start (Broadcast)
        if self.kick_iam and self.app:
            asyncio.create_task(self._kick_iam(self.app))

    async def _kick_iam(self, app: Application) -> None:
        try:
            await asyncio.sleep(0.2)
            dev = getattr(app, "device_object", None)
            if not dev:
                raise RuntimeError("Lokales Device nicht gefunden")
            req = IAmRequest(
                iAmDeviceIdentifier=dev.objectIdentifier,
                maxAPDULengthAccepted=int(dev.maxApduLengthAccepted),
                segmentationSupported=dev.segmentationSupported,
                vendorID=int(dev.vendorIdentifier),
            )
            req.pduDestination = Address("*:*")
            await app.request(req)
            _LOGGER.debug("I-Am gesendet (Broadcast *:*)")
        except Exception as e:
            _LOGGER.debug("I-Am fehlgeschlagen: %s", e, exc_info=True)

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
