from __future__ import annotations

from typing import Any

import pytest
from bacpypes3.primitivedata import Null

from custom_components.bacnet_hub.client_runtime import (
    DEFAULT_WRITE_PRIORITY,
    WRITE_PRIORITY_OPTIONS,
    _normalize_priority_array,
    _point_entity_id,
    _point_extra_attributes,
    _point_has_priority_array,
    _point_is_commandable,
    _point_is_writable,
    _point_platform,
    _point_unique_id,
    _read_remote_property,
    _write_client_point_present_value,
)


def test_write_priority_options() -> None:
    assert WRITE_PRIORITY_OPTIONS == [8, 9, 10, 11, 12, 13, 14, 15, 16]
    assert DEFAULT_WRITE_PRIORITY == 8


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        (None, False),
        ([1, 2], True),
        ([], False),
        ("null", False),
        ("none", False),
        ("0", False),
        ("priorityArray", True),
    ],
)
def test_point_has_priority_array(raw: Any, expected: bool) -> None:
    assert _point_has_priority_array({"has_priority_array": raw}) is expected


@pytest.mark.parametrize(
    ("type_slug", "has_pa", "writable", "commandable"),
    [
        ("av", False, True, False),
        ("av", True, True, True),
        ("bv", True, True, True),
        ("mv", True, True, True),
        ("csv", False, True, False),
        ("ao", False, False, False),
        ("ao", True, True, True),
        ("bo", True, True, True),
        ("ai", True, False, False),
        ("bi", False, False, False),
    ],
)
def test_point_writable_and_commandable(
    type_slug: str, has_pa: bool, writable: bool, commandable: bool
) -> None:
    point = {"type_slug": type_slug, "has_priority_array": has_pa}
    assert _point_is_writable(point) is writable
    assert _point_is_commandable(point) is commandable


@pytest.mark.parametrize(
    ("type_slug", "has_pa", "platform"),
    [
        ("ai", False, "sensor"),
        ("bi", False, "binary_sensor"),
        ("csv", False, "text"),
        ("mv", True, "select"),
        ("av", True, "number"),
        ("ao", True, "number"),
        ("ao", False, "sensor"),
        ("bv", True, "switch"),
        ("bo", False, "binary_sensor"),
    ],
)
def test_point_platform(type_slug: str, has_pa: bool, platform: str) -> None:
    point = {"type_slug": type_slug, "has_priority_array": has_pa}
    assert _point_platform(point) == platform


def test_point_ids_are_stable() -> None:
    # Registry-critical: changing these breaks existing installations.
    assert (
        _point_unique_id("entry1", "client_5", "ao", 3)
        == "entry1-client_5-point-ao-3"
    )
    assert (
        _point_entity_id(5, "ao", 3, entity_domain="number")
        == "number.bacnet_doi_5_ao_3"
    )
    assert _point_entity_id(5, "ai", 1) == "sensor.bacnet_doi_5_ai_1"


def test_normalize_priority_array() -> None:
    from bacpypes3.basetypes import PriorityValue

    raw = [PriorityValue(null=()), PriorityValue(real=21.5), PriorityValue(unsigned=3)]
    normalized = _normalize_priority_array(raw)
    assert normalized is not None
    assert len(normalized) == 16
    assert normalized[0] is None
    assert normalized[1] == 21.5
    assert isinstance(normalized[1], float)
    assert normalized[2] == 3
    assert normalized[3:] == [None] * 13

    # Missing priorityArray stays None (drives has_priority_array).
    assert _normalize_priority_array(None) is None
    # Plain scalars pass through; short arrays are padded to 16 slots.
    assert _normalize_priority_array([None, 1.0])[:3] == [None, 1.0, None]
    assert len(_normalize_priority_array([])) == 16


def test_point_extra_attributes() -> None:
    point = {
        "priority_array": [None] * 7 + [21.5] + [None] * 8,
        "relinquish_default": 18.0,
    }
    assert _point_extra_attributes(point) == {
        "priority_array": point["priority_array"],
        "relinquish_default": 18.0,
    }
    # Points without a priorityArray expose no priority attributes at all.
    assert _point_extra_attributes({"relinquish_default": 0}) == {}
    assert _point_extra_attributes({"priority_array": None}) == {}


