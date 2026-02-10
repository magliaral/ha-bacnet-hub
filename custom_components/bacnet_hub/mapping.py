# custom_components/bacnet_hub/mapping.py
from __future__ import annotations

from typing import Any, Dict, List
import voluptuous as vol
from homeassistant.helpers import selector as sel

# Which object types can be created via "Publish"?
OBJECT_TYPES: List[str] = [
    "analogValue",
    "binaryValue",
]

# Counter per type – used to assign instances sequentially
DEFAULT_COUNTERS: Dict[str, int] = {t: 0 for t in OBJECT_TYPES}


def next_instance_for_type(obj_type: str, counters: Dict[str, int]) -> int:
    """Get the next free instance number for obj_type and increment."""
    if obj_type not in counters:
        counters[obj_type] = 0
    inst = counters[obj_type]
    counters[obj_type] = inst + 1
    return inst


def _coerce_int(val: Any, fb: int) -> int:
    try:
        return int(val)
    except Exception:
        return fb


def clean_published_list(items: Any) -> List[Dict[str, Any]]:
    """
    Brings the stored publish mappings into a robust form.
    Expected keys per entry:
      - entity_id: str
      - object_type: str (in OBJECT_TYPES)
      - instance: int
      - units: Optional[str]
    Foreign keys remain untouched (for future extensions).
    """
    result: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return result

    for it in items:
        if not isinstance(it, dict):
            continue
        ent = str(it.get("entity_id", "")).strip()
        typ = str(it.get("object_type", "")).strip()
        if not ent or typ not in OBJECT_TYPES:
            continue
        inst = _coerce_int(it.get("instance", 0), 0)
        units = it.get("units")
        # Copy original dict, then insert required fields normalized
        cleaned = dict(it)
        cleaned.pop("writable", None)
        cleaned.update(
            entity_id=ent,
            object_type=typ,
            instance=inst,
            units=units if (units is None or isinstance(units, str)) else str(units),
        )
        result.append(cleaned)
    return result


# -------------------------- Schemas für den Options-Flow --------------------------

def schema_publish_add(default_obj_type: str, default_instance: int) -> vol.Schema:
    """
    Schema for "Publish – add".
    - entity_id: Home Assistant entity (freely selectable)
    - object_type: analogValue | binaryValue
    - instance: number (auto-filled & sequential)
    - units: optional text (only useful for AV)
    """
    return vol.Schema({
        vol.Required("entity_id"): sel.EntitySelector(),
        vol.Required("object_type", default=default_obj_type): sel.SelectSelector(
            sel.SelectSelectorConfig(
                options=[{"label": t, "value": t} for t in OBJECT_TYPES],
                mode=sel.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required("instance", default=default_instance): sel.NumberSelector(
            sel.NumberSelectorConfig(min=0, step=1, mode=sel.NumberSelectorMode.BOX)
        ),
        vol.Optional("units", default=None): sel.TextSelector(
            sel.TextSelectorConfig(multiline=False, type=sel.TextSelectorType.TEXT)
        ),
    })


def schema_publish_edit(current: Dict[str, Any]) -> vol.Schema:
    """
    Schema for "Publish – edit".
    Pre-fill all fields with current values.
    """
    return vol.Schema({
        vol.Required("entity_id", default=current.get("entity_id", "")): sel.EntitySelector(),
        vol.Required("object_type", default=current.get("object_type", OBJECT_TYPES[0])): sel.SelectSelector(
            sel.SelectSelectorConfig(
                options=[{"label": t, "value": t} for t in OBJECT_TYPES],
                mode=sel.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required("instance", default=int(current.get("instance", 0))): sel.NumberSelector(
            sel.NumberSelectorConfig(min=0, step=1, mode=sel.NumberSelectorMode.BOX)
        ),
        vol.Optional("units", default=current.get("units")): sel.TextSelector(
            sel.TextSelectorConfig(multiline=False, type=sel.TextSelectorType.TEXT)
        ),
        # Dummy field so the flow can recognize that the page was confirmed
        vol.Required("apply", default=True): sel.BooleanSelector(),
    })
