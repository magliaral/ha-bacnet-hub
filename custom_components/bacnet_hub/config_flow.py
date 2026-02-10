from __future__ import annotations

import logging
import re
import socket
from typing import Any, Dict, List, Optional

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector as sel

from .const import (
    CONF_IMPORT_AREAS,
    CONF_IMPORT_LABEL,
    CONF_PUBLISH_MODE,
    DEFAULT_PUBLISH_MODE,
    DOMAIN,
    PUBLISH_MODE_AREAS,
    PUBLISH_MODE_CLASSIC,
    PUBLISH_MODE_LABELS,
)
from .discovery import (
    area_choices,
    determine_object_type_and_units,
    entity_friendly_name,
    label_choices,
    supported_entities_for_device,
)

_LOGGER = logging.getLogger(__name__)

# Limits / Defaults
DEFAULT_INSTANCE = 8123
MIN_INSTANCE = 0
MAX_INSTANCE = 4_194_302
DEFAULT_PREFIX = 24
DEFAULT_PORT = 47808

PUBLISH_MODES = {PUBLISH_MODE_CLASSIC, PUBLISH_MODE_LABELS, PUBLISH_MODE_AREAS}
PUBLISH_ADD_SOURCE_ENTITY = "entity"
PUBLISH_ADD_SOURCE_DEVICE = "device"
PUBLISH_ADD_SOURCES = {PUBLISH_ADD_SOURCE_ENTITY, PUBLISH_ADD_SOURCE_DEVICE}

_ADDR_RE = re.compile(
    r"^\s*(?P<ip>(\d{1,3}\.){3}\d{1,3})"
    r"(?:/(?P<prefix>\d{1,2}))?"
    r"(?::(?P<port>\d{1,5}))?\s*$"
)


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


def _normalize_publish_mode(value: Any) -> str:
    mode = str(value or DEFAULT_PUBLISH_MODE).strip().lower()
    return mode if mode in PUBLISH_MODES else DEFAULT_PUBLISH_MODE


def _detect_local_ip() -> Optional[str]:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        socket.inet_aton(ip)
        return ip
    except Exception as err:
        _LOGGER.debug("Could not detect local IP: %s", err)
        return None


def _default_address() -> str:
    ip = _detect_local_ip() or "0.0.0.0"
    return f"{ip}/{DEFAULT_PREFIX}:{DEFAULT_PORT}"


def _validate_bacnet_address(addr: str) -> Optional[str]:
    if not addr or not isinstance(addr, str):
        return "invalid_address"

    match = _ADDR_RE.match(addr)
    if not match:
        return "invalid_address"

    ip = match.group("ip")
    try:
        parts = [int(x) for x in ip.split(".")]
        if any(p < 0 or p > 255 for p in parts):
            return "invalid_address"
    except Exception:
        return "invalid_address"

    prefix = match.group("prefix")
    if prefix is not None:
        try:
            prefix_int = int(prefix)
            if prefix_int < 0 or prefix_int > 32:
                return "invalid_address"
        except Exception:
            return "invalid_address"

    port = match.group("port")
    if port is not None:
        try:
            port_int = int(port)
            if port_int < 1 or port_int > 65535:
                return "invalid_address"
        except Exception:
            return "invalid_address"

    return None


def _mapping_label(hass: HomeAssistant, mapping: Dict[str, Any]) -> str:
    ent = str(mapping.get("entity_id") or "?")
    object_type = str(mapping.get("object_type") or "?")
    instance = mapping.get("instance", "?")
    writable = " writable" if bool(mapping.get("writable", False)) else ""
    friendly = str(mapping.get("friendly_name") or entity_friendly_name(hass, ent))
    auto_suffix = " [auto]" if bool(mapping.get("auto", False)) else ""
    return f"{object_type}:{instance} <= {ent} ({friendly}){writable}{auto_suffix}"


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


class BacnetHubConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        errors: Dict[str, str] = {}
        placeholders = {
            "min": f"{MIN_INSTANCE}",
            "max": "4194302",
            "default": f"{DEFAULT_INSTANCE}",
            "addr_hint": "e.g. 192.168.31.36/24:47808",
        }

        if user_input is not None:
            try:
                inst = int(user_input.get("instance", DEFAULT_INSTANCE))
                if inst < MIN_INSTANCE or inst > MAX_INSTANCE:
                    raise ValueError
            except Exception:
                errors["instance"] = "invalid_int"
                inst = DEFAULT_INSTANCE

            addr = (user_input.get("address") or "").strip()
            if not addr:
                addr = _default_address()
            err = _validate_bacnet_address(addr)
            if err:
                errors["address"] = err

            if not errors:
                return self.async_create_entry(
                    title="BACnet - Hub",
                    data={"instance": inst, "address": addr},
                )

        schema = vol.Schema(
            {
                vol.Required("instance", default=DEFAULT_INSTANCE): sel.NumberSelector(
                    sel.NumberSelectorConfig(
                        min=MIN_INSTANCE,
                        max=MAX_INSTANCE,
                        step=1,
                        mode=sel.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required("address", default=_default_address()): sel.TextSelector(
                    sel.TextSelectorConfig(multiline=False, type=sel.TextSelectorType.TEXT)
                ),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_import(self, user_input: Dict) -> ConfigFlowResult:
        return await self.async_step_user(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BacnetHubOptionsFlow(config_entry)


class BacnetHubOptionsFlow(OptionsFlow):
    def __init__(self, config_entry) -> None:
        self._entry = config_entry
        self._opts: Dict[str, Any] = dict(config_entry.options or {})
        # Drop old options from removed UI tab.
        self._opts.pop("ui_mode", None)
        self._opts.pop("show_technical_ids", None)
        self._opts.pop("label_template", None)

    async def async_step_init(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["device", "publish"],
        )

    async def async_step_device(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        current_instance = _as_int(
            self._opts.get("instance", self._entry.data.get("instance", DEFAULT_INSTANCE)),
            DEFAULT_INSTANCE,
        )
        current_address = (
            self._opts.get("address") or self._entry.data.get("address") or _default_address()
        )

        placeholders = {
            "min": f"{MIN_INSTANCE}",
            "max": "4194302",
            "current": f"{current_instance}",
            "addr_hint": "e.g. 192.168.31.36/24:47808",
        }

        errors: Dict[str, str] = {}
        if user_input is not None:
            inst = _as_int(user_input.get("instance"), current_instance)
            if inst < MIN_INSTANCE or inst > MAX_INSTANCE:
                errors["instance"] = "invalid_int"

            addr = (user_input.get("address") or "").strip()
            if not addr:
                addr = current_address or _default_address()
            err = _validate_bacnet_address(addr)
            if err:
                errors["address"] = err

            if not errors:
                self._opts["instance"] = inst
                self._opts["address"] = addr
                return self.async_create_entry(title="", data=self._opts)

        schema = vol.Schema(
            {
                vol.Required("instance", default=current_instance): sel.NumberSelector(
                    sel.NumberSelectorConfig(
                        min=MIN_INSTANCE,
                        max=MAX_INSTANCE,
                        step=1,
                        mode=sel.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required("address", default=current_address): sel.TextSelector(
                    sel.TextSelectorConfig(multiline=False, type=sel.TextSelectorType.TEXT)
                ),
            }
        )
        return self.async_show_form(
            step_id="device",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_publish(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        return await self.async_step_publish_mode()

    async def async_step_publish_mode(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        current_mode = _normalize_publish_mode(
            self._opts.get(CONF_PUBLISH_MODE, DEFAULT_PUBLISH_MODE)
        )
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        mappings_count = len(published)
        auto_count = sum(1 for item in published if bool(item.get("auto", False)))

        errors: Dict[str, str] = {}
        if user_input is not None:
            mode = _normalize_publish_mode(user_input.get(CONF_PUBLISH_MODE))
            self._opts[CONF_PUBLISH_MODE] = mode
            self._opts.pop("_edit_index", None)

            if mode != PUBLISH_MODE_LABELS:
                self._opts.pop(CONF_IMPORT_LABEL, None)
            if mode != PUBLISH_MODE_AREAS:
                self._opts.pop(CONF_IMPORT_AREAS, None)

            if mode == PUBLISH_MODE_CLASSIC:
                if current_mode != PUBLISH_MODE_CLASSIC:
                    # Persist mode switch immediately so stale auto mappings are cleaned up.
                    return self.async_create_entry(title="", data=self._opts)
                return await self.async_step_publish_classic()
            if mode == PUBLISH_MODE_LABELS:
                return await self.async_step_publish_labels()
            return await self.async_step_publish_areas()

        schema = vol.Schema(
            {
                vol.Required(CONF_PUBLISH_MODE, default=current_mode): sel.SelectSelector(
                    sel.SelectSelectorConfig(
                        options=[
                            PUBLISH_MODE_CLASSIC,
                            PUBLISH_MODE_LABELS,
                            PUBLISH_MODE_AREAS,
                        ],
                        mode=sel.SelectSelectorMode.DROPDOWN,
                        translation_key="publish_mode",
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="publish_mode",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "mappings_count": str(mappings_count),
                "auto_count": str(auto_count),
            },
        )

    async def async_step_publish_classic(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        lines: List[str] = []
        for mapping in published:
            try:
                lines.append(f"- {_mapping_label(self.hass, mapping)}")
            except Exception:
                continue

        desc = "\n".join(lines) if lines else "No mappings available."
        return self.async_show_menu(
            step_id="publish_classic",
            menu_options=["publish_add", "publish_edit", "publish_delete"],
            description_placeholders={"current_mappings": desc},
        )

    async def async_step_publish_labels(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        options = label_choices(self.hass)
        current_label = str(self._opts.get(CONF_IMPORT_LABEL) or "")
        errors: Dict[str, str] = {}

        if not options:
            return self.async_show_form(
                step_id="publish_labels",
                data_schema=vol.Schema({}),
                errors={"base": "no_labels_available"},
            )

        available_ids = {opt_id for opt_id, _ in options}
        if user_input is not None:
            selected = str(user_input.get(CONF_IMPORT_LABEL) or "").strip()
            if selected not in available_ids:
                errors[CONF_IMPORT_LABEL] = "invalid_label"
            else:
                self._opts[CONF_PUBLISH_MODE] = PUBLISH_MODE_LABELS
                self._opts[CONF_IMPORT_LABEL] = selected
                self._opts.pop(CONF_IMPORT_AREAS, None)
                return self.async_create_entry(title="", data=self._opts)

        default_label = current_label if current_label in available_ids else options[0][0]
        schema = vol.Schema(
            {
                vol.Required(CONF_IMPORT_LABEL, default=default_label): sel.SelectSelector(
                    sel.SelectSelectorConfig(
                        options=[
                            sel.SelectOptionDict(value=opt_id, label=label)
                            for opt_id, label in options
                        ],
                        mode=sel.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="publish_labels", data_schema=schema, errors=errors)

    async def async_step_publish_areas(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        options = area_choices(self.hass)
        current_areas = _as_string_list(self._opts.get(CONF_IMPORT_AREAS, []))
        errors: Dict[str, str] = {}

        if not options:
            return self.async_show_form(
                step_id="publish_areas",
                data_schema=vol.Schema({}),
                errors={"base": "no_areas_available"},
            )

        available_ids = {opt_id for opt_id, _ in options}
        if user_input is not None:
            selected = _as_string_list(user_input.get(CONF_IMPORT_AREAS))
            selected = [item for item in selected if item in available_ids]
            if not selected:
                errors[CONF_IMPORT_AREAS] = "invalid_area_selection"
            else:
                self._opts[CONF_PUBLISH_MODE] = PUBLISH_MODE_AREAS
                self._opts[CONF_IMPORT_AREAS] = selected
                self._opts.pop(CONF_IMPORT_LABEL, None)
                return self.async_create_entry(title="", data=self._opts)

        default_areas = [item for item in current_areas if item in available_ids]
        schema = vol.Schema(
            {
                vol.Required(CONF_IMPORT_AREAS, default=default_areas): sel.SelectSelector(
                    sel.SelectSelectorConfig(
                        options=[
                            sel.SelectOptionDict(value=opt_id, label=label)
                            for opt_id, label in options
                        ],
                        mode=sel.SelectSelectorMode.DROPDOWN,
                        multiple=True,
                    )
                )
            }
        )
        return self.async_show_form(step_id="publish_areas", data_schema=schema, errors=errors)

    async def async_step_publish_add(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        errors: Dict[str, str] = {}
        if user_input is not None:
            source = str(user_input.get("source") or "").strip().lower()
            if source not in PUBLISH_ADD_SOURCES:
                errors["source"] = "invalid_source"
            elif source == PUBLISH_ADD_SOURCE_ENTITY:
                return await self.async_step_publish_add_entity()
            else:
                return await self.async_step_publish_add_device()

        schema = vol.Schema(
            {
                vol.Required("source", default=PUBLISH_ADD_SOURCE_ENTITY): sel.SelectSelector(
                    sel.SelectSelectorConfig(
                        options=[PUBLISH_ADD_SOURCE_ENTITY, PUBLISH_ADD_SOURCE_DEVICE],
                        mode=sel.SelectSelectorMode.DROPDOWN,
                        translation_key="publish_add_source",
                    )
                )
            }
        )
        return self.async_show_form(step_id="publish_add", data_schema=schema, errors=errors)

    async def async_step_publish_add_entity(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        errors: Dict[str, str] = {}
        if user_input is not None:
            entity_id = str(user_input.get("entity_id", "")).strip()
            writable = bool(user_input.get("writable", False))

            if not entity_id or "." not in entity_id:
                errors["entity_id"] = "invalid_entity"
            elif self._append_mapping(entity_id, writable, auto=False):
                return self.async_create_entry(title="", data=self._opts)
            else:
                errors["base"] = "mapping_exists"

        schema = vol.Schema(
            {
                vol.Required("entity_id"): sel.EntitySelector(sel.EntitySelectorConfig()),
                vol.Required("writable", default=False): sel.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="publish_add_entity", data_schema=schema, errors=errors
        )

    async def async_step_publish_add_device(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        errors: Dict[str, str] = {}
        if user_input is not None:
            device_id = str(user_input.get("device_id", "")).strip()
            writable = bool(user_input.get("writable", False))
            entity_ids = supported_entities_for_device(self.hass, device_id)
            if not entity_ids:
                errors["base"] = "no_supported_entities"
            else:
                added = 0
                for entity_id in entity_ids:
                    if self._append_mapping(entity_id, writable, auto=False):
                        added += 1
                if added == 0:
                    errors["base"] = "no_new_entities"
                else:
                    return self.async_create_entry(title="", data=self._opts)

        schema = vol.Schema(
            {
                vol.Required("device_id"): sel.DeviceSelector(sel.DeviceSelectorConfig()),
                vol.Required("writable", default=False): sel.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="publish_add_device", data_schema=schema, errors=errors
        )

    async def async_step_publish_edit(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        if not published:
            return await self.async_step_publish_classic()

        options = {
            str(i): _mapping_label(self.hass, mapping) for i, mapping in enumerate(published)
        }

        if user_input is not None and "sel" in user_input:
            idx = str(user_input.get("sel"))
            if idx in options:
                self._opts["_edit_index"] = int(idx)
                return await self.async_step_publish_edit_form()

        schema = vol.Schema(
            {
                vol.Required("sel"): sel.SelectSelector(
                    sel.SelectSelectorConfig(
                        options=[sel.SelectOptionDict(value=k, label=v) for k, v in options.items()],
                        mode=sel.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="publish_edit", data_schema=schema)

    async def async_step_publish_edit_form(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        idx = int(self._opts.get("_edit_index", -1))
        if idx < 0 or idx >= len(published):
            return await self.async_step_publish_classic()

        current = dict(published[idx])
        cur_entity = current.get("entity_id", "")
        cur_writable = bool(current.get("writable", False))

        errors: Dict[str, str] = {}
        if user_input is not None:
            new_entity = str(user_input.get("entity_id") or "").strip()
            new_writable = bool(user_input.get("writable", False))

            if not new_entity or "." not in new_entity:
                errors["entity_id"] = "invalid_entity"
            elif any(
                i != idx and item.get("entity_id") == new_entity
                for i, item in enumerate(published)
            ):
                errors["base"] = "mapping_exists"
            else:
                current["entity_id"] = new_entity
                current["writable"] = new_writable
                current["friendly_name"] = entity_friendly_name(self.hass, new_entity)
                current["auto"] = False
                current["auto_mode"] = None

                published[idx] = current
                self._opts["published"] = published
                self._opts.pop("_edit_index", None)
                return self.async_create_entry(title="", data=self._opts)

        schema = vol.Schema(
            {
                vol.Required("entity_id", default=cur_entity): sel.EntitySelector(
                    sel.EntitySelectorConfig()
                ),
                vol.Required("writable", default=cur_writable): sel.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="publish_edit_form",
            data_schema=schema,
            errors=errors,
            description_placeholders={"mapping_label": _mapping_label(self.hass, current)},
        )

    async def async_step_publish_delete(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        if not published:
            return await self.async_step_publish_classic()

        options = {
            str(i): _mapping_label(self.hass, mapping) for i, mapping in enumerate(published)
        }

        if user_input is not None and "sel" in user_input:
            idx = str(user_input.get("sel"))
            if idx in options:
                published.pop(int(idx))
                self._opts["published"] = published
                return self.async_create_entry(title="", data=self._opts)

        schema = vol.Schema(
            {
                vol.Required("sel"): sel.SelectSelector(
                    sel.SelectSelectorConfig(
                        options=[sel.SelectOptionDict(value=k, label=v) for k, v in options.items()],
                        mode=sel.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="publish_delete", data_schema=schema)

    def _append_mapping(self, entity_id: str, writable: bool, auto: bool) -> bool:
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        if any((m.get("entity_id") == entity_id) for m in published):
            return False

        object_type, units = determine_object_type_and_units(self.hass, entity_id)
        counters: Dict[str, int] = dict(self._opts.get("counters", {}))

        max_for_type = -1
        for item in published:
            if item.get("object_type") != object_type:
                continue
            inst = _as_int(item.get("instance"), -1)
            if inst > max_for_type:
                max_for_type = inst
        floor = max_for_type + 1
        next_idx = max(_as_int(counters.get(object_type), 0), floor)
        counters[object_type] = next_idx + 1
        self._opts["counters"] = counters

        new_map: Dict[str, Any] = {
            "entity_id": entity_id,
            "object_type": object_type,
            "instance": next_idx,
            "units": units,
            "writable": writable,
            "friendly_name": entity_friendly_name(self.hass, entity_id),
        }
        if auto:
            new_map["auto"] = True
            new_map["auto_mode"] = self._opts.get(CONF_PUBLISH_MODE)

        published.append(new_map)
        self._opts["published"] = published
        return True