class FakeApp:
    def __init__(self, fail_first_signatures: int = 0, present_value: Any = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.read_calls: list[tuple[Any, ...]] = []
        self.present_value = present_value
        self._failures_left = fail_first_signatures

    async def write_property(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append((args, kwargs))
        if self._failures_left > 0:
            self._failures_left -= 1
            raise TypeError("unsupported signature")
        return "ok"

    async def read_property(self, *args: Any, array_index: Any = None) -> Any:
        self.read_calls.append(args)
        return self.present_value


async def test_write_present_value_passes_priority_kwarg() -> None:
    app = FakeApp()
    result = await _write_client_point_present_value(
        app, "192.168.1.10", "analogOutput", 3, 21.5, priority=8
    )
    assert result == "ok"
    args, kwargs = app.calls[0]
    assert args == ("192.168.1.10", "analogOutput,3", "presentValue", 21.5)
    assert kwargs == {"priority": 8}


async def test_write_present_value_without_priority() -> None:
    app = FakeApp()
    await _write_client_point_present_value(app, "192.168.1.10", "analogInput", 1, 42)
    args, kwargs = app.calls[0]
    assert args == ("192.168.1.10", "analogInput,1", "presentValue", 42)
    assert kwargs == {}


async def test_write_present_value_falls_back_to_positional_priority() -> None:
    app = FakeApp(fail_first_signatures=1)
    await _write_client_point_present_value(
        app, "192.168.1.10", "binaryOutput", 2, "active", priority=10
    )
    assert len(app.calls) == 2
    args, kwargs = app.calls[1]
    assert args == ("192.168.1.10", "binaryOutput,2", "presentValue", "active", 10)
    assert kwargs == {}


async def test_write_present_value_relinquish_with_null() -> None:
    app = FakeApp()
    await _write_client_point_present_value(
        app, "192.168.1.10", "analogOutput", 3, Null(()), priority=8
    )
    args, kwargs = app.calls[0]
    assert isinstance(args[3], Null)
    assert kwargs == {"priority": 8}


async def test_write_present_value_raises_last_error() -> None:
    app = FakeApp(fail_first_signatures=3)
    with pytest.raises(TypeError):
        await _write_client_point_present_value(
            app, "192.168.1.10", "analogOutput", 3, 1.0, priority=8
        )


async def test_readback_returns_device_value_not_written_value() -> None:
    # Priority 5 is active with "active": writing "inactive" at priority 8
    # does not change presentValue; the read-back must report the device
    # value, not the value that was written.
    app = FakeApp(present_value="active")
    await _write_client_point_present_value(
        app, "192.168.1.10", "binaryOutput", 2, "inactive", priority=8
    )
    value = await _read_remote_property(
        app, "192.168.1.10", "binaryOutput,2", "presentValue"
    )
    assert value == "active"
    assert app.read_calls == [("192.168.1.10", "binaryOutput,2", "presentValue")]


class FakeHass:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}


def test_client_device_info_links_hub_by_registry_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.bacnet_hub import client_runtime as rt

    hass = FakeHass()
    monkeypatch.setattr(rt, "_SUPPORTS_VIA_DEVICE_ID", True)
    rt._hub_device_id_set(hass, "entry-1", "hub-device-id")
    rt._client_cache_set(
        hass,
        "entry-1",
        "client-1",
        {"name": "Controller", "device": {"vendor_name": "ACME", "model_name": "X1"}},
    )

    info = rt._client_device_info(hass, "entry-1", "client-1", 1031010)

    assert info["identifiers"] == {("bacnet_hub", "client-1")}
    assert info["name"] == "Controller"
    assert info["manufacturer"] == "ACME"
    assert info["model"] == "X1"
    # HA 2026.9 rejects the deprecated via_device key for every entity after
    # the first one; only via_device_id (registry id) is allowed.
    assert "via_device" not in info
    assert info["via_device_id"] == "hub-device-id"


def test_client_device_info_resolves_hub_from_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from custom_components.bacnet_hub import client_runtime as rt

    hass = FakeHass()
    monkeypatch.setattr(rt, "_SUPPORTS_VIA_DEVICE_ID", True)
    lookups: list[tuple[Any, ...]] = []

    class FakeRegistry:
        def async_get_device_by_identifier(self, identifier: Any, config_entry_id: str) -> Any:
            lookups.append((identifier, config_entry_id))
            return SimpleNamespace(id="resolved-hub-id")

    monkeypatch.setattr(rt.dr, "async_get", lambda _hass: FakeRegistry())

    info = rt._client_device_info(hass, "entry-2", "client-9", 7)
    assert info["via_device_id"] == "resolved-hub-id"
    assert lookups == [(("bacnet_hub", "entry-2"), "entry-2")]
    assert "via_device" not in info

    # Second call is served from the cache, no further registry lookups.
    rt._client_device_info(hass, "entry-2", "client-9", 7)
    assert len(lookups) == 1


def test_client_device_info_without_hub_device_omits_link(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.bacnet_hub import client_runtime as rt

    hass = FakeHass()
    monkeypatch.setattr(rt, "_SUPPORTS_VIA_DEVICE_ID", True)

    class FakeRegistry:
        def async_get_device_by_identifier(self, identifier: Any, config_entry_id: str) -> Any:
            return None

    monkeypatch.setattr(rt.dr, "async_get", lambda _hass: FakeRegistry())

    info = rt._client_device_info(hass, "entry-3", "client-1", 3)
    assert info["identifiers"] == {("bacnet_hub", "client-1")}
    assert "via_device_id" not in info
    assert "via_device" not in info


def test_client_device_info_legacy_core_uses_via_device(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.bacnet_hub import client_runtime as rt

    hass = FakeHass()
    monkeypatch.setattr(rt, "_SUPPORTS_VIA_DEVICE_ID", False)
    rt._hub_device_id_set(hass, "entry-4", "hub-device-id")

    info = rt._client_device_info(hass, "entry-4", "client-1", 3)
    assert info["via_device"] == ("bacnet_hub", "entry-4")
    assert "via_device_id" not in info
