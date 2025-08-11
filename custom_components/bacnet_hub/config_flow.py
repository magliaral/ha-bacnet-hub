from __future__ import annotations
from typing import Any
from homeassistant import config_entries
from homeassistant.core import callback
import voluptuous as vol
from homeassistant.helpers import selector
from .const import DOMAIN, CONF_DEVICE_ID, CONF_ADDRESS, CONF_PORT, CONF_BBMD_IP, CONF_BBMD_TTL, CONF_OBJECTS

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="BACnet Hub", data=user_input)

        schema = vol.Schema({
            vol.Required(CONF_DEVICE_ID, default=500000): int,
            vol.Optional(CONF_ADDRESS, default="0.0.0.0"): str,
            vol.Optional(CONF_PORT, default=47808): int,
            vol.Optional(CONF_BBMD_IP): str,
            vol.Optional(CONF_BBMD_TTL, default=600): int,
        })
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler(config_entry)

class OptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry):
        self.config_entry = config_entry
        self._data: dict[str, Any] = dict(config_entry.options or {})

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data[CONF_OBJECTS] = user_input.get(CONF_OBJECTS, [])
            return self.async_create_entry(title="", data=self._data)

        schema = vol.Schema({
            vol.Optional(CONF_OBJECTS, default=self._data.get(CONF_OBJECTS, [])): selector.ObjectSelector(),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
