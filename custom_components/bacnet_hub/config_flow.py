from __future__ import annotations

import logging
import re
import socket
from string import Formatter
from typing import Any, Dict, List, Optional

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import selector as sel

_LOGGER = logging.getLogger(__name__)

try:
    from .const import DOMAIN  # type: ignore
except Exception:
    DOMAIN = "bacnet_hub"

# Limits / Defaults
DEFAULT_INSTANCE = 8123
MIN_INSTANCE = 0
MAX_INSTANCE = 4_194_302
DEFAULT_PREFIX = 24
DEFAULT_PORT = 47808

# UI / Label options
UI_MODE_SIMPLE = "simple"
UI_MODE_ADVANCED = "advanced"
UI_MODE_LABEL = "label"
UI_MODES = {UI_MODE_SIMPLE, UI_MODE_ADVANCED, UI_MODE_LABEL}
DEFAULT_UI_MODE = UI_MODE_SIMPLE
DEFAULT_SHOW_TECHNICAL_IDS = False
DEFAULT_LABEL_TEMPLATE = "{friendly_name}"
MAX_LABEL_TEMPLATE_LEN = 120
ALLOWED_LABEL_FIELDS = {
    "entity_id",
    "friendly_name",
    "object_type",
    "instance",
    "writable",
    "writable_text",
}

_ADDR_RE = re.compile(
    r"^\s*(?P<ip>(\d{1,3}\.){3}\d{1,3})"
    r"(?:/(?P<prefix>\d{1,2}))?"
    r"(?::(?P<port>\d{1,5}))?\s*$"
)

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


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


def _detect_local_ip() -> Optional[str]:
    """Determine preferred IPv4 of the host."""
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
    """Validate IPv4[/prefix][:port]."""
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


def _entity_friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    """Return entity display name as shown in frontend."""
    state = hass.states.get(entity_id)
    if not state:
        return entity_id
    if getattr(state, "name", None):
        return str(state.name)
    return str(state.attributes.get("friendly_name") or entity_id)


def _normalize_ui_mode(value: Any) -> str:
    mode = str(value or DEFAULT_UI_MODE).strip().lower()
    return mode if mode in UI_MODES else DEFAULT_UI_MODE


def _show_friendly_name_field(options: Dict[str, Any]) -> bool:
    return _normalize_ui_mode(options.get("ui_mode")) in {UI_MODE_ADVANCED, UI_MODE_LABEL}


def _validate_label_template(template: str) -> Optional[str]:
    text = str(template or "").strip()
    if not text:
        return "invalid_label_template"
    if len(text) > MAX_LABEL_TEMPLATE_LEN:
        return "invalid_label_template"
    try:
        for _, field_name, _, _ in Formatter().parse(text):
            if field_name and field_name not in ALLOWED_LABEL_FIELDS:
                return "invalid_label_template"
        text.format(
            entity_id="sensor.example",
            friendly_name="Example",
            object_type="analogValue",
            instance="0",
            writable="false",
            writable_text="read-only",
        )
    except Exception:
        return "invalid_label_template"
    return None


def _label_for_mapping(hass: HomeAssistant, mapping: Dict[str, Any], options: Dict[str, Any]) -> str:
    """Build mapping label based on selected UI mode."""
    ent = str(mapping.get("entity_id") or "?")
    object_type = str(mapping.get("object_type") or "?")
    instance = mapping.get("instance", "?")
    technical_id = f"{object_type}:{instance}"
    writable = bool(mapping.get("writable", False))
    writable_text = "writable" if writable else "read-only"
    friendly_name = str(mapping.get("friendly_name") or _entity_friendly_name(hass, ent))

    mode = _normalize_ui_mode(options.get("ui_mode"))
    if mode == UI_MODE_ADVANCED:
        base = f"{technical_id} <= {ent} ({friendly_name})"
        if writable:
            base = f"{base} writable"
    elif mode == UI_MODE_LABEL:
        template = str(options.get("label_template") or DEFAULT_LABEL_TEMPLATE)
        try:
            base = template.format(
                entity_id=ent,
                friendly_name=friendly_name,
                object_type=object_type,
                instance=instance,
                writable=str(writable).lower(),
                writable_text=writable_text,
            ).strip()
        except Exception:
            base = friendly_name
        if not base:
            base = friendly_name
    else:
        base = friendly_name
        if writable:
            base = f"{base} ({writable_text})"

    show_technical_ids = bool(options.get("show_technical_ids", DEFAULT_SHOW_TECHNICAL_IDS))
    if show_technical_ids and technical_id not in base:
        base = f"{base} [{technical_id}]"

    return base


