from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .client_point_entities import BacnetClientPointReleaseButton
from .client_runtime import _point_is_commandable, _setup_client_point_platform


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    _setup_client_point_platform(
        hass,
        entry,
        async_add_entities,
        match=_point_is_commandable,
        build=lambda client_id, client_instance, point_key, point: BacnetClientPointReleaseButton(
            hass=hass,
            entry_id=entry.entry_id,
            client_id=client_id,
            client_instance=client_instance,
            point_key=point_key,
        ),
    )
