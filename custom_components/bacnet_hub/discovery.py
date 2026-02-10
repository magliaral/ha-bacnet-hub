from __future__ import annotations

from typing import Any, Iterable, Optional

from homeassistant.core import HomeAssistant, State

_BINARY_DOMAINS = {
    "binary_sensor",
    "switch",
    "light",
    "lock",
    "cover",
    "input_boolean",
    "alarm_control_panel",
    "device_tracker",
    "button",
}


def _safe_iter(values: Any) -> Iterable[Any]:
    if values is None:
        return []
    if isinstance(values, dict):
        return values.values()
    if hasattr(values, "values"):
        try:
            return values.values()
        except Exception:
            return values
    return values


def _get_entity_registry(hass: HomeAssistant) -> tuple[Any, Any]:
    try:
        from homeassistant.helpers import entity_registry as er

        return er, er.async_get(hass)
    except Exception:
        return None, None


def _get_device_registry(hass: HomeAssistant) -> tuple[Any, Any]:
    try:
        from homeassistant.helpers import device_registry as dr

        return dr, dr.async_get(hass)
    except Exception:
        return None, None


def _get_area_registry(hass: HomeAssistant) -> tuple[Any, Any]:
    try:
        from homeassistant.helpers import area_registry as ar

        return ar, ar.async_get(hass)
    except Exception:
        return None, None


def _get_label_registry(hass: HomeAssistant) -> tuple[Any, Any]:
    try:
        from homeassistant.helpers import label_registry as lr

        return lr, lr.async_get(hass)
    except Exception:
        return None, None


def _entity_registry_entries(er_mod: Any, ent_reg: Any) -> list[Any]:
    if er_mod is None or ent_reg is None:
        return []
    try:
        if hasattr(er_mod, "async_entries"):
            return list(er_mod.async_entries(ent_reg))
        if hasattr(ent_reg, "entities"):
            return list(_safe_iter(ent_reg.entities))
    except Exception:
        return []
    return []


def _device_entry(dev_reg: Any, device_id: str) -> Any:
    if not dev_reg or not device_id:
        return None
    try:
        if hasattr(dev_reg, "async_get"):
            return dev_reg.async_get(device_id)
        if hasattr(dev_reg, "devices"):
            return dev_reg.devices.get(device_id)
    except Exception:
        return None
    return None


def _is_numeric_state(state: Optional[State]) -> bool:
    if not state:
        return False
    try:
        float(state.state)
        return True
    except Exception:
        return False


def entity_friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    if not state:
        return entity_id
    if getattr(state, "name", None):
        return str(state.name)
    return str(state.attributes.get("friendly_name") or entity_id)


def determine_object_type_and_units(
    hass: HomeAssistant, entity_id: str
) -> tuple[str, Optional[str]]:
    domain = (entity_id.split(".", 1)[0] if "." in entity_id else "").lower()
    state = hass.states.get(entity_id)

    if domain in _BINARY_DOMAINS:
        return "binaryValue", None

    uom = state.attributes.get("unit_of_measurement") if state else None
    if uom or _is_numeric_state(state):
        return "analogValue", str(uom) if uom is not None else None

    if state:
        txt = str(state.state).strip().lower()
        if txt in ("on", "off", "open", "closed", "true", "false", "active", "inactive"):
            return "binaryValue", None

    return "binaryValue", None


def is_supported_entity(hass: HomeAssistant, entity_id: str) -> bool:
    if not entity_id or "." not in entity_id:
        return False
    return hass.states.get(entity_id) is not None


def area_choices(hass: HomeAssistant) -> list[tuple[str, str]]:
    ar_mod, area_reg = _get_area_registry(hass)
    if not ar_mod or not area_reg:
        return []

    try:
        if hasattr(ar_mod, "async_list_areas"):
            areas = ar_mod.async_list_areas(area_reg)
        elif hasattr(ar_mod, "async_entries"):
            areas = ar_mod.async_entries(area_reg)
        elif hasattr(area_reg, "areas"):
            areas = _safe_iter(area_reg.areas)
        else:
            areas = []
    except Exception:
        areas = []

    result: list[tuple[str, str]] = []
    for area in areas:
        area_id = getattr(area, "id", None) or getattr(area, "area_id", None)
        name = getattr(area, "name", None) or str(area_id or "")
        if area_id:
            result.append((str(area_id), str(name)))

    result.sort(key=lambda x: x[1].lower())
    return result


