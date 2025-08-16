from __future__ import annotations
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_ADDRESS,
    CONF_PORT,
    CONF_INSTANCE,
    CONF_DEVICE_NAME,
    CONF_BROADCAST,
    CONF_OBJECTS_YAML,
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Einfacher, robuster Config-Flow (ohne Frontend-Selectoren)."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            # Ein Entry pro Instanz (Mehrfachinstanzen erlaubt)
            return self.async_create_entry(
                title=user_input.get(CONF_DEVICE_NAME) or "BACnet Hub",
                data=user_input,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS, default="192.168.1.10/24"): str,
                vol.Optional(CONF_PORT, default=47808): int,
                vol.Optional(CONF_BROADCAST, default=""): str,
                vol.Optional(CONF_INSTANCE, default=500000): int,
                vol.Optional(CONF_DEVICE_NAME, default="BACnet Hub"): str,
                # YAML als normaler Text – keine speziellen Selector-Handler nötig
                vol.Optional(CONF_OBJECTS_YAML, default=""): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow(config_entry)


class OptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        data = self.config_entry.data | self.config_entry.options
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {vol.Optional(CONF_OBJECTS_YAML, default=data.get(CONF_OBJECTS_YAML, "")): str}
        )
        return self.async_show_form(step_id="init", data_schema=schema)
