from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from ..discovery import mapping_friendly_name, mapping_source_key
from .ha_writeback import forward_to_ha_from_bacnet, is_mapping_auto_writable
from .object_factory import create_object
from .publisher_common import SUPPORTED_TYPES
from .value_mapping import apply_from_ha, source_value

_LOGGER = logging.getLogger(__name__)


class BacnetPublisher:
    """
    Lightweight Publisher:
      - HA -> BACnet: initial + on-change via direct assignment (COV-friendly)
      - BACnet -> HA: Forwarding is called by server write handlers.
    """

    def __init__(self, hass: HomeAssistant, app: Any, mappings: List[Dict[str, Any]]):
        self.hass = hass
        self.app = app
        self._cfg = [
            m for m in (mappings or [])
            if isinstance(m, dict) and m.get("object_type") in SUPPORTED_TYPES
        ]

        self.by_source: Dict[str, Any] = {}
        self.map_by_source: Dict[str, Dict[str, Any]] = {}
        self.sources_by_entity: Dict[str, List[str]] = {}

        self.by_oid: Dict[Tuple[str, int], Any] = {}
        self.map_by_oid: Dict[Tuple[str, int], Dict[str, Any]] = {}

        self._ha_unsub: Optional[Callable[[], None]] = None

    async def start(self) -> None:
        for m in self._cfg:
            ent = str(m.get("entity_id") or "")
            if not ent:
                continue

            source_attr = m.get("source_attr")
            source_key = mapping_source_key(ent, source_attr)
            if source_key in self.by_source:
                _LOGGER.debug("Skipping duplicate source mapping for %s", source_key)
                continue

            friendly = str(m.get("friendly_name") or mapping_friendly_name(self.hass, m) or ent)
            obj = create_object(
                self.hass,
                m,
                entity_id=ent,
                source_attr=source_attr,
                friendly=friendly,
            )

            self.app.add_object(obj)
            oid = getattr(obj, "objectIdentifier", None)
            if not isinstance(oid, tuple) or len(oid) != 2:
                _LOGGER.warning("Unexpected objectIdentifier for %s: %r", source_key, oid)
                continue

            self.by_source[source_key] = obj
            self.map_by_source[source_key] = m
            self.sources_by_entity.setdefault(ent, []).append(source_key)
            self.by_oid[oid] = obj
            self.map_by_oid[oid] = m

            _LOGGER.info(
                "Published %s:%s <= %s (name=%r, desc=%r, units=%s)",
                oid[0],
                oid[1],
                source_key,
                getattr(obj, "objectName", None),
                getattr(obj, "description", None),
                getattr(obj, "units", None) if hasattr(obj, "units") else None,
            )

        await self._initial_sync()

        self._ha_unsub = async_track_state_change_event(
            self.hass,
            list(self.sources_by_entity.keys()),
            self._on_state_changed,
        )

        _LOGGER.info("BacnetPublisher running (%d mappings).", len(self.by_source))

    async def stop(self) -> None:
        if self._ha_unsub:
            try:
                self._ha_unsub()
            except Exception:
                pass
            self._ha_unsub = None

        self.by_source.clear()
        self.map_by_source.clear()
        self.sources_by_entity.clear()
        self.by_oid.clear()
        self.map_by_oid.clear()
        _LOGGER.info("BacnetPublisher stopped")

    async def update_descriptions(self) -> None:
        """Update BACnet object descriptions from current entity names."""
        for source_key, obj in self.by_source.items():
            mapping = self.map_by_source.get(source_key)
            if not mapping:
                continue

            new_friendly = mapping_friendly_name(self.hass, mapping)
            current_desc = getattr(obj, "description", None)
            if new_friendly == current_desc:
                continue

            try:
                obj.description = new_friendly
                _LOGGER.debug("Description updated for %s: %r -> %r", source_key, current_desc, new_friendly)
            except Exception as err:
                _LOGGER.debug("Could not update description for %s: %s", source_key, err)

    async def _initial_sync(self) -> None:
        for source_key, obj in self.by_source.items():
            mapping = self.map_by_source.get(source_key)
            if not mapping:
                continue

            ent = str(mapping.get("entity_id") or "")
            st = self.hass.states.get(ent)
            if not st:
                continue

            value = source_value(st, mapping)
            await apply_from_ha(obj, value, mapping)

    @callback
    async def _on_state_changed(self, event) -> None:
        if event.event_type != EVENT_STATE_CHANGED:
            return

        data = event.data or {}
        ent = data.get("entity_id")
        ns = data.get("new_state")
        if not ent or not ns:
            return

        for source_key in self.sources_by_entity.get(ent, []):
            obj = self.by_source.get(source_key)
            mapping = self.map_by_source.get(source_key)
            if not obj or not mapping:
                continue

            value = source_value(ns, mapping)
            asyncio.create_task(apply_from_ha(obj, value, mapping))

    def is_mapping_writable(self, mapping: Dict[str, Any]) -> bool:
        """Public guard used by BACnet write handler before local PV updates."""
        return is_mapping_auto_writable(self.hass, mapping)

    async def forward_to_ha_from_bacnet(self, mapping: Dict[str, Any], value: Any) -> None:
        await forward_to_ha_from_bacnet(self.hass, mapping, value)

