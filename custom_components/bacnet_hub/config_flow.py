from __future__ import annotations

import logging
import re
import socket
from typing import Any, Dict, Optional

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector as sel

from .const import (
    CONF_IMPORT_AREAS,
    CONF_IMPORT_LABEL,
    CONF_IMPORT_LABELS,
    CONF_PUBLISH_MODE,
    DEFAULT_IMPORT_LABEL_COLOR,
    DEFAULT_IMPORT_LABEL_ICON,
    DEFAULT_IMPORT_LABEL_NAME,
    DOMAIN,
    PUBLISH_MODE_LABELS,
)
from .discovery import label_choices

_LOGGER = logging.getLogger(__name__)

# Limits / Defaults
DEFAULT_INSTANCE = 8123
MIN_INSTANCE = 0
MAX_INSTANCE = 4_194_302
DEFAULT_PREFIX = 24
DEFAULT_PORT = 47808

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


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


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


def _label_entry_id(entry: Any) -> str:
    return str(getattr(entry, "label_id", None) or getattr(entry, "id", None) or "")


def _label_entry_name(entry: Any) -> str:
    return str(getattr(entry, "name", None) or "")


def _label_entries(hass: HomeAssistant) -> tuple[Any, Any, list[Any]]:
    try:
        from homeassistant.helpers import label_registry as lr
    except Exception:
        return None, None, []

    try:
        reg = lr.async_get(hass)
    except Exception:
        return lr, None, []

    entries: list[Any] = []
    try:
        if hasattr(lr, "async_list_labels"):
            entries = list(lr.async_list_labels(reg))
        elif hasattr(lr, "async_entries"):
            entries = list(lr.async_entries(reg))
        elif hasattr(reg, "labels"):
            labels_obj = getattr(reg, "labels")
            if isinstance(labels_obj, dict):
                entries = list(labels_obj.values())
            elif hasattr(labels_obj, "values"):
                entries = list(labels_obj.values())
    except Exception:
        entries = []

    return lr, reg, entries


async def _async_ensure_default_label(hass: HomeAssistant) -> str:
    lr_mod, label_reg, entries = _label_entries(hass)
    if not lr_mod or not label_reg:
        return ""

    wanted = DEFAULT_IMPORT_LABEL_NAME.strip().lower()
    for entry in entries:
        if _label_entry_name(entry).strip().lower() == wanted:
            return _label_entry_id(entry)

    create_attempts = (
        {
            "name": DEFAULT_IMPORT_LABEL_NAME,
            "icon": DEFAULT_IMPORT_LABEL_ICON,
            "color": DEFAULT_IMPORT_LABEL_COLOR,
        },
        {
            "name": DEFAULT_IMPORT_LABEL_NAME,
            "icon": DEFAULT_IMPORT_LABEL_ICON,
        },
        {
            "name": DEFAULT_IMPORT_LABEL_NAME,
        },
    )

    for kwargs in create_attempts:
        for fn_name in ("async_create", "async_create_label"):
            fn = getattr(label_reg, fn_name, None)
            if not callable(fn):
                continue
            try:
                created = fn(**kwargs)
                created_id = _label_entry_id(created)
                if created_id:
                    _LOGGER.info("Created default label '%s' (%s)", DEFAULT_IMPORT_LABEL_NAME, created_id)
                    return created_id
            except TypeError:
                continue
            except Exception:
                _LOGGER.debug("Could not create label via %s(%s)", fn_name, kwargs, exc_info=True)

    # Final fallback: refresh and search by name again.
    _, _, refreshed = _label_entries(hass)
    for entry in refreshed:
        if _label_entry_name(entry).strip().lower() == wanted:
            return _label_entry_id(entry)

    return ""


def _current_labels_from_store(store: Dict[str, Any]) -> list[str]:
    labels = _as_string_list(store.get(CONF_IMPORT_LABELS))
    if labels:
        return labels

    legacy = str(store.get(CONF_IMPORT_LABEL) or "").strip()
    return [legacy] if legacy else []


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
                default_label_id = await _async_ensure_default_label(self.hass)
                default_labels = [default_label_id] if default_label_id else []
                data: Dict[str, Any] = {
                    "instance": inst,
                    "address": addr,
                    CONF_PUBLISH_MODE: PUBLISH_MODE_LABELS,
                    CONF_IMPORT_LABELS: default_labels,
                    "published": [],
                    "counters": {
                        "analogValue": 0,
                        "binaryValue": 0,
                        "multiStateValue": 0,
                    },
                }
                if default_labels:
                    data[CONF_IMPORT_LABEL] = default_labels[0]

                return self.async_create_entry(
                    title="BACnet - Hub",
                    data=data,
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
        entry_data = dict(config_entry.data or {})

        for key in (
            "instance",
            "address",
            "published",
            "counters",
            CONF_PUBLISH_MODE,
            CONF_IMPORT_LABELS,
            CONF_IMPORT_LABEL,
        ):
            if key not in self._opts and key in entry_data:
                self._opts[key] = entry_data[key]

        self._opts.pop("ui_mode", None)
        self._opts.pop("show_technical_ids", None)
        self._opts.pop("label_template", None)
        self._opts.pop("_edit_index", None)

    async def async_step_init(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        return await self.async_step_device(user_input)

    async def async_step_device(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        current_instance = _as_int(self._opts.get("instance", DEFAULT_INSTANCE), DEFAULT_INSTANCE)
        current_address = str(self._opts.get("address") or _default_address())

        default_label_id = await _async_ensure_default_label(self.hass)
        label_options = label_choices(self.hass)
        available_ids = {label_id for label_id, _ in label_options}

        current_labels = [
            item for item in _current_labels_from_store(self._opts) if item in available_ids
        ]
        if not current_labels and default_label_id and default_label_id in available_ids:
            current_labels = [default_label_id]

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

            selected_labels = _as_string_list(user_input.get(CONF_IMPORT_LABELS))
            selected_labels = [label_id for label_id in selected_labels if label_id in available_ids]
            if not selected_labels:
                errors[CONF_IMPORT_LABELS] = "invalid_label_selection"

            if not errors:
                previous_mode = str(self._opts.get(CONF_PUBLISH_MODE) or "").strip().lower()
                previous_labels = set(current_labels)
                new_labels = set(selected_labels)

                self._opts["instance"] = inst
                self._opts["address"] = addr
                self._opts[CONF_PUBLISH_MODE] = PUBLISH_MODE_LABELS
                self._opts[CONF_IMPORT_LABELS] = selected_labels
                self._opts[CONF_IMPORT_LABEL] = selected_labels[0]
                self._opts.pop(CONF_IMPORT_AREAS, None)

                if previous_mode != PUBLISH_MODE_LABELS or previous_labels != new_labels:
                    self._opts["published"] = []
                    self._opts["counters"] = {
                        "analogValue": 0,
                        "binaryValue": 0,
                        "multiStateValue": 0,
                    }

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
                vol.Required(CONF_IMPORT_LABELS, default=current_labels): sel.SelectSelector(
                    sel.SelectSelectorConfig(
                        options=[
                            sel.SelectOptionDict(value=label_id, label=label_name)
                            for label_id, label_name in label_options
                        ],
                        mode=sel.SelectSelectorMode.DROPDOWN,
                        multiple=True,
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
