from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import voluptuous as vol
from bacpypes3.basetypes import PriorityValue
from bacpypes3.primitivedata import Null
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bacnet_hub import (
    ATTR_PRIORITY,
    DEFAULT_RELEASE_PRIORITY,
    SERVICE_RELEASE_SCHEMA,
    _async_release_targets,
)
from custom_components.bacnet_hub.client_point_entities import (
    BacnetClientPointEntityBase,
)
from custom_components.bacnet_hub.client_runtime import _async_release_point
from custom_components.bacnet_hub.const import DOMAIN, KEY_CLIENT_POINT_ENTITIES


def _fake_hass() -> SimpleNamespace:
    return SimpleNamespace(data={})


def test_release_schema_defaults_priority() -> None:
    data = SERVICE_RELEASE_SCHEMA({"entity_id": "number.bacnet_doi_1234_ao_1"})
    assert data[ATTR_PRIORITY] == DEFAULT_RELEASE_PRIORITY == 8


@pytest.mark.parametrize("priority", [0, 17, "abc"])
def test_release_schema_rejects_invalid_priority(priority: Any) -> None:
    with pytest.raises(vol.Invalid):
        SERVICE_RELEASE_SCHEMA(
            {"entity_id": "number.bacnet_doi_1234_ao_1", ATTR_PRIORITY: priority}
        )


class FakeApp:
    """Records write_property calls and answers RPM reads with canned values."""

    def __init__(self, rpm_values: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.rpm_values = rpm_values or {}

    async def write_property(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append((args, kwargs))
        return "ok"

    async def read_property_multiple(self, *args: Any) -> dict[str, Any]:
        return dict(self.rpm_values)


async def test_async_release_point_writes_null_at_priority() -> None:
    app = FakeApp(
        rpm_values={
            "presentValue": 18.0,
            "priorityArray": [PriorityValue(null=())] * 16,
            "relinquishDefault": 18.0,
        }
    )
    updates = await _async_release_point(app, "192.168.1.10", "analogOutput", 3, 8)

    args, kwargs = app.calls[0]
    assert args[:3] == ("192.168.1.10", "analogOutput,3", "presentValue")
    assert isinstance(args[3], Null)
    assert kwargs == {"priority": 8}

    assert updates["present_value"] == 18.0
    assert updates["priority_array"] == [None] * 16
    assert updates["relinquish_default"] == 18.0


async def test_release_entity_without_priority_array_raises_validation_error() -> None:
    hass = _fake_hass()
    # Seed the point cache before the entity resolves its ids from it.
    hass.data[DOMAIN] = {
        "client_point_cache": {
            "entry1": {
                "client_5": {
                    "av_2": {
                        "type_slug": "av",
                        "object_instance": 2,
                        "has_priority_array": False,
                    }
                }
            }
        }
    }
    entity = BacnetClientPointEntityBase(
        hass, "entry1", "client_5", 5, "av_2", entity_domain="number"
    )
    with pytest.raises(ServiceValidationError):
        await entity.async_release(8)


class FakeReleaseEntity:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.released_at: list[int] = []

    async def async_release(self, priority: int) -> None:
        if self.error is not None:
            raise self.error
        self.released_at.append(priority)


async def test_release_targets_bundles_errors_and_continues() -> None:
    hass = _fake_hass()
    first = FakeReleaseEntity()
    failing = FakeReleaseEntity(error=ServiceValidationError("number.b: no priority array"))
    last = FakeReleaseEntity()
    hass.data[DOMAIN] = {
        KEY_CLIENT_POINT_ENTITIES: {
            "number.a": first,
            "number.b": failing,
            "number.c": last,
        }
    }

    with pytest.raises(ServiceValidationError) as excinfo:
        await _async_release_targets(
            hass, {"number.a", "number.b", "number.c", "number.missing"}, 8
        )

    # Both healthy entities were still released despite the failure in between.
    assert first.released_at == [8]
    assert last.released_at == [8]
    message = str(excinfo.value)
    assert "number.b: no priority array" in message
    assert "number.missing" in message


async def test_release_targets_mixed_errors_raise_home_assistant_error() -> None:
    hass = _fake_hass()
    hass.data[DOMAIN] = {
        KEY_CLIENT_POINT_ENTITIES: {
            "number.a": FakeReleaseEntity(error=HomeAssistantError("BACnet app unavailable")),
        }
    }
    with pytest.raises(HomeAssistantError) as excinfo:
        await _async_release_targets(hass, {"number.a"}, 8)
    assert not isinstance(excinfo.value, ServiceValidationError)


async def test_release_targets_without_targets_raises() -> None:
    with pytest.raises(ServiceValidationError):
        await _async_release_targets(_fake_hass(), set(), 8)


async def test_release_targets_default_priority_flows_through() -> None:
    hass = _fake_hass()
    entity = FakeReleaseEntity()
    hass.data[DOMAIN] = {KEY_CLIENT_POINT_ENTITIES: {"switch.a": entity}}

    data = SERVICE_RELEASE_SCHEMA({"entity_id": "switch.a"})
    await _async_release_targets(hass, {"switch.a"}, int(data[ATTR_PRIORITY]))
    assert entity.released_at == [8]
