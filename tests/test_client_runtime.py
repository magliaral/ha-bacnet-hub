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
