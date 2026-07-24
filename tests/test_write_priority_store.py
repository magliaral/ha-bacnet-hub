from __future__ import annotations

from types import SimpleNamespace

from custom_components.bacnet_hub.client_runtime import (
    DEFAULT_WRITE_PRIORITY,
    _client_write_priority_get,
    _client_write_priority_set,
)
from custom_components.bacnet_hub.const import DOMAIN


def _fake_hass() -> SimpleNamespace:
    return SimpleNamespace(data={})


def test_default_priority() -> None:
    hass = _fake_hass()
    assert _client_write_priority_get(hass, "entry1", "client_5") == DEFAULT_WRITE_PRIORITY


def test_set_and_get_roundtrip() -> None:
    hass = _fake_hass()
    assert _client_write_priority_set(hass, "entry1", "client_5", 8) == 8
    assert _client_write_priority_get(hass, "entry1", "client_5") == 8
    assert _client_write_priority_set(hass, "entry1", "client_5", "12") == 12
    assert _client_write_priority_get(hass, "entry1", "client_5") == 12


def test_invalid_values_fall_back_to_default() -> None:
    hass = _fake_hass()
    assert _client_write_priority_set(hass, "entry1", "client_5", "abc") == DEFAULT_WRITE_PRIORITY
    assert _client_write_priority_set(hass, "entry1", "client_5", None) == DEFAULT_WRITE_PRIORITY
    # Below the exposed range (1..7 are reserved/high levels, not selectable).
    assert _client_write_priority_set(hass, "entry1", "client_5", 7) == DEFAULT_WRITE_PRIORITY
    assert _client_write_priority_set(hass, "entry1", "client_5", 17) == DEFAULT_WRITE_PRIORITY


def test_isolation_per_entry_and_client() -> None:
    hass = _fake_hass()
    _client_write_priority_set(hass, "entry1", "client_5", 8)
    assert _client_write_priority_get(hass, "entry1", "client_6") == DEFAULT_WRITE_PRIORITY
    assert _client_write_priority_get(hass, "entry2", "client_5") == DEFAULT_WRITE_PRIORITY


def test_cleanup_on_unload_shape() -> None:
    # async_unload_entry pops the per-entry dict; simulate and verify defaults return.
    hass = _fake_hass()
    _client_write_priority_set(hass, "entry1", "client_5", 9)
    hass.data[DOMAIN]["client_write_priority"].pop("entry1", None)
    assert _client_write_priority_get(hass, "entry1", "client_5") == DEFAULT_WRITE_PRIORITY
