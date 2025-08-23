# custom_components/bacnet_hub/mapping.py
from __future__ import annotations

from typing import Any, Dict, List
import voluptuous as vol
from homeassistant.helpers import selector as sel

# Welche Objekt-Typen dürfen über "Publish" erzeugt werden?
OBJECT_TYPES: List[str] = [
    "analogValue",
    "binaryValue",
]

# Zähler pro Typ – wird genutzt, um Instanzen fortlaufend zu vergeben
DEFAULT_COUNTERS: Dict[str, int] = {t: 0 for t in OBJECT_TYPES}


def next_instance_for_type(obj_type: str, counters: Dict[str, int]) -> int:
    """Hole die nächste freie Instanznummer für obj_type und zähle hoch."""
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
    Bringt die gespeicherten Publish-Mappings in eine robuste Form.
    Erwartete Keys pro Eintrag:
      - entity_id: str
      - object_type: str (in OBJECT_TYPES)
      - instance: int
      - units: Optional[str]
      - writable: bool
    Fremde Keys bleiben unangetastet (für spätere Erweiterungen).
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
        writable = bool(it.get("writable", False))
        # Original dict kopieren, dann Pflichtfelder normiert einsetzen
        cleaned = dict(it)
        cleaned.update(
            entity_id=ent,
            object_type=typ,
            instance=inst,
            units=units if (units is None or isinstance(units, str)) else str(units),
            writable=writable,
        )
        result.append(cleaned)
    return result


# -------------------------- Schemas für den Options-Flow --------------------------

def schema_publish_add(default_obj_type: str, default_instance: int) -> vol.Schema:
    """
    Schema für "Publish – hinzufügen".
    - entity_id: Home-Assistant-Entität (frei wählbar)
    - object_type: analogValue | binaryValue
    - instance: Nummer (auto vorbefüllt & fortlaufend)
    - units: optionaler Text (nur für AV sinnvoll)
    - writable: bool
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
        vol.Required("writable", default=False): sel.BooleanSelector(),
    })


def schema_publish_edit(current: Dict[str, Any]) -> vol.Schema:
    """
    Schema für "Publish – bearbeiten".
    Alle Felder mit aktuellen Werten vorbelegen.
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
        vol.Required("writable", default=bool(current.get("writable", False))): sel.BooleanSelector(),
        # Dummy-Feld, damit der Flow erkennen kann, dass die Seite bestätigt wurde
        vol.Required("apply", default=True): sel.BooleanSelector(),
    })
