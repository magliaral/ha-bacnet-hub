from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .client_point_entities import BacnetClientPointText
from .sensor_helpers import _entry_client_points, _entry_points_signal, _point_platform, _to_int


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    added: set[tuple[str, str]] = set()

    @callback
    def _add_missing(_payload=None) -> None:
        entities: list[BacnetClientPointText] = []
        per_entry = _entry_client_points(hass, entry.entry_id)
        for client_id, point_cache in per_entry.items():
            for point_key, point in sorted(point_cache.items()):
                if _point_platform(dict(point or {})) != "text":
                    continue
                key = (str(client_id), str(point_key))
                if key in added:
                    continue
                client_instance = _to_int((point or {}).get("client_instance"))
                if client_instance is None:
                    client_instance = _to_int(str(client_id).split("_")[-1]) or 0
                entities.append(
                    BacnetClientPointText(
                        hass=hass,
                        entry_id=entry.entry_id,
                        client_id=str(client_id),
                        client_instance=int(client_instance),
                        point_key=str(point_key),
                    )
                )
                added.add(key)
        if entities:
            async_add_entities(entities)

    _add_missing()
    unsub = async_dispatcher_connect(hass, _entry_points_signal(entry.entry_id), _add_missing)
    entry.async_on_unload(unsub)
