from __future__ import annotations

from typing import Any

import pytest
from bacpypes3.primitivedata import Null

from custom_components.bacnet_hub.client_runtime import (
    DEFAULT_WRITE_PRIORITY,
    WRITE_PRIORITY_OPTIONS,
    _point_entity_id,
    _point_has_priority_array,
    _point_is_commandable,
    _point_is_writable,
    _point_platform,
    _point_unique_id,
    _write_client_point_present_value,
)


def test_write_priority_options() -> None:
    assert WRITE_PRIORITY_OPTIONS == [8, 9, 10, 11, 12, 13, 14, 15, 16]
    assert DEFAULT_WRITE_PRIORITY == 16


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


class FakeApp:
    def __init__(self, fail_first_signatures: int = 0) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._failures_left = fail_first_signatures

    async def write_property(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append((args, kwargs))
        if self._failures_left > 0:
            self._failures_left -= 1
            raise TypeError("unsupported signature")
        return "ok"


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
