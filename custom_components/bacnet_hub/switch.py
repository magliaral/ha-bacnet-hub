from __future__ import annotations

from typing import Any, Dict, List

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .client_point_entities import BacnetClientPointSwitch
from .const import (
    CONF_ADDRESS,
    CONF_INSTANCE,
    DOMAIN,
    hub_display_name,
    published_observer_is_config,
    published_observer_platform,
)
from .published_control_entities import BacnetPublishedSwitchObserver
from .core.cache import _entry_client_points, _entry_points_signal, _point_platform, _to_int


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    data = hass.data[DOMAIN]
    published: List[Dict[str, Any]] = data.get("published", {}).get(entry.entry_id, []) or []
    merged = {**(entry.data or {}), **(entry.options or {})}
    hub_instance = merged.get(CONF_INSTANCE, 0)
    hub_address = merged.get(CONF_ADDRESS, "")
    hub_name = hub_display_name(hub_instance)

    published_entities: list[BacnetPublishedSwitchObserver] = []
    for m in published:
        if published_observer_platform(dict(m or {})) != "switch":
            continue
        ent_id = m.get("entity_id")
        if not ent_id:
            continue
        instance = int(m.get("instance", 0))
        source_attr = m.get("source_attr")
        read_attr = m.get("read_attr")
        hvac_on_mode = m.get("hvac_on_mode")
        friendly = m.get("friendly_name")
        name = f"(BV-{instance}) {friendly}"
        published_entities.append(
            BacnetPublishedSwitchObserver(
                hass=hass,
                entry_id=entry.entry_id,
                hub_instance=hub_instance,
                hub_address=hub_address,
                hub_name=hub_name,
                source_entity_id=ent_id,
                instance=instance,
                name=name,
                source_attr=source_attr,
                read_attr=read_attr,
                hvac_on_mode=hvac_on_mode,
                is_config=published_observer_is_config(dict(m or {})),
            )
        )
    if published_entities:
        async_add_entities(published_entities)

    added: set[tuple[str, str]] = set()

    @callback
    def _add_missing(_payload=None) -> None:
        entities: list[BacnetClientPointSwitch] = []
        per_entry = _entry_client_points(hass, entry.entry_id)
        for client_id, point_cache in per_entry.items():
            for point_key, point in sorted(point_cache.items()):
                if _point_platform(dict(point or {})) != "switch":
                    continue
                key = (str(client_id), str(point_key))
                if key in added:
                    continue
                client_instance = _to_int((point or {}).get("client_instance"))
                if client_instance is None:
                    client_instance = _to_int(str(client_id).split("_")[-1]) or 0
                entities.append(
                    BacnetClientPointSwitch(
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
