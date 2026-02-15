from __future__ import annotations

from typing import Any

SUPPORTED_TYPES = {"binaryValue", "analogValue", "multiStateValue"}


def entity_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def object_name(entity_id: str, source_attr: Any) -> str:
    src = str(source_attr or "").strip()
    return entity_id if not src else f"{entity_id}.{src}"


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0

    text = str(value).strip().lower()
    if not text:
        return False

    if "inactive" in text:
        return False
    if "active" in text:
        return True

    if text in ("0", "false", "off", "closed"):
        return False
    return text in ("1", "true", "on", "open", "heat", "cool", "heating", "cooling")


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default

