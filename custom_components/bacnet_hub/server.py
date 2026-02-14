# custom_components/bacnet_hub/server.py
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from typing import Any, Dict, Iterable, Optional

from bacpypes3.app import Application
from bacpypes3.errors import ExecutionError
from bacpypes3.vendor import get_vendor_info
from bacpypes3.object import ObjectType, PropertyIdentifier
from bacpypes3.basetypes import IPMode, HostNPort, BDTEntry
from bacpypes3.apdu import IAmRequest, WritePropertyRequest
from bacpypes3.pdu import Address

# Service mixins (enable standard services including COV)
from bacpypes3.service.device import WhoIsIAmServices
from bacpypes3.service.object import ReadWritePropertyServices
from bacpypes3.service.cov import ChangeOfValueServices

from .helpers.versions import get_integration_version, get_bacpypes3_version
from .publisher import BacnetPublisher
from .const import (
    CONF_DEVICE_DESCRIPTION,
    CONF_DEVICE_NAME,
    DEFAULT_BACNET_DEVICE_DESCRIPTION,
    DEFAULT_BACNET_OBJECT_NAME,
    DOMAIN,
)

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

_SYSTEM_STATUS_LABELS: dict[int, str] = {
    0: "operational",
    1: "operational_read_only",
    2: "download_required",
    3: "download_in_progress",
    4: "non_operational",
    5: "backup_in_progress",
}

# ---------------------- Network/Address Helpers -----------------------------

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
                _LOGGER.warning("Address is not valid IPv4 (%s). Using fallback.", addr)
            else:
                pfx = int(prefix) if prefix is not None else _DEFAULT_PREFIX
                prt = int(port) if port is not None else _DEFAULT_PORT
                norm = f"{ip}/{pfx}:{prt}"
                if norm != addr:
                    _LOGGER.debug("Address normalized: '%s' -> '%s'", addr, norm)
                return norm

    ip = _detect_local_ip() or "192.168.0.2"
    norm = f"{ip}/{_DEFAULT_PREFIX}:{_DEFAULT_PORT}"
    _LOGGER.debug("Using fallback address: %s", norm)
    return norm


def _split_ip_port(address_str: str) -> tuple[str, int]:
    m = _ADDR_RE.match(address_str)
    if not m:
        raise ValueError(f"Invalid address: {address_str!r}")
    ip = m.group("ip")
    port = int(m.group("port") or _DEFAULT_PORT)
    return ip, port


def _split_ip_prefix_port(address_str: str) -> tuple[str, int, int]:
    m = _ADDR_RE.match(address_str)
    if not m:
        raise ValueError(f"Invalid address: {address_str!r}")
    ip = m.group("ip")
    prefix = int(m.group("prefix") or _DEFAULT_PREFIX)
    if prefix < 0 or prefix > 32:
        prefix = _DEFAULT_PREFIX
    port = int(m.group("port") or _DEFAULT_PORT)
    return ip, prefix, port


def _prefix_to_netmask(prefix: int) -> str:
    try:
        return str(ipaddress.ip_network(f"0.0.0.0/{prefix}", strict=False).netmask)
    except Exception:
        return "255.255.255.0"


def _normalize_system_status(value: Any) -> tuple[int | None, str]:
    raw = str(value or "").strip()
    if not raw:
        return None, "unknown"

    code_match = re.search(r"\b([0-5])\b", raw)
    if code_match:
        code = int(code_match.group(1))
        return code, _SYSTEM_STATUS_LABELS.get(code, "unknown")

    token = raw.split(".")[-1].strip().lower()
    token = re.sub(r"[^a-z0-9]+", "_", token).strip("_")
    if token.isdigit():
        code = int(token)
        return code, _SYSTEM_STATUS_LABELS.get(code, "unknown")

    for code, label in _SYSTEM_STATUS_LABELS.items():
        if token == label:
            return code, label

    return None, token or "unknown"


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
            _LOGGER.debug("Preflight bind OK (attempt %d): %s", attempt, address_str)
            return
        except OSError as e:
            last_err = e
            if e.errno not in (98, 10048):
                _LOGGER.error("Preflight bind error: %s", e, exc_info=True)
                raise
            _LOGGER.debug("Port busy (attempt %d/%d): %s – waiting %.2fs", attempt, _BIND_RETRIES, address_str, delay)
            await asyncio.sleep(delay)
            delay *= _BIND_BACKOFF
    raise last_err or OSError("Preflight bind failed")

# ----------------------------- HubApp ---------------------------------------

