from __future__ import annotations
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from .const import DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required("address"): str,
        vol.Optional("port", default=47808): int,
        vol.Optional("broadcastAddress"): str,
        vol.Optional("instance", default=500000): int,
        vol.Optional("device_name", default="BACnet Hub"): str,
        vol.Optional("objects_yaml"): selector.Selector(
            {"text": {"multiline": True}}
        ),
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                title=user_input.get("device_name") or "BACnet Hub",
                data=user_input,
            )
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)

    async def async_step_import(self, user_input=None):
        return await self.async_step_user(user_input)


class OptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        return await self.async_step_edit()

    async def async_step_edit(self, user_input=None):
        data = self.config_entry.data | self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required("address", default=data.get("address", "")): str,
                vol.Optional("port", default=data.get("port", 47808)): int,
                vol.Optional(
                    "broadcastAddress", default=data.get("broadcastAddress", "")
                ): str,
                vol.Optional("instance", default=data.get("instance", 500000)): int,
                vol.Optional(
                    "device_name", default=data.get("device_name", "BACnet Hub")
                ): str,
                vol.Optional("objects_yaml", default=data.get("objects_yaml", "")): selector.Selector(
                    {"text": {"multiline": True}}
                ),
            }
        )
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(step_id="edit", data_schema=schema)
