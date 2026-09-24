"""Tests for the outOfService service, status attributes and generic writes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import voluptuous as vol
from bacpypes3.apdu import ErrorRejectAbortNack
from bacpypes3.basetypes import ErrorType, StatusFlags
from bacpypes3.primitivedata import Boolean
from homeassistant.exceptions import HomeAssistantError

from custom_components.bacnet_hub import (
    ATTR_OUT_OF_SERVICE,
    SERVICE_SET_OUT_OF_SERVICE_SCHEMA,
    _async_set_out_of_service_targets,
)
from custom_components.bacnet_hub.client_point_entities import (
    BacnetClientPointEntityBase,
    BacnetClientPointNumber,
    BacnetClientPointSensor,
    BacnetClientPointSwitch,
)
from custom_components.bacnet_hub.client_runtime import (
    _async_set_point_out_of_service,
    _client_points_get,
    _map_rpm_result,
    _parse_status_flags,
    _point_state_attributes,
    _status_flags_names,
    _to_bool,
    _write_client_point_property,
)
from custom_components.bacnet_hub.const import DOMAIN


class _Rejected(ErrorRejectAbortNack):
    reason = "write-access-denied"

    def __str__(self) -> str:
        return "property: write-access-denied"


class FakeApp:
    def __init__(self, rpm_values: dict[str, Any] | None = None, write_result: Any = "ok") -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.rpm_values = rpm_values or {}
        self.write_result = write_result

    async def write_property(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.write_result

    async def read_property_multiple(self, *args: Any) -> dict[str, Any]:
        return dict(self.rpm_values)


# --- helpers ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), (True, True), (Boolean(1), True), (Boolean(0), False), (0, False),
     ("inactive", False), ("active", True), ("x", None)],
)
def test_to_bool(raw: Any, expected: bool | None) -> None:
    assert _to_bool(raw) is expected


def test_status_flags_names_and_parse() -> None:
    assert _status_flags_names(StatusFlags([0, 1, 0, 1])) == ["fault", "out-of-service"]
    assert _status_flags_names(StatusFlags([0, 0, 0, 0])) == []
    assert _status_flags_names("in-alarm") == ["in-alarm"]
    assert _status_flags_names(["fault"]) == ["fault"]
    assert _status_flags_names(None) is None
    assert _parse_status_flags([]) == {"in_alarm": False, "fault": False, "overridden": False}
    assert _parse_status_flags("in-alarm;overridden") == {
        "in_alarm": True, "fault": False, "overridden": True,
    }
    assert _parse_status_flags(None) is None


def test_point_state_attributes_sensor_and_switch() -> None:
    sensor_point = {
        "type_slug": "ai",
        "out_of_service": Boolean(0),
        "status_flags": ["in-alarm"],
        "reliability": "no-fault-detected",
        "event_state": "offnormal",
    }
    assert _point_state_attributes(sensor_point) == {
        "out_of_service": False,
        "in_alarm": True,
        "fault": False,
        "overridden": False,
        "reliability": "no-fault-detected",
        "event_state": "offnormal",
    }
    switch_point = {
        "type_slug": "bo",
        "has_priority_array": True,
        "out_of_service": True,
        "status_flags": [],
        "priority_array": [None] * 16,
        "relinquish_default": 0,
    }
    attrs = _point_state_attributes(switch_point)
    assert attrs["priority_array"] == [None] * 16
    assert attrs["relinquish_default"] == 0
    assert attrs["out_of_service"] is True
    assert attrs["fault"] is False
    assert _point_state_attributes({}) == {}
    assert "priority_array" not in _point_state_attributes({"type_slug": "ai", "priority_array": [None] * 16})


def test_map_rpm_result_handles_bacpypes3_tuples_and_errors() -> None:
    from bacpypes3.basetypes import PropertyIdentifier
    from bacpypes3.primitivedata import ObjectIdentifier

    oid = ObjectIdentifier("analog-input,1")
    raw = [
        (oid, PropertyIdentifier.presentValue, None, 21.5),
        (oid, PropertyIdentifier.minPresValue, None, ErrorType(errorClass="property", errorCode="unknown-property")),
        (oid, PropertyIdentifier.units, None, "degreesCelsius"),
    ]
    mapped = _map_rpm_result(raw, ["presentValue", "minPresValue", "outOfService"])
    assert mapped == {"presentValue": 21.5, "minPresValue": None}
    assert _map_rpm_result({"presentValue": 1}, ["presentValue", "units"]) == {"presentValue": 1}
    assert _map_rpm_result("garbage", ["presentValue"]) is None


# --- generic write ---------------------------------------------------------------


async def test_write_property_raises_on_device_rejection_without_retry() -> None:
    app = FakeApp(write_result=_Rejected())
    with pytest.raises(HomeAssistantError, match="write-access-denied"):
        await _write_client_point_property(app, "192.168.1.10", "analog-input", 1, "outOfService", Boolean(1), priority=8)
    # Rejected by the device: no further signature attempts.
    assert len(app.calls) == 1


async def test_write_property_raises_on_marker_string() -> None:
    app = FakeApp(write_result="-no property type-")
    with pytest.raises(HomeAssistantError, match="no property type"):
        await _write_client_point_property(app, "192.168.1.10", "analog-input", 1, "outOfService", True)


# --- transaction -------------------------------------------------------------------


async def test_set_point_out_of_service_writes_boolean_without_priority() -> None:
    app = FakeApp(rpm_values={
        "outOfService": Boolean(1),
        "statusFlags": StatusFlags([0, 0, 0, 1]),
        "presentValue": 21.5,
        "reliability": "no-fault-detected",
        "eventState": "normal",
    })
    updates = await _async_set_point_out_of_service(app, "192.168.1.10", "analog-input", 1, True)

    args, kwargs = app.calls[0]
    assert args[:3] == ("192.168.1.10", "analog-input,1", "outOfService")
    assert isinstance(args[3], Boolean) and bool(args[3]) is True
    assert kwargs == {}
    assert updates == {
        "out_of_service": True,
        "status_flags": ["out-of-service"],
        "present_value": 21.5,
        "reliability": "no-fault-detected",
        "event_state": "normal",
    }


# --- entity + service --------------------------------------------------------------


def _seed_hass(app: FakeApp, point: dict[str, Any]) -> SimpleNamespace:
    hass = SimpleNamespace(data={})
    hass.data[DOMAIN] = {
        "client_point_cache": {"entry1": {"client_5": {point["point_key"]: dict(point)}}},
        "servers": {"entry1": SimpleNamespace(app=app)},
    }
    return hass


_AI_POINT = {
    "point_key": "ai_1", "type_slug": "ai", "object_type": "analog-input", "object_instance": 1,
    "client_address": "192.168.1.10", "has_priority_array": False,
    "present_value": 21.5, "out_of_service": False, "status_flags": [],
}


async def test_entity_set_out_of_service_updates_cache_and_keeps_availability(monkeypatch) -> None:
    import custom_components.bacnet_hub.client_point_entities as entities_module

    app = FakeApp(rpm_values={"outOfService": Boolean(1), "statusFlags": StatusFlags([0, 0, 0, 1])})
    hass = _seed_hass(app, _AI_POINT)
    monkeypatch.setattr(entities_module, "async_dispatcher_send", lambda *a, **k: None)
    entity = BacnetClientPointEntityBase(hass, "entry1", "client_5", 5, "ai_1", entity_domain="sensor")

    await entity.async_set_out_of_service(True)

    cached = _client_points_get(hass, "entry1", "client_5")["ai_1"]
    assert cached["out_of_service"] is True
    assert cached["status_flags"] == ["out-of-service"]
    assert entity._attr_available is True


async def test_entity_set_out_of_service_reports_device_error_with_entity_id(monkeypatch) -> None:
    import custom_components.bacnet_hub.client_point_entities as entities_module

    hass = _seed_hass(FakeApp(write_result=_Rejected()), _AI_POINT)
    monkeypatch.setattr(entities_module, "async_dispatcher_send", lambda *a, **k: None)
    entity = BacnetClientPointEntityBase(hass, "entry1", "client_5", 5, "ai_1", entity_domain="sensor")
    with pytest.raises(HomeAssistantError, match="sensor.bacnet_doi_5_ai_1: .*write-access-denied"):
        await entity.async_set_out_of_service(True)


def test_set_out_of_service_schema() -> None:
    assert SERVICE_SET_OUT_OF_SERVICE_SCHEMA({"entity_id": "sensor.a", ATTR_OUT_OF_SERVICE: "true"})[ATTR_OUT_OF_SERVICE] is True
    assert SERVICE_SET_OUT_OF_SERVICE_SCHEMA({"entity_id": "sensor.a", ATTR_OUT_OF_SERVICE: False})[ATTR_OUT_OF_SERVICE] is False
    with pytest.raises(vol.Invalid):
        SERVICE_SET_OUT_OF_SERVICE_SCHEMA({"entity_id": "sensor.a"})
    with pytest.raises(vol.Invalid):
        SERVICE_SET_OUT_OF_SERVICE_SCHEMA({"entity_id": "sensor.a", ATTR_OUT_OF_SERVICE: "abc"})


class FakeOosEntity:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.values: list[bool] = []

    async def async_set_out_of_service(self, enabled: bool) -> None:
        if self.error is not None:
            raise self.error
        self.values.append(enabled)


async def test_set_out_of_service_targets_bundles_errors() -> None:
    good, bad = FakeOosEntity(), FakeOosEntity(HomeAssistantError("device said no"))
    hass = SimpleNamespace(data={DOMAIN: {"client_point_entities": {"sensor.good": good, "sensor.bad": bad}}})
    with pytest.raises(HomeAssistantError, match="device said no"):
        await _async_set_out_of_service_targets(hass, {"sensor.good", "sensor.bad", "sensor.missing"}, True)
    assert good.values == [True]


async def test_handle_points_update_sets_attributes_on_sensor_and_switch(monkeypatch) -> None:
    hass = SimpleNamespace(data={DOMAIN: {"client_point_cache": {"entry1": {"client_5": {
        "ai_1": {**_AI_POINT, "status_flags": ["fault"], "reliability": "unreliable-other"},
        "bo_2": {"point_key": "bo_2", "type_slug": "bo", "object_type": "binary-output", "object_instance": 2,
                 "client_address": "192.168.1.10", "has_priority_array": True, "present_value": 1,
                 "out_of_service": True, "status_flags": [], "priority_array": [None] * 16, "relinquish_default": 0},
    }}}}})
    sensor = BacnetClientPointSensor(hass, "entry1", "client_5", 5, "ai_1")
    switch = BacnetClientPointSwitch(hass, "entry1", "client_5", 5, "bo_2")
    for entity in (sensor, switch):
        monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)
        entity._handle_points_update()

    assert sensor._attr_extra_state_attributes == {
        "out_of_service": False, "in_alarm": False, "fault": True, "overridden": False,
        "reliability": "unreliable-other",
    }
    assert "priority_array" not in sensor._attr_extra_state_attributes
    assert switch._attr_extra_state_attributes["priority_array"] == [None] * 16
    assert switch._attr_extra_state_attributes["out_of_service"] is True
    assert switch._attr_available is True


def test_number_takes_limits_and_step_from_device() -> None:
    hass = SimpleNamespace(data={DOMAIN: {"client_point_cache": {"entry1": {"client_5": {
        "av_3": {"point_key": "av_3", "type_slug": "av", "object_type": "analog-value", "object_instance": 3,
                 "present_value": 150.0, "min_pres_value": -10.0, "max_pres_value": 300.0, "resolution": 0.5},
        "av_4": {"point_key": "av_4", "type_slug": "av", "object_type": "analog-value", "object_instance": 4,
                 "present_value": 1.0},
    }}}}})
    number = BacnetClientPointNumber(hass, "entry1", "client_5", 5, "av_3")
    number._apply_point_state(number._get_point())
    assert (number.native_min_value, number.native_max_value, number.native_step) == (-10.0, 300.0, 0.5)
    plain = BacnetClientPointNumber(hass, "entry1", "client_5", 5, "av_4")
    plain._apply_point_state(plain._get_point())
    assert (plain.native_min_value, plain.native_max_value, plain.native_step) == (0.0, 100.0, 0.1)
