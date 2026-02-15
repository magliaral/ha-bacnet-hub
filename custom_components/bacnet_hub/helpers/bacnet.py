from __future__ import annotations

import re
from typing import Any

_DEFAULT_PREFIX = 24
_INSTANCE_SUFFIX_RE = re.compile(r"(?:,|:)\s*(\d+)\s*$")


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def device_instance_from_identifier(value: Any) -> int | None:
    """Extract BACnet device instance from tuple/object/text forms."""
    if isinstance(value, tuple) and len(value) == 2:
        return _to_int(value[1])

    for attr in ("instance", "objectInstance", "instanceNumber"):
        try:
            inst = _to_int(getattr(value, attr, None))
        except Exception:
            inst = None
        if inst is not None:
            return inst

    text = str(value or "").strip()
    if not text:
        return None

    match = _INSTANCE_SUFFIX_RE.search(text)
    if match:
        return _to_int(match.group(1))

    return _to_int(text)


def prefix_to_netmask(prefix: int) -> str:
    """Convert IPv4 prefix length (0..32) to dotted netmask."""
    try:
        parsed = int(prefix)
    except Exception:
        parsed = _DEFAULT_PREFIX

    if parsed < 0 or parsed > 32:
        parsed = _DEFAULT_PREFIX

    if parsed <= 0:
        return "0.0.0.0"
    if parsed >= 32:
        return "255.255.255.255"

    mask = (0xFFFFFFFF << (32 - parsed)) & 0xFFFFFFFF
    return ".".join(str((mask >> shift) & 0xFF) for shift in (24, 16, 8, 0))
