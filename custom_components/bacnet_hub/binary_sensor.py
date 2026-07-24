from __future__ import annotations

from typing import Any, Dict, List

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .binary_sensor_entities import BacnetPublishedBinarySensor
from .client_point_entities import BacnetClientPointBinarySensor
from .const import (
    CONF_ADDRESS,
    CONF_INSTANCE,
    DOMAIN,
    hub_display_name,
    published_observer_is_config,
    published_observer_platform,
)
from .client_runtime import _point_platform, _setup_client_point_platform


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    data = hass.data[DOMAIN]
    published: List[Dict[str, Any]] = data.get("published", {}).get(entry.entry_id, []) or []
    merged = {**(entry.data or {}), **(entry.options or {})}
    hub_instance = merged.get(CONF_INSTANCE, 0)
    hub_address = merged.get(CONF_ADDRESS, "")
    hub_name = hub_display_name(hub_instance)

    entities: List[Any] = []
    for m in published:
        if published_observer_platform(dict(m or {})) != "binary_sensor":
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
        entities.append(
            BacnetPublishedBinarySensor(
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
    if entities:
        async_add_entities(entities)

    _setup_client_point_platform(
        hass,
        entry,
        async_add_entities,
        match=lambda point: _point_platform(point) == "binary_sensor",
        build=lambda client_id, client_instance, point_key, point: BacnetClientPointBinarySensor(
            hass=hass,
            entry_id=entry.entry_id,
            client_id=client_id,
            client_instance=client_instance,
            point_key=point_key,
        ),
    )


