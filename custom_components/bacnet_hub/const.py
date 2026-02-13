from __future__ import annotations

import re
from typing import Any

DOMAIN = "bacnet_hub"
DEFAULT_NAME = "BACnet Hub"
DEFAULT_BACNET_OBJECT_NAME = "HA-BACnetHub"
DEFAULT_BACNET_DEVICE_DESCRIPTION = "BACnet Hub - Home Assistant Custom Integration"

CONF_ADDRESS = "address"
CONF_PORT = "port"
CONF_INSTANCE = "instance"
CONF_DEVICE_NAME = "device_name"
CONF_DEVICE_DESCRIPTION = "device_description"
CONF_OBJECTS_YAML = "objects_yaml"

CONF_PUBLISH_MODE = "publish_mode"
CONF_IMPORT_LABEL = "import_label"
CONF_IMPORT_LABELS = "import_labels"
CONF_IMPORT_AREAS = "import_areas"

PUBLISH_MODE_CLASSIC = "classic"
PUBLISH_MODE_LABELS = "labels"
PUBLISH_MODE_AREAS = "areas"

DEFAULT_PUBLISH_MODE = PUBLISH_MODE_LABELS
DEFAULT_IMPORT_LABEL_NAME = "BACnet"
DEFAULT_IMPORT_LABEL_ICON = "mdi:server-network-outline"
DEFAULT_IMPORT_LABEL_COLOR = "light-green"

MIRRORED_STATE_ATTRIBUTE_EXCLUDE = frozenset(
    {
        # Core entity presentation attributes managed by the mirror entity itself.
        "friendly_name",
        "icon",
        "unit_of_measurement",
        "device_class",
        "state_class",
        "entity_category",
    }
)


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


def _slug_part(value: Any, fallback: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return fallback
    slug = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return slug or fallback


def object_type_slug(object_type: str) -> str:
    text = str(object_type or "").strip()
    if not text:
        return "obj"
    # Convert BACnet style camelCase names to kebab-case (e.g. analogValue -> analog-value).
    return re.sub(r"(?<!^)(?=[A-Z])", "-", text).lower()


def stable_hub_key(instance: Any, address: Any) -> str:
    inst = _as_int(instance, 0)
    addr = _slug_part(address, fallback="addr_unknown")
    return f"inst_{inst}_{addr}"


def published_unique_id(
    *,
    hub_instance: Any,
    hub_address: Any,
    object_type: str,
    object_instance: Any,
) -> str:
    type_slug = object_type_slug(object_type)
    inst = _as_int(object_instance, 0)
    return f"{DOMAIN}:hub:{stable_hub_key(hub_instance, hub_address)}:{type_slug}:{inst}"


def published_suggested_object_id(object_type: str, object_instance: Any) -> str:
    # Entity object IDs must be underscore-safe; HA entity_id does not keep hyphens.
    type_slug = object_type_slug(object_type).replace("-", "_")
    inst = _as_int(object_instance, 0)
    return f"{type_slug}_{inst}"


def published_entity_id(
    entity_domain: str,
    object_type: str,
    object_instance: Any,
) -> str:
    return f"{entity_domain}.{published_suggested_object_id(object_type, object_instance)}"


def mirrored_state_attributes(attrs: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (attrs or {}).items()
        if key not in MIRRORED_STATE_ATTRIBUTE_EXCLUDE
    }