def label_choices(hass: HomeAssistant) -> list[tuple[str, str]]:
    lr_mod, label_reg = _get_label_registry(hass)
    if not lr_mod or not label_reg:
        return []

    try:
        if hasattr(lr_mod, "async_list_labels"):
            labels = lr_mod.async_list_labels(label_reg)
        elif hasattr(lr_mod, "async_entries"):
            labels = lr_mod.async_entries(label_reg)
        elif hasattr(label_reg, "labels"):
            labels = _safe_iter(label_reg.labels)
        else:
            labels = []
    except Exception:
        labels = []

    result: list[tuple[str, str]] = []
    for label in labels:
        label_id = getattr(label, "id", None) or getattr(label, "label_id", None)
        name = getattr(label, "name", None) or str(label_id or "")
        if label_id:
            result.append((str(label_id), str(name)))

    result.sort(key=lambda x: x[1].lower())
    return result


def supported_entities_for_device(hass: HomeAssistant, device_id: str) -> list[str]:
    er_mod, ent_reg = _get_entity_registry(hass)
    entries = _entity_registry_entries(er_mod, ent_reg)
    if not entries:
        return []

    entity_ids: list[str] = []
    for entry in entries:
        if getattr(entry, "device_id", None) != device_id:
            continue
        if getattr(entry, "disabled_by", None) is not None:
            continue
        entity_id = getattr(entry, "entity_id", None)
        if not entity_id or not is_supported_entity(hass, entity_id):
            continue
        entity_ids.append(entity_id)
    entity_ids.sort()
    return entity_ids


def entity_ids_for_label(hass: HomeAssistant, label_id: str) -> set[str]:
    if not label_id:
        return set()

    er_mod, ent_reg = _get_entity_registry(hass)
    dr_mod, dev_reg = _get_device_registry(hass)
    entries = _entity_registry_entries(er_mod, ent_reg)

    result: set[str] = set()
    for entry in entries:
        if getattr(entry, "disabled_by", None) is not None:
            continue
        entity_id = getattr(entry, "entity_id", None)
        if not entity_id or not is_supported_entity(hass, entity_id):
            continue

        ent_labels = set(getattr(entry, "labels", set()) or set())
        if label_id in ent_labels:
            result.add(entity_id)
            continue

        device_id = getattr(entry, "device_id", None)
        if not device_id or not dr_mod or not dev_reg:
            continue
        device = _device_entry(dev_reg, device_id)
        dev_labels = set(getattr(device, "labels", set()) or set()) if device else set()
        if label_id in dev_labels:
            result.add(entity_id)

    return result


def entity_ids_for_areas(hass: HomeAssistant, area_ids: set[str]) -> set[str]:
    if not area_ids:
        return set()

    er_mod, ent_reg = _get_entity_registry(hass)
    dr_mod, dev_reg = _get_device_registry(hass)
    entries = _entity_registry_entries(er_mod, ent_reg)

    result: set[str] = set()
    for entry in entries:
        if getattr(entry, "disabled_by", None) is not None:
            continue
        entity_id = getattr(entry, "entity_id", None)
        if not entity_id or not is_supported_entity(hass, entity_id):
            continue

        ent_area_id = getattr(entry, "area_id", None)
        if ent_area_id in area_ids:
            result.add(entity_id)
            continue

        device_id = getattr(entry, "device_id", None)
        if not device_id or not dr_mod or not dev_reg:
            continue
        device = _device_entry(dev_reg, device_id)
        dev_area_id = getattr(device, "area_id", None) if device else None
        if dev_area_id in area_ids:
            result.add(entity_id)

    return result
