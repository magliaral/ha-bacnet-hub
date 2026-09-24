"""Tests for the COV lease renewal and re-subscribe fallbacks."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import custom_components.bacnet_hub.client_point_entities as entities_module
from custom_components.bacnet_hub.client_point_entities import BacnetClientPointEntityBase
from custom_components.bacnet_hub.const import DOMAIN

_POINT = {
    "point_key": "ai_1", "type_slug": "ai", "object_type": "analog-input", "object_instance": 1,
    "client_address": "192.168.1.10", "object_identifier": "analog-input,1",
}


def _entity() -> BacnetClientPointEntityBase:
    hass = SimpleNamespace(data={DOMAIN: {"client_point_cache": {"entry1": {"client_5": {"ai_1": dict(_POINT)}}}}})
    return BacnetClientPointEntityBase(hass, "entry1", "client_5", 5, "ai_1", entity_domain="sensor")


def _capture_tasks(monkeypatch) -> list[str]:
    started: list[str] = []

    def _fake_task(hass: Any, coro: Any, **kwargs: Any) -> SimpleNamespace:
        started.append(kwargs["message"])
        coro.close()
        return SimpleNamespace(done=lambda: True)

    monkeypatch.setattr(entities_module, "create_logged_task", _fake_task)
    return started


async def test_failed_renewal_clears_registration_and_resubscribes(monkeypatch) -> None:
    entity = _entity()
    entity._cov_registered = True
    entity._cov_context = object()

    async def _refresh_fails(context: Any) -> None:
        raise TimeoutError()

    monkeypatch.setattr(entities_module, "_async_refresh_cov_subscription", _refresh_fails)
    started = _capture_tasks(monkeypatch)

    await entity._async_refresh_cov_lease()

    assert entity._cov_registered is False
    assert started == ["COV re-register for ai_1"]


async def test_successful_renewal_reschedules_and_stamps_time(monkeypatch) -> None:
    entity = _entity()
    entity._cov_registered = True
    entity._cov_context = object()
    scheduled: list[float] = []

    async def _refresh_ok(context: Any) -> None:
        return None

    monkeypatch.setattr(entities_module, "_async_refresh_cov_subscription", _refresh_ok)
    monkeypatch.setattr(entities_module, "async_call_later", lambda hass, delay, cb: scheduled.append(delay) or (lambda: None))
    before = time.monotonic()

    await entity._async_refresh_cov_lease()

    assert entity._cov_registered is True
    assert entity._cov_last_renewed_ts >= before
    assert scheduled == [480.0]


def test_iam_signal_renews_in_place_when_registered_and_throttled(monkeypatch) -> None:
    entity = _entity()
    entity._cov_registered = True
    entity._cov_context = object()
    started = _capture_tasks(monkeypatch)

    # Renewed just now: the signal is ignored.
    entity._cov_last_renewed_ts = time.monotonic()
    entity._handle_cov_reregister()
    assert started == []

    # Older than the throttle: renew in place, not a full re-subscribe.
    entity._cov_last_renewed_ts = time.monotonic() - 60.0
    entity._handle_cov_reregister()
    assert started == ["COV lease refresh for ai_1"]


def test_iam_signal_resubscribes_when_not_registered(monkeypatch) -> None:
    entity = _entity()
    started = _capture_tasks(monkeypatch)

    entity._handle_cov_reregister()

    assert started == ["COV re-register for ai_1"]


def test_failed_registration_schedules_its_own_retry(monkeypatch) -> None:
    entity = _entity()
    scheduled: list[float] = []
    monkeypatch.setattr(entities_module, "async_call_later", lambda hass, delay, cb: scheduled.append(delay) or (lambda: None))

    entity._schedule_cov_retry(10.5)

    assert scheduled == [10.5]
    assert entity._cov_lease_unsub is not None
