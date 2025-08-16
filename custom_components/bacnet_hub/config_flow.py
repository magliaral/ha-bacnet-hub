import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_ADDRESS,
    CONF_PORT,
    CONF_INSTANCE,
    CONF_DEVICE_NAME,
    CONF_BBMD_IP,
    CONF_BBMD_TTL,
    CONF_OBJECTS_YAML,
)


class BacnetHubConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BACnet Hub."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                title=user_input.get(CONF_DEVICE_NAME) or "BACnet Hub",
                data=user_input,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS, default="0.0.0.0"): str,
                vol.Required(CONF_PORT, default=47808): int,
                vol.Required(CONF_INSTANCE, default=12345): int,
                vol.Required(CONF_DEVICE_NAME, default="BACnet Hub"): str,
                vol.Optional(CONF_BBMD_IP, default=""): str,
                vol.Optional(CONF_BBMD_TTL, default=300): int,
                vol.Optional(CONF_OBJECTS_YAML, default=""): str,
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BacnetHubOptionsFlowHandler(config_entry)


class BacnetHubOptionsFlowHandler(config_entries.OptionsFlow):
    """Options for BACnet Hub."""

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_OBJECTS_YAML,
                    default=self.config_entry.options.get(CONF_OBJECTS_YAML, ""),
                ): str,
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
