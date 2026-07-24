from __future__ import annotations

from typing import Any, Dict, List

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .client_point_entities import BacnetClientPointSelect, BacnetClientWritePrioritySelect
from .const import (
    CONF_ADDRESS,
    CONF_INSTANCE,
    DOMAIN,
    hub_display_name,
    published_observer_is_config,
    published_observer_platform,
)
from .published_point_entities import BacnetPublishedSelectObserver
from .client_runtime import (
    _point_is_commandable,
    _point_platform,
    _setup_client_point_platform,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    data = hass.data[DOMAIN]
    published: List[Dict[str, Any]] = data.get("published", {}).get(entry.entry_id, []) or []
    merged = {**(entry.data or {}), **(entry.options or {})}
    hub_instance = merged.get(CONF_INSTANCE, 0)
    hub_address = merged.get(CONF_ADDRESS, "")
    hub_name = hub_display_name(hub_instance)

    published_entities: list[BacnetPublishedSelectObserver] = []
    for m in published:
        if published_observer_platform(dict(m or {})) != "select":
            continue
        ent_id = m.get("entity_id")
        if not ent_id:
            continue
        instance = int(m.get("instance", 0))
        source_attr = m.get("source_attr")
        read_attr = m.get("read_attr")
        friendly = m.get("friendly_name")
        name = f"(MV-{instance}) {friendly}"
        published_entities.append(
            BacnetPublishedSelectObserver(
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
                options=list(m.get("mv_states") or []),
                is_config=published_observer_is_config(dict(m or {})),
            )
        )
    if published_entities:
        async_add_entities(published_entities)

    _setup_client_point_platform(
        hass,
        entry,
        async_add_entities,
        match=lambda point: _point_platform(point) == "select",
        build=lambda client_id, client_instance, point_key, point: BacnetClientPointSelect(
            hass=hass,
            entry_id=entry.entry_id,
            client_id=client_id,
            client_instance=client_instance,
            point_key=point_key,
        ),
        # One write-priority select per client device, created with its first
        # known commandable point.
        client_match=_point_is_commandable,
        client_build=lambda client_id, client_instance: BacnetClientWritePrioritySelect(
            hass=hass,
            entry_id=entry.entry_id,
            client_id=client_id,
            client_instance=client_instance,
        ),
    )



