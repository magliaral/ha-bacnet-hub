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
    entity_mapping_candidates,
    entity_friendly_name,
    label_choices,
    mapping_friendly_name,
    mapping_key,
    mapping_source_key,
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
    friendly = str(mapping.get("friendly_name") or entity_friendly_name(hass, ent))
    source_attr = str(mapping.get("source_attr") or "").strip()
    source_suffix = f" [{source_attr}]" if source_attr else ""
    auto_suffix = " [auto]" if bool(mapping.get("auto", False)) else ""
    return f"{object_type}:{instance} <= {ent}{source_suffix} ({friendly}){auto_suffix}"


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
        current_mode = _normalize_publish_mode(
            self._opts.get(CONF_PUBLISH_MODE, DEFAULT_PUBLISH_MODE)
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
            mode = _normalize_publish_mode(user_input.get(CONF_PUBLISH_MODE, current_mode))

            if not errors:
                self._opts["instance"] = inst
                self._opts["address"] = addr
                self._opts[CONF_PUBLISH_MODE] = mode
                if mode != current_mode:
                    # Hard reset on mode change: remove all mappings and restart instances.
                    self._opts["published"] = []
                    self._opts["counters"] = {
                        "analogValue": 0,
                        "binaryValue": 0,
                        "multiStateValue": 0,
                    }
                    self._opts.pop("_edit_index", None)
                if mode != PUBLISH_MODE_LABELS:
                    self._opts.pop(CONF_IMPORT_LABEL, None)
                if mode != PUBLISH_MODE_AREAS:
                    self._opts.pop(CONF_IMPORT_AREAS, None)
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
        mode = _normalize_publish_mode(self._opts.get(CONF_PUBLISH_MODE, DEFAULT_PUBLISH_MODE))
        if mode == PUBLISH_MODE_LABELS:
            return await self.async_step_publish_labels_menu()
        if mode == PUBLISH_MODE_AREAS:
            return await self.async_step_publish_areas_menu()
        return await self.async_step_publish_classic()

    async def _async_step_publish_for_current_mode(self) -> ConfigFlowResult:
        return await self.async_step_publish()

    def _publish_overview(self) -> str:
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        lines: List[str] = []
        for mapping in published:
            try:
                lines.append(f"- {_mapping_label(self.hass, mapping)}")
            except Exception:
                continue
        return "\n".join(lines) if lines else "No mappings available."

    async def async_step_publish_classic(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="publish_classic",
            menu_options=["publish_add", "publish_edit", "publish_delete"],
            description_placeholders={"current_mappings": self._publish_overview()},
        )

    async def async_step_publish_labels_menu(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="publish_labels_menu",
            menu_options=["publish_labels", "publish_edit", "publish_delete"],
            description_placeholders={"current_mappings": self._publish_overview()},
        )

    async def async_step_publish_areas_menu(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="publish_areas_menu",
            menu_options=["publish_areas", "publish_edit", "publish_delete"],
            description_placeholders={"current_mappings": self._publish_overview()},
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

            if not entity_id or "." not in entity_id:
                errors["entity_id"] = "invalid_entity"
            elif self._append_mapping(entity_id, auto=False):
                return self.async_create_entry(title="", data=self._opts)
            else:
                errors["base"] = "mapping_exists"

        schema = vol.Schema(
            {
                vol.Required("entity_id"): sel.EntitySelector(sel.EntitySelectorConfig()),
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
            entity_ids = supported_entities_for_device(self.hass, device_id)
            if not entity_ids:
                errors["base"] = "no_supported_entities"
            else:
                added = 0
                for entity_id in entity_ids:
                    if self._append_mapping(entity_id, auto=False):
                        added += 1
                if added == 0:
                    errors["base"] = "no_new_entities"
                else:
                    return self.async_create_entry(title="", data=self._opts)

        schema = vol.Schema(
            {
                vol.Required("device_id"): sel.DeviceSelector(sel.DeviceSelectorConfig()),
            }
        )
        return self.async_show_form(
            step_id="publish_add_device", data_schema=schema, errors=errors
        )

    async def async_step_publish_edit(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        if not published:
            return await self._async_step_publish_for_current_mode()

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
            return await self._async_step_publish_for_current_mode()

        current = dict(published[idx])
        cur_entity = current.get("entity_id", "")
        current_auto = bool(current.get("auto", False))
        current_auto_mode = current.get("auto_mode")

        errors: Dict[str, str] = {}
        if user_input is not None:
            new_entity = str(user_input.get("entity_id") or "").strip()
            current_source_attr = current.get("source_attr")
            new_key = mapping_source_key(new_entity, current_source_attr)

            if not new_entity or "." not in new_entity:
                errors["entity_id"] = "invalid_entity"
            elif any(
                i != idx and mapping_key(item) == new_key
                for i, item in enumerate(published)
            ):
                errors["base"] = "mapping_exists"
            else:
                current["entity_id"] = new_entity
                current.pop("writable", None)
                current["friendly_name"] = mapping_friendly_name(self.hass, current)
                if new_entity == cur_entity:
                    current["auto"] = current_auto
                    current["auto_mode"] = current_auto_mode
                else:
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
            return await self._async_step_publish_for_current_mode()

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

    def _append_mapping(self, entity_id: str, auto: bool) -> bool:
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        candidates = entity_mapping_candidates(self.hass, entity_id)
        if not candidates:
            return False

        existing_keys: set[str] = {
            mapping_key(item)
            for item in published
            if isinstance(item, dict) and item.get("entity_id")
        }
        counters: Dict[str, int] = dict(self._opts.get("counters", {}))
        added = False

        for candidate in candidates:
            key = mapping_key(candidate)
            if key in existing_keys:
                continue

            object_type = str(candidate.get("object_type") or "").strip()
            if not object_type:
                continue

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

            new_map: Dict[str, Any] = {
                "entity_id": entity_id,
                "object_type": object_type,
                "instance": next_idx,
                "units": candidate.get("units"),
                "friendly_name": str(candidate.get("friendly_name") or mapping_friendly_name(self.hass, candidate)),
            }
            if candidate.get("source_attr"):
                new_map["source_attr"] = candidate.get("source_attr")
            if candidate.get("read_attr"):
                new_map["read_attr"] = candidate.get("read_attr")
            if candidate.get("write_action"):
                new_map["write_action"] = candidate.get("write_action")
            if candidate.get("mv_states"):
                new_map["mv_states"] = list(candidate.get("mv_states") or [])
            if candidate.get("hvac_on_mode"):
                new_map["hvac_on_mode"] = candidate.get("hvac_on_mode")
            if candidate.get("hvac_off_mode"):
                new_map["hvac_off_mode"] = candidate.get("hvac_off_mode")
            if candidate.get("cov_increment") is not None:
                new_map["cov_increment"] = float(candidate.get("cov_increment"))

            if auto:
                new_map["auto"] = True
                new_map["auto_mode"] = self._opts.get(CONF_PUBLISH_MODE)

            published.append(new_map)
            existing_keys.add(key)
            added = True

        if not added:
            return False

        self._opts["counters"] = counters
        self._opts["published"] = published
        return True