def _is_numeric_state(state: Optional[State]) -> bool:
    if not state:
        return False
    try:
        float(state.state)
        return True
    except Exception:
        return False


def _determine_object_type_and_units(
    hass: HomeAssistant, entity_id: str
) -> tuple[str, Optional[str]]:
    """
    Determine object_type (analogValue|binaryValue) + units (if available).
    """
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


class BacnetHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Setup flow: asks for instance and address."""

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
    """Options flow."""

    def __init__(self, config_entry) -> None:
        self._entry = config_entry
        self._opts: Dict[str, Any] = dict(config_entry.options or {})

    async def async_step_init(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["device", "ui", "publish"],
        )

    async def async_step_ui(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        current_mode = _normalize_ui_mode(self._opts.get("ui_mode", DEFAULT_UI_MODE))
        current_show_technical_ids = bool(
            self._opts.get("show_technical_ids", DEFAULT_SHOW_TECHNICAL_IDS)
        )
        current_label_template = str(self._opts.get("label_template") or DEFAULT_LABEL_TEMPLATE)

        errors: Dict[str, str] = {}
        if user_input is not None:
            new_mode = _normalize_ui_mode(user_input.get("ui_mode"))
            new_show_technical_ids = bool(
                user_input.get("show_technical_ids", DEFAULT_SHOW_TECHNICAL_IDS)
            )
            new_label_template = str(
                user_input.get("label_template") or DEFAULT_LABEL_TEMPLATE
            ).strip()

            err = _validate_label_template(new_label_template)
            if err:
                errors["label_template"] = err

            if not errors:
                self._opts["ui_mode"] = new_mode
                self._opts["show_technical_ids"] = new_show_technical_ids
                self._opts["label_template"] = new_label_template
                return self.async_create_entry(title="", data=self._opts)

        schema = vol.Schema(
            {
                vol.Required("ui_mode", default=current_mode): sel.SelectSelector(
                    sel.SelectSelectorConfig(
                        options=[UI_MODE_SIMPLE, UI_MODE_ADVANCED, UI_MODE_LABEL],
                        mode=sel.SelectSelectorMode.DROPDOWN,
                        translation_key="ui_mode",
                    )
                ),
                vol.Required(
                    "show_technical_ids",
                    default=current_show_technical_ids,
                ): sel.BooleanSelector(),
                vol.Required("label_template", default=current_label_template): sel.TextSelector(
                    sel.TextSelectorConfig(multiline=False, type=sel.TextSelectorType.TEXT)
                ),
            }
        )
        return self.async_show_form(step_id="ui", data_schema=schema, errors=errors)

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
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        lines: List[str] = []
        for mapping in published:
            try:
                lines.append(f"- {_label_for_mapping(self.hass, mapping, self._opts)}")
            except Exception:
                continue

        desc = "\n".join(lines) if lines else "No mappings available."
        return self.async_show_menu(
            step_id="publish",
            menu_options=["publish_add", "publish_edit", "publish_delete"],
            description_placeholders={"current_mappings": desc},
        )

    async def async_step_publish_add(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        errors: Dict[str, str] = {}
        if user_input is not None:
            entity_id = str(user_input.get("entity_id", "")).strip()
            writable = bool(user_input.get("writable", False))
            friendly_name = str(user_input.get("friendly_name") or "").strip()

            if not entity_id or "." not in entity_id:
                errors["entity_id"] = "invalid_entity"
            else:
                object_type, units = _determine_object_type_and_units(self.hass, entity_id)
                detected_friendly_name = _entity_friendly_name(self.hass, entity_id)

                counters: Dict[str, int] = dict(self._opts.get("counters", {}))
                next_idx = int(counters.get(object_type, 0))

                new_map = {
                    "entity_id": entity_id,
                    "object_type": object_type,
                    "instance": next_idx,
                    "units": units,
                    "writable": writable,
                    "friendly_name": friendly_name or detected_friendly_name,
                }

                published: List[Dict[str, Any]] = list(self._opts.get("published", []))
                published.append(new_map)
                self._opts["published"] = published

                counters[object_type] = next_idx + 1
                self._opts["counters"] = counters

                return self.async_create_entry(title="", data=self._opts)

        schema_fields: Dict[Any, Any] = {
            vol.Required("entity_id"): sel.EntitySelector(sel.EntitySelectorConfig()),
            vol.Required("writable", default=False): sel.BooleanSelector(),
        }
        if _show_friendly_name_field(self._opts):
            schema_fields[vol.Optional("friendly_name", default="")] = sel.TextSelector(
                sel.TextSelectorConfig(multiline=False, type=sel.TextSelectorType.TEXT)
            )

        schema = vol.Schema(schema_fields)
        return self.async_show_form(step_id="publish_add", data_schema=schema, errors=errors)

    async def async_step_publish_edit(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        if not published:
            return await self.async_step_publish()

        options = {
            str(i): _label_for_mapping(self.hass, mapping, self._opts)
            for i, mapping in enumerate(published)
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
            return await self.async_step_publish()

        current = dict(published[idx])
        cur_entity = current.get("entity_id", "")
        cur_writable = bool(current.get("writable", False))
        cur_friendly = current.get("friendly_name") or _entity_friendly_name(self.hass, cur_entity)
        show_friendly_name_field = _show_friendly_name_field(self._opts)

        errors: Dict[str, str] = {}
        if user_input is not None:
            new_entity = str(user_input.get("entity_id") or "").strip()
            new_writable = bool(user_input.get("writable", False))
            new_friendly = str(user_input.get("friendly_name") or "").strip()

            if not new_entity or "." not in new_entity:
                errors["entity_id"] = "invalid_entity"
            else:
                current["entity_id"] = new_entity
                current["writable"] = new_writable
                if show_friendly_name_field:
                    current["friendly_name"] = (
                        new_friendly or _entity_friendly_name(self.hass, new_entity)
                    )
                else:
                    current["friendly_name"] = _entity_friendly_name(self.hass, new_entity)

                published[idx] = current
                self._opts["published"] = published
                self._opts.pop("_edit_index", None)
                return self.async_create_entry(title="", data=self._opts)

        schema_fields: Dict[Any, Any] = {
            vol.Required("entity_id", default=cur_entity): sel.EntitySelector(
                sel.EntitySelectorConfig()
            ),
            vol.Required("writable", default=cur_writable): sel.BooleanSelector(),
        }
        if show_friendly_name_field:
            schema_fields[vol.Optional("friendly_name", default=cur_friendly)] = sel.TextSelector(
                sel.TextSelectorConfig(multiline=False, type=sel.TextSelectorType.TEXT)
            )

        schema = vol.Schema(schema_fields)
        return self.async_show_form(
            step_id="publish_edit_form",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "mapping_label": _label_for_mapping(self.hass, current, self._opts),
            },
        )

    async def async_step_publish_delete(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        if not published:
            return await self.async_step_publish()

        options = {
            str(i): _label_for_mapping(self.hass, mapping, self._opts)
            for i, mapping in enumerate(published)
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
