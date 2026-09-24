"""Tests for the global write priority option and stale-entity cleanup."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.bacnet_hub as bacnet_hub_init
from custom_components.bacnet_hub.client_runtime import (
    DEFAULT_WRITE_PRIORITY,
    WRITE_PRIORITY_OPTIONS,
    _entry_write_priority,
    _normalize_write_priority,
)
from custom_components.bacnet_hub.const import CONF_WRITE_PRIORITY


def _hass_with_entry(data: dict[str, Any] | None, options: dict[str, Any] | None):
    entry = None if data is None and options is None else SimpleNamespace(data=data, options=options)
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_get_entry=lambda entry_id: entry)
    )


def test_options_and_default() -> None:
    assert WRITE_PRIORITY_OPTIONS == list(range(8, 17))
    assert DEFAULT_WRITE_PRIORITY == 8


@pytest.mark.parametrize(
    ("value", "expected"),
    [(8, 8), ("12", 12), (16, 16), (7, 8), (17, 8), ("abc", 8), (None, 8)],
)
def test_normalize_write_priority(value: Any, expected: int) -> None:
    assert _normalize_write_priority(value) == expected


def test_entry_write_priority_default_without_entry_or_option() -> None:
    assert _entry_write_priority(_hass_with_entry(None, None), "e1") == 8
    assert _entry_write_priority(_hass_with_entry({}, {}), "e1") == 8


def test_entry_write_priority_prefers_options_over_data() -> None:
    hass = _hass_with_entry({CONF_WRITE_PRIORITY: 10}, {CONF_WRITE_PRIORITY: "14"})
    assert _entry_write_priority(hass, "e1") == 14
    hass = _hass_with_entry({CONF_WRITE_PRIORITY: 10}, {})
    assert _entry_write_priority(hass, "e1") == 10


def test_entry_write_priority_invalid_falls_back() -> None:
    hass = _hass_with_entry({}, {CONF_WRITE_PRIORITY: 3})
    assert _entry_write_priority(hass, "e1") == 8


def test_cleanup_removes_stale_select_and_button_entries(monkeypatch) -> None:
    entries = [
        SimpleNamespace(domain="select", unique_id="e1-client_1-write-priority", entity_id="select.a"),
        SimpleNamespace(domain="button", unique_id="e1-client_1-point-bo-1-release", entity_id="button.b"),
        SimpleNamespace(domain="select", unique_id="e1-client_1-point-mv-2", entity_id="select.c"),
        SimpleNamespace(domain="switch", unique_id="e1-client_1-point-bo-1", entity_id="switch.d"),
    ]
    removed: list[str] = []
    registry = SimpleNamespace(async_remove=removed.append)
    monkeypatch.setattr(bacnet_hub_init.er, "async_get", lambda hass: registry)
    monkeypatch.setattr(
        bacnet_hub_init.er,
        "async_entries_for_config_entry",
        lambda reg, entry_id: list(entries),
    )

    count = bacnet_hub_init._cleanup_removed_entities(
        SimpleNamespace(), SimpleNamespace(entry_id="e1")
    )

    assert count == 2
    assert removed == ["select.a", "button.b"]


def test_enable_points_only_touches_integration_disabled_point_entries(monkeypatch) -> None:
    from homeassistant.helpers.entity_registry import RegistryEntryDisabler

    entries = [
        SimpleNamespace(unique_id="e1-client_1-point-bo-1", entity_id="switch.a",
                        disabled_by=RegistryEntryDisabler.INTEGRATION),
        SimpleNamespace(unique_id="e1-client_1-point-ai-2", entity_id="sensor.b",
                        disabled_by=RegistryEntryDisabler.USER),
        SimpleNamespace(unique_id="e1-client_1-point-bi-3", entity_id="binary_sensor.c",
                        disabled_by=None),
        SimpleNamespace(unique_id="bacnet_hub:hub:k:analogValue:1", entity_id="sensor.d",
                        disabled_by=RegistryEntryDisabler.INTEGRATION),
    ]
    updates: list[tuple[str, Any]] = []
    registry = SimpleNamespace(
        async_update_entity=lambda entity_id, **kw: updates.append((entity_id, kw))
    )
    monkeypatch.setattr(bacnet_hub_init.er, "async_get", lambda hass: registry)
    monkeypatch.setattr(
        bacnet_hub_init.er,
        "async_entries_for_config_entry",
        lambda reg, entry_id: list(entries),
    )

    count = bacnet_hub_init._enable_integration_disabled_points(
        SimpleNamespace(), SimpleNamespace(entry_id="e1")
    )

    assert count == 1
    assert updates == [("switch.a", {"disabled_by": None})]
