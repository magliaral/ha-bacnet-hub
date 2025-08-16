from __future__ import annotations

from typing import Dict, Optional
import voluptuous as vol

from homeassistant.core import callback
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers import selector as sel

try:
    from .const import DOMAIN  # type: ignore
except Exception:
    DOMAIN = "bacnet_hub"


class BacnetHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Minimaler Setup-Flow: erstellt sofort einen Eintrag (Server nutzt Defaults)."""
    VERSION = 1

    async def async_step_user(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        # Keine unique_id -> mehrere Instanzen möglich; bei single_config_entry im Manifest bleibt es eh bei einer
        return self.async_create_entry(title="BACnetHub", data={})

    async def async_step_import(self, user_input: Dict) -> ConfigFlowResult:
        return await self.async_step_user(user_input)

    # Diese statische Factory sorgt zuverlässig für das Zahnrad (Options-Flow)
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BacnetHubOptionsFlow(config_entry)


class BacnetHubOptionsFlow(OptionsFlow):
    """Minimaler Options-Flow: nur ein Toggle zum Verifizieren."""

    def __init__(self, config_entry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # Speichert in entry.options; Reload wird asynchron getriggert (siehe __init__.py)
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema({
            vol.Optional(
                "debug_mode",
                default=bool(self._entry.options.get("debug_mode", False))
            ): sel.BooleanSelector()
        })
        return self.async_show_form(step_id="init", data_schema=schema)