class HubApp(
    Application,
    WhoIsIAmServices,            # I-Am / Who-Is
    ReadWritePropertyServices,   # Read/WriteProperty(+Multiple)
    ChangeOfValueServices,       # SubscribeCOV + COV Notifications
):
    """
    Application with standard services enabled.
    Mirrors WriteProperty(presentValue) from BACnet to HA when mapping exists.
    """
    def __init__(self, *args, publisher: Optional[BacnetPublisher] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.publisher: Optional[BacnetPublisher] = publisher

    async def do_WritePropertyRequest(self, apdu: WritePropertyRequest):
        """
        1) Read old value BEFORE super() call
        2) Standard handling via superclass: writes to local object
        3) Manually trigger COV via re-assignment with NEW value (bacpypes3 mechanism)
        4) If presentValue was affected: forward to Publisher (BACnet -> HA),
           unless it was an echo from HA (Echo-Guard)
        """
        # CRITICAL: Read old value BEFORE super() changes it
        old_value = None
        obj = None
        mapping = None
        oid = None
        is_present_value = apdu.propertyIdentifier in (
            "presentValue",
            PropertyIdentifier.presentValue,
        )
        try:
            if is_present_value:
                if self.publisher:
                    oid = apdu.objectIdentifier
                    obj = self.publisher.by_oid.get(oid)
                    mapping = self.publisher.map_by_oid.get(oid)
                    if obj:
                        old_value = getattr(obj, "presentValue", None)
                        _LOGGER.debug("WriteProperty BEFORE super(): %r old_value=%r", oid, old_value)
        except Exception:
            pass  # Continue even if old value read fails

        # Enforce read-only mappings before local PV mutation/COV.
        if is_present_value and self.publisher and mapping:
            if not self.publisher.is_mapping_writable(mapping):
                _LOGGER.debug(
                    "WriteProperty denied (read-only mapping): %r -> %s",
                    oid,
                    mapping.get("entity_id"),
                )
                raise ExecutionError("property", "writeAccessDenied")

        # Call superclass to perform the actual write
        await super().do_WritePropertyRequest(apdu)

        try:
            if not is_present_value:
                return

            if not self.publisher:
                return

            oid = apdu.objectIdentifier

            # Get object (might have already been retrieved above)
            if obj is None:
                obj = self.publisher.by_oid.get(oid)
            if not obj:
                _LOGGER.debug("WriteProperty: Object not found in publisher: %r", oid)
                return

            # Echo-Guard: was this change triggered by HA?
            if getattr(obj, "_ha_guard", False):
                _LOGGER.debug("WriteProperty: Echo-Guard active for %r, skipping", oid)
                return

            # Read NEW value AFTER super() wrote it
            new_value = getattr(obj, "presentValue", None)
            _LOGGER.debug("WriteProperty AFTER super(): %r old=%r new=%r",
                         oid, old_value, new_value)

            # Trigger COV by creating a REAL change that bacpypes3 will detect
            # Strategy: Set to OLD value, then to NEW value (creates detectable change)
            try:
                if old_value is not None and old_value != new_value:
                    # First set back to old value (creates change: new→old)
                    obj.presentValue = old_value
                    _LOGGER.debug("BACnet->BACnet COV-Trigger step 1: %r PV=%r -> %r",
                                 oid, new_value, old_value)

                # Then set to new value (creates change: old→new, triggers COV!)
                obj.presentValue = new_value
                _LOGGER.debug("BACnet->BACnet COV-Trigger step 2: %r PV=%r -> %r (COV sent)",
                             oid, old_value if old_value != new_value else new_value, new_value)
            except Exception as e:
                _LOGGER.error("Failed to trigger COV for %r: %s", oid, e, exc_info=True)
                raise

            mapping = self.publisher.map_by_oid.get(oid)
            if not mapping:
                _LOGGER.debug("WriteProperty: No mapping found for %r, skipping HA forwarding", oid)
                return

            # Don't block: HA service call as task
            asyncio.create_task(self.publisher.forward_to_ha_from_bacnet(mapping, new_value))
        except Exception as e:
            _LOGGER.error("Hook do_WritePropertyRequest failed for %r: %s",
                         getattr(apdu, 'objectIdentifier', 'unknown'), e, exc_info=True)

# -------------------------- Server (HA Wrapper) -----------------------------

class BacnetHubServer:
    """Starts HubApp + Publisher. Uses standard services (incl. COV)."""

    def __init__(self, hass, merged_config: Dict[str, Any]) -> None:
        self.hass = hass
        self.cfg = merged_config or {}

        self.address_str: str = _normalize_address(str(self.cfg.get("address") or ""))
        self.network_number: Optional[int] = int(self.cfg.get("network_number", 1))
        self.name: str = str(
            self.cfg.get(CONF_DEVICE_NAME)
            or self.cfg.get("name")
            or DEFAULT_BACNET_OBJECT_NAME
        )
        self.instance: int = int(self.cfg.get("instance", 8123))
        self.foreign: Optional[str] = self.cfg.get("foreign")
        self.ttl: int = int(self.cfg.get("ttl", 30))
        self.bbmd = self.cfg.get("bbmd")

        # Device metadata
        self.vendor_identifier: int = 999
        self.vendor_name: str = "magliaral"
        self.model_name: str = "BACnet Hub"
        self.description: str = str(
            self.cfg.get(CONF_DEVICE_DESCRIPTION) or DEFAULT_BACNET_DEVICE_DESCRIPTION
        )
        self.application_software_version: Optional[str] = None
        self.firmware_revision: Optional[str] = None
        self.network_port_instance: int = 1
        self.hardware_revision: str = "1.0.2"
        self.system_status: str = "operational"
        self.system_status_code: int | None = 0
        self.device_object_identifier: str = f"OBJECT_DEVICE:{self.instance}"
        self.network_port_object_identifier: str = f"OBJECT_NETWORK_PORT:{self.network_port_instance}"
        try:
            ip_addr, prefix, udp_port = _split_ip_prefix_port(self.address_str)
        except Exception:
            ip_addr, prefix, udp_port = "0.0.0.0", _DEFAULT_PREFIX, _DEFAULT_PORT
        self.ip_address: str = ip_addr
        self.network_prefix: int = prefix
        self.subnet_mask: str = _prefix_to_netmask(prefix)
        self.udp_port: int = udp_port
        self.mac_address: Optional[str] = None
        self.network_interface: Optional[str] = None

        # Debug
        self.debug_bacpypes: bool = bool(self.cfg.get("debug_bacpypes", True))
        self.kick_iam: bool = bool(self.cfg.get("kick_iam", True))

        # Mapping list
        self.published = list(self.cfg.get("published") or [])

        # Runtime
        self.publisher: Optional[BacnetPublisher] = None
        self.app: Optional[HubApp] = None
        self.device_object: Any | None = None
        self.network_port_object: Any | None = None

    async def start(self) -> None:
        if self.debug_bacpypes:
            for name in (
                "bacpypes3", "bacpypes3.app", "bacpypes3.apdu",
                "bacpypes3.pdu", "bacpypes3.service.cov"
            ):
                logging.getLogger(name).setLevel(logging.DEBUG)

        # Version info from manifest/installation
        try:
            v = await get_integration_version(self.hass, DOMAIN)
            if v:
                self.application_software_version = f"{v}"
        except Exception:
            _LOGGER.debug("Could not read application_software_version from manifest.", exc_info=True)

        try:
            bpv = await get_bacpypes3_version(self.hass)
            if bpv:
                self.firmware_revision = f"bacpypes3 v{bpv}"
        except Exception:
            _LOGGER.debug("Could not determine bacpypes3 version.", exc_info=True)

        # Check UDP port availability upfront (avoids race with HA/addon restarts)
        await _wait_port_free(self.address_str)

        # Vendor info & object classes
        vendor_info = get_vendor_info(self.vendor_identifier)
        device_object_class = vendor_info.get_object_class(ObjectType.device)
        if not device_object_class:
            raise RuntimeError("vendor identifier {self.vendor_identifier} missing device object class")
        network_port_object_class = vendor_info.get_object_class(ObjectType.networkPort)
        if not network_port_object_class:
            raise RuntimeError("vendor identifier {self.vendor_identifier} missing network port object class")

        # Address for NetworkPort
        address = self.address_str or ("host:0" if self.foreign else "host")

        # Device object
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
        self.device_object = device_object
        self.device_object_identifier = f"OBJECT_DEVICE:{self.instance}"
        status_value = getattr(device_object, "systemStatus", self.system_status)
        self.system_status_code, self.system_status = _normalize_system_status(status_value)

        # Network port object
        network_port_object = network_port_object_class(
            address,
            objectIdentifier=("network-port", int(self.network_port_instance)),
            objectName=f"NetworkPort-{self.network_port_instance}",
            networkNumber=int(self.network_number),
            networkNumberQuality="configured" if self.network_number else "unknown",
        )
        self.network_port_object = network_port_object
        self.network_port_object_identifier = (
            f"OBJECT_NETWORK_PORT:{self.network_port_instance}"
        )

        # Foreign device?
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

        # --- Create application (with services incl. COV) ---
        self.app = HubApp.from_object_list([device_object, network_port_object])

        _LOGGER.info("BACnet Hub started on %s", self.address_str)

        # Start publisher (reads/writes exclusively via services)
        if self.published:
            self.publisher = BacnetPublisher(self.hass, self.app, self.published)
            # Give the app the publisher (for BACnet→HA)
            self.app.publisher = self.publisher
            await self.publisher.start()

        # Optional: I-Am on startup (broadcast)
        if self.kick_iam and self.app:
            asyncio.create_task(self._kick_iam(self.app))

    async def _kick_iam(self, app: Application) -> None:
        try:
            await asyncio.sleep(0.2)
            dev = getattr(app, "device_object", None)
            if not dev:
                raise RuntimeError("Local device not found")
            req = IAmRequest(
                iAmDeviceIdentifier=dev.objectIdentifier,
                maxAPDULengthAccepted=int(dev.maxApduLengthAccepted),
                segmentationSupported=dev.segmentationSupported,
                vendorID=int(dev.vendorIdentifier),
            )
            req.pduDestination = Address("*:*")
            await app.request(req)
            _LOGGER.debug("I-Am sent (broadcast *:*)")
        except Exception as e:
            _LOGGER.debug("I-Am failed: %s", e, exc_info=True)

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
        self.device_object = None
        self.network_port_object = None

        _LOGGER.info("BACnet Hub stopped")
        try:
            await asyncio.sleep(0.1)
        except Exception:
            pass
