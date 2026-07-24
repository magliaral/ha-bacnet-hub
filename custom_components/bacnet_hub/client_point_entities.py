from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from bacpypes3.primitivedata import Null

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.text import TextEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import StateType

from .const import DOMAIN, client_display_name
from .helpers.tasks import create_logged_task
from .client_runtime import (
    CLIENT_COV_LEASE_SECONDS,
    CLIENT_COV_RENEW_FACTOR,
    WRITE_PRIORITY_OPTIONS,
    _client_cache_get,
    _client_cov_signal,
    _client_points_get,
    _client_points_set,
    _client_points_signal,
    _client_rescan_signal,
    _client_write_priority_get,
    _client_write_priority_set,
    _cov_process_identifier,
    _entry_points_signal,
    _normalize_bacnet_unit,
    _point_entity_id,
    _point_is_commandable,
    _point_is_writable,
    _point_native_value_from_payload,
    _point_unique_id,
    _property_slug,
    _safe_text,
    _sensor_device_class_from_unit,
    _to_int,
)
from .client_runtime import _open_cov_subscription_context, _write_client_point_present_value

_LOGGER = logging.getLogger(__name__)


def _point_is_on(point: dict[str, Any]) -> bool | None:
    value = point.get("present_value")
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"active", "on", "true", "1"}:
        return True
    if text in {"inactive", "off", "false", "0"}:
        return False
    try:
        return bool(int(text))
    except Exception:
        return None


def _client_device_info(
    hass: HomeAssistant, entry_id: str, client_id: str, client_instance: int
) -> DeviceInfo:
    diag_cache = _client_cache_get(hass, entry_id, client_id)
    device_data = dict(diag_cache.get("device", {}) or {})
    return DeviceInfo(
        identifiers={(DOMAIN, client_id)},
        via_device=(DOMAIN, entry_id),
        name=str(diag_cache.get("name") or client_display_name(client_instance)),
        manufacturer=_safe_text(device_data.get("vendor_name")),
        model=_safe_text(device_data.get("model_name")),
        sw_version=_safe_text(device_data.get("firmware_revision")),
        hw_version=_safe_text(device_data.get("hardware_revision")),
        serial_number=_safe_text(device_data.get("serial_number")),
    )


class BacnetClientPointBase:
    """Lean point entity base: identity, naming, availability, write transport.

    Carries no COV machinery. Secondary entities of a point (e.g. the release
    button) extend this directly so the point is never subscribed twice.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_entity_registry_enabled_default = False
    _POINT_UNAVAILABLE_KEY = "_cov_unavailable"
    _POINT_UNAVAILABLE_REASON_KEY = "_cov_unavailable_reason"
    # Appended to the point-derived display name (secondary entities only).
    _name_suffix = ""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
        *,
        entity_domain: str,
    ) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._client_id = client_id
        self._client_instance = int(client_instance)
        self._point_key = str(point_key)
        self._entity_domain = str(entity_domain).strip().lower()

        self._unsub_points_dispatcher: Callable[[], None] | None = None

        cache = _client_points_get(hass, entry_id, client_id).get(self._point_key, {})
        type_slug = str(cache.get("type_slug") or "point")
        object_instance = _to_int(cache.get("object_instance")) or 0

        self._attr_unique_id = _point_unique_id(entry_id, client_id, type_slug, object_instance)
        self.entity_id = _point_entity_id(
            self._client_instance,
            type_slug,
            object_instance,
            entity_domain=self._entity_domain,
        )
        description = _safe_text(cache.get("description"))
        object_name = _safe_text(cache.get("object_name"))
        base_name = str(description or object_name or f"{type_slug.upper()} {object_instance}")
        self._attr_name = f"{base_name}{self._name_suffix}"
        self._attr_available = not bool(cache.get(self._POINT_UNAVAILABLE_KEY, False))

    @property
    def device_info(self) -> DeviceInfo:
        return _client_device_info(
            self.hass, self._entry_id, self._client_id, self._client_instance
        )

    async def async_added_to_hass(self) -> None:
        signal = _client_points_signal(self._entry_id, self._client_id)
        self._unsub_points_dispatcher = async_dispatcher_connect(
            self.hass,
            signal,
            self._handle_points_update,
        )
        self._handle_points_update()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_points_dispatcher is not None:
            self._unsub_points_dispatcher()
            self._unsub_points_dispatcher = None

    def _get_point(self) -> dict[str, Any]:
        return dict(
            _client_points_get(self.hass, self._entry_id, self._client_id).get(self._point_key, {}) or {}
        )

    @callback
    def _handle_points_update(self) -> None:
        point = self._get_point()
        if not point:
            return

        self._attr_available = not bool(point.get(self._POINT_UNAVAILABLE_KEY, False))

        base_name = _safe_text(point.get("description")) or _safe_text(point.get("object_name"))
        if base_name:
            self._attr_name = f"{base_name}{self._name_suffix}"

        self._apply_point_state(point)
        self.async_write_ha_state()

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        raise NotImplementedError

    async def _async_write_point(self, value: Any, *, optimistic: bool) -> None:
        """Write presentValue at the client's configured priority.

        With optimistic=True the local cache is updated to the written value;
        a relinquish (Null) must pass optimistic=False so the cache only
        changes once the device reports the resulting value via COV.
        """
        point = self._get_point()
        if not point:
            raise HomeAssistantError("Point payload unavailable")

        server = self.hass.data.get(DOMAIN, {}).get("servers", {}).get(self._entry_id)
        app = getattr(server, "app", None) if server is not None else None
        if app is None:
            raise HomeAssistantError("BACnet app unavailable")

        address = _safe_text(point.get("client_address"))
        object_type = _safe_text(point.get("object_type"))
        object_instance = _to_int(point.get("object_instance"))
        if not address or not object_type or object_instance is None:
            raise HomeAssistantError("Point addressing incomplete")

        write_priority = (
            _client_write_priority_get(self.hass, self._entry_id, self._client_id)
            if _point_is_commandable(point)
            else None
        )

        await _write_client_point_present_value(
            app,
            address,
            object_type,
            int(object_instance),
            value,
            priority=write_priority,
        )

        if not optimistic:
            return

        point["present_value"] = value
        _client_points_set(
            self.hass,
            self._entry_id,
            self._client_id,
            {self._point_key: point},
        )
        async_dispatcher_send(self.hass, _client_points_signal(self._entry_id, self._client_id))
        async_dispatcher_send(
            self.hass,
            _entry_points_signal(self._entry_id),
            {"client_id": self._client_id},
        )

    async def _async_write_present_value(self, value: Any) -> None:
        await self._async_write_point(value, optimistic=True)


class BacnetClientPointEntityBase(BacnetClientPointBase):
    """Primary point entity base: adds the COV subscription runtime."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
        *,
        entity_domain: str,
    ) -> None:
        super().__init__(
            hass,
            entry_id,
            client_id,
            client_instance,
            point_key,
            entity_domain=entity_domain,
        )
        self._unsub_cov_dispatcher: Callable[[], None] | None = None
        self._cov_context: Any | None = None
        self._cov_task: asyncio.Task | None = None
        self._cov_reregister_task: asyncio.Task | None = None
        self._cov_lease_unsub: Callable[[], None] | None = None
        self._cov_lock = asyncio.Lock()
        self._cov_registered = False
        self._cov_last_target: tuple[str, str] | None = None
        self._cov_retry_delay_seconds: float = 10.0
        self._cov_retry_not_before_ts: float = 0.0
        self._cov_rescan_not_before_ts: float = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        cov_signal = _client_cov_signal(self._entry_id, self._client_id)
        self._unsub_cov_dispatcher = async_dispatcher_connect(
            self.hass,
            cov_signal,
            self._handle_cov_reregister,
        )
        await self._async_register_cov()
        self._handle_points_update()

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        if self._unsub_cov_dispatcher is not None:
            self._unsub_cov_dispatcher()
            self._unsub_cov_dispatcher = None
        # A pending re-register must not resurrect the COV subscription after
        # the entity is gone.
        if self._cov_reregister_task is not None and not self._cov_reregister_task.done():
            self._cov_reregister_task.cancel()
        self._cov_reregister_task = None
        async with self._cov_lock:
            await self._async_stop_cov_runtime()

    def _set_client_points_unavailable(self, unavailable: bool, *, reason: str | None = None) -> None:
        point_cache = _client_points_get(self.hass, self._entry_id, self._client_id)
        if not point_cache:
            return

        payload: dict[str, dict[str, Any]] = {}
        changed = False
        for point_key, raw_point in point_cache.items():
            point = dict(raw_point or {})
            prev_unavailable = bool(point.get(self._POINT_UNAVAILABLE_KEY, False))
            if prev_unavailable != unavailable:
                point[self._POINT_UNAVAILABLE_KEY] = unavailable
                changed = True

            if unavailable:
                reason_text = str(reason or "cov_register_failed")
                if str(point.get(self._POINT_UNAVAILABLE_REASON_KEY) or "") != reason_text:
                    point[self._POINT_UNAVAILABLE_REASON_KEY] = reason_text
                    changed = True
            else:
                if self._POINT_UNAVAILABLE_REASON_KEY in point:
                    point.pop(self._POINT_UNAVAILABLE_REASON_KEY, None)
                    changed = True

            payload[str(point_key)] = point

        if not changed:
            return

        _client_points_set(self.hass, self._entry_id, self._client_id, payload)
        async_dispatcher_send(self.hass, _client_points_signal(self._entry_id, self._client_id))
        async_dispatcher_send(
            self.hass,
            _entry_points_signal(self._entry_id),
            {"client_id": self._client_id},
        )

    @callback
    def _handle_cov_reregister(self) -> None:
        self._start_cov_reregister_task()

    def _start_cov_reregister_task(self) -> None:
        if self._cov_reregister_task is not None and not self._cov_reregister_task.done():
            return
        self._cov_reregister_task = create_logged_task(
            self.hass,
            self._async_reregister_cov(),
            logger=_LOGGER,
            message=f"COV re-register for {self._point_key}",
        )

    async def _async_reregister_cov(self) -> None:
        try:
            await self._async_register_cov()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("COV re-register failed for %s", self._point_key, exc_info=True)

    async def _async_stop_cov_runtime(self) -> None:
        if self._cov_lease_unsub is not None:
            try:
                self._cov_lease_unsub()
            except Exception:
                pass
            self._cov_lease_unsub = None
        if self._cov_task is not None and not self._cov_task.done():
            self._cov_task.cancel()
            try:
                await self._cov_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._cov_task = None
        if self._cov_context is not None:
            await self._async_cleanup_cov_context(self._cov_context, call_aexit=True)
        self._cov_context = None
        self._cov_registered = False

    async def _async_cleanup_cov_context(self, context_obj: Any, *, call_aexit: bool) -> None:
        if context_obj is None:
            return

        if call_aexit:
            try:
                await context_obj.__aexit__(None, None, None)
            except Exception:
                pass

        # bacpypes3 is pinned in manifest.json. Its SubscriptionContextManager
        # (bacpypes3.service.cov) keeps exactly these two refresh artifacts,
        # which can outlive a failed or abandoned context.
        handle = getattr(context_obj, "refresh_subscription_handle", None)
        if handle is not None:
            try:
                handle.cancel()
            except Exception:
                pass

        task = getattr(context_obj, "refresh_subscription_task", None)
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def _async_register_cov(self) -> None:
        point = self._get_point()
        object_identifier = _safe_text(point.get("object_identifier"))
        address = _safe_text(point.get("client_address"))
        if not object_identifier or not address:
            self._set_client_points_unavailable(True, reason="cov_target_missing")
            return
        now = time.monotonic()
        target = (str(address), str(object_identifier))
        if self._cov_last_target != target:
            self._cov_last_target = target
            self._cov_retry_not_before_ts = 0.0
            self._cov_retry_delay_seconds = 10.0
        if now < self._cov_retry_not_before_ts:
            return

        server = self.hass.data.get(DOMAIN, {}).get("servers", {}).get(self._entry_id)
        app = getattr(server, "app", None) if server is not None else None
        if app is None:
            self._set_client_points_unavailable(True, reason="bacnet_app_unavailable")
            return

        cov_factory = getattr(app, "change_of_value", None)
        if not callable(cov_factory):
            self._cov_registered = False
            self._set_client_points_unavailable(True, reason="cov_not_supported")
            return

        process_id = _cov_process_identifier(self._entry_id, self._client_id, self._point_key)
        async with self._cov_lock:
            await self._async_stop_cov_runtime()

            async def _cleanup_failed_context(context_obj: Any) -> None:
                await self._async_cleanup_cov_context(context_obj, call_aexit=False)

            opened_context, last_err = await _open_cov_subscription_context(
                app,
                address=address,
                object_identifier=object_identifier,
                process_id=process_id,
                lifetime=CLIENT_COV_LEASE_SECONDS,
                cleanup_context=_cleanup_failed_context,
                max_offset_attempts=3,
            )
            self._cov_context = opened_context
            self._cov_registered = False
            if last_err is not None:
                exc_info = (type(last_err), last_err, last_err.__traceback__)
                _LOGGER.debug(
                    "COV subscribe failed for %s (%s)",
                    object_identifier,
                    address,
                    exc_info=exc_info,
                )
                now_fail = time.monotonic()
                if now_fail >= self._cov_rescan_not_before_ts:
                    self._cov_rescan_not_before_ts = now_fail + 10.0
                    async_dispatcher_send(
                        self.hass,
                        _client_rescan_signal(self._entry_id),
                        {"instance": self._client_instance},
                    )
                self._cov_retry_not_before_ts = time.monotonic() + self._cov_retry_delay_seconds
                self._cov_retry_delay_seconds = min(self._cov_retry_delay_seconds * 2.0, 300.0)
                self._set_client_points_unavailable(True, reason="cov_register_failed")
                return

            if self._cov_context is None:
                self._set_client_points_unavailable(True, reason="cov_register_failed")
                return

            self._cov_registered = True
            self._cov_retry_not_before_ts = 0.0
            self._cov_retry_delay_seconds = 10.0
            self._set_client_points_unavailable(False)
            self._cov_task = self.hass.async_create_task(self._async_cov_receive_loop())
            self._schedule_cov_lease_reregister()

    def _schedule_cov_lease_reregister(self) -> None:
        if self._cov_lease_unsub is not None:
            try:
                self._cov_lease_unsub()
            except Exception:
                pass
            self._cov_lease_unsub = None

        # Renew before the lease expires (at CLIENT_COV_RENEW_FACTOR of the
        # lifetime): the old subscription is still valid while the new one is
        # opened, so entities never blip to unavailable on renewal.
        delay = max(30.0, float(CLIENT_COV_LEASE_SECONDS) * float(CLIENT_COV_RENEW_FACTOR))

        @callback
        def _lease_expired(_now) -> None:
            self._cov_lease_unsub = None
            self._start_cov_reregister_task()

        self._cov_lease_unsub = async_call_later(self.hass, delay, _lease_expired)

    async def _async_cov_receive_loop(self) -> None:
        while True:
            context = self._cov_context
            if context is None:
                return
            try:
                prop, value = await context.get_value()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._cov_registered = False
                self._set_client_points_unavailable(True, reason="cov_receive_failed")
                self._handle_points_update()
                _LOGGER.debug("COV receive loop failed for %s", self._point_key, exc_info=True)
                return

            key = _property_slug(prop)
            if not key:
                continue
            if key not in {
                "presentvalue",
                "statusflags",
                "outofservice",
                "reliability",
                "description",
                "objectname",
                "statetext",
                "activetext",
                "inactivetext",
            }:
                continue

            point = self._get_point()
            if not point:
                continue

            if key == "presentvalue":
                point["present_value"] = value
            elif key == "statusflags":
                point["status_flags"] = _safe_text(value)
            elif key == "outofservice":
                point["out_of_service"] = value
            elif key == "reliability":
                point["reliability"] = _safe_text(value)
            elif key == "description":
                point["description"] = _safe_text(value)
            elif key == "objectname":
                point["object_name"] = _safe_text(value)
            elif key == "statetext":
                if isinstance(value, (list, tuple)):
                    point["state_text"] = [str(item) for item in value]
                else:
                    try:
                        point["state_text"] = [str(item) for item in list(value)]
                    except Exception:
                        pass
            elif key == "activetext":
                point["active_text"] = _safe_text(value)
            elif key == "inactivetext":
                point["inactive_text"] = _safe_text(value)

            _client_points_set(
                self.hass,
                self._entry_id,
                self._client_id,
                {self._point_key: point},
            )
            async_dispatcher_send(self.hass, _client_points_signal(self._entry_id, self._client_id))
            async_dispatcher_send(
                self.hass,
                _entry_points_signal(self._entry_id),
                {"client_id": self._client_id},
            )


class BacnetClientPointSensor(BacnetClientPointEntityBase, SensorEntity):
    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        super().__init__(
            hass,
            entry_id,
            client_id,
            client_instance,
            point_key,
            entity_domain="sensor",
        )
        self._attr_native_value: StateType = None
        self._attr_native_unit_of_measurement: str | None = None
        self._attr_device_class: SensorDeviceClass | None = None
        self._attr_state_class: SensorStateClass | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        self._attr_native_unit_of_measurement = _normalize_bacnet_unit(point.get("unit"))
        self._attr_device_class = _sensor_device_class_from_unit(self._attr_native_unit_of_measurement)
        native_value = _point_native_value_from_payload(point)
        self._attr_state_class = None
        if str(point.get("type_slug") or "") in {"ai", "ao", "av"} and isinstance(native_value, (int, float)):
            self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_value = native_value
        self._attr_extra_state_attributes = {}


class BacnetClientPointBinarySensor(BacnetClientPointEntityBase, BinarySensorEntity):
    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        super().__init__(
            hass,
            entry_id,
            client_id,
            client_instance,
            point_key,
            entity_domain="binary_sensor",
        )
        self._attr_is_on: bool | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        self._attr_is_on = _point_is_on(point)
        self._attr_extra_state_attributes = {}


class BacnetClientPointNumber(BacnetClientPointEntityBase, NumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_native_step = 0.1

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        super().__init__(
            hass,
            entry_id,
            client_id,
            client_instance,
            point_key,
            entity_domain="number",
        )
        self._attr_native_value: float | None = None
        self._attr_native_unit_of_measurement: str | None = None
        self._attr_device_class: SensorDeviceClass | None = None

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        self._attr_native_unit_of_measurement = _normalize_bacnet_unit(point.get("unit"))
        self._attr_device_class = _sensor_device_class_from_unit(self._attr_native_unit_of_measurement)
        value = point.get("present_value")
        try:
            self._attr_native_value = round(float(value), 1) if value is not None else None
        except Exception:
            self._attr_native_value = None

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write_present_value(round(float(value), 1))


class BacnetClientPointSwitch(BacnetClientPointEntityBase, SwitchEntity):
    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        super().__init__(
            hass,
            entry_id,
            client_id,
            client_instance,
            point_key,
            entity_domain="switch",
        )
        self._attr_is_on: bool = False

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        value = _point_is_on(point)
        self._attr_is_on = bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write_present_value(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write_present_value(0)


class BacnetClientPointSelect(BacnetClientPointEntityBase, SelectEntity):
    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        super().__init__(
            hass,
            entry_id,
            client_id,
            client_instance,
            point_key,
            entity_domain="select",
        )
        self._attr_options: list[str] = []
        self._attr_current_option: str | None = None

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        texts = point.get("state_text")
        options: list[str] = []
        if isinstance(texts, (list, tuple)):
            options = [str(item).strip() for item in texts if str(item).strip()]
        if not options:
            count = _to_int(point.get("number_of_states")) or 0
            if count > 0:
                options = [str(idx) for idx in range(1, min(count, 128) + 1)]
        self._attr_options = options

        idx = _to_int(point.get("present_value"))
        self._attr_current_option = None
        if idx is not None and options:
            pos = int(idx) - 1
            if 0 <= pos < len(options):
                self._attr_current_option = options[pos]

    async def async_select_option(self, option: str) -> None:
        point = self._get_point()
        texts = point.get("state_text")
        options = list(self.options or [])
        value_index: int | None = None
        if option in options:
            value_index = options.index(option) + 1
        elif isinstance(texts, (list, tuple)):
            normalized = [str(item).strip() for item in texts]
            if option in normalized:
                value_index = normalized.index(option) + 1
        if value_index is None:
            maybe_int = _to_int(option)
            if maybe_int is None:
                raise HomeAssistantError(f"Unsupported option: {option}")
            value_index = int(maybe_int)
        await self._async_write_present_value(int(value_index))


class BacnetClientPointText(BacnetClientPointEntityBase, TextEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        super().__init__(
            hass,
            entry_id,
            client_id,
            client_instance,
            point_key,
            entity_domain="text",
        )
        self._attr_native_value: str | None = None

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        value = _point_native_value_from_payload(point)
        self._attr_native_value = None if value is None else str(value)

    async def async_set_value(self, value: str) -> None:
        point = self._get_point()
        if not _point_is_writable(point):
            raise HomeAssistantError("Point is read-only")
        await self._async_write_present_value(str(value))


class BacnetClientWritePrioritySelect(SelectEntity, RestoreEntity):
    """Per-client BACnet write priority (8..16) applied to commandable writes."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:priority-high"
    _attr_name = "Write priority"
    _attr_options = [str(priority) for priority in WRITE_PRIORITY_OPTIONS]

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
    ) -> None:
        self.hass = hass
        self._entry_id = str(entry_id)
        self._client_id = str(client_id)
        self._client_instance = int(client_instance)

        self._attr_unique_id = f"{self._entry_id}-{self._client_id}-write-priority"
        self.entity_id = f"select.bacnet_doi_{self._client_instance}_write_priority"
        self._attr_current_option = str(
            _client_write_priority_get(hass, self._entry_id, self._client_id)
        )

    @property
    def device_info(self) -> DeviceInfo:
        return _client_device_info(
            self.hass, self._entry_id, self._client_id, self._client_instance
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        value = (
            last_state.state
            if last_state is not None and last_state.state in self._attr_options
            else self._attr_current_option
        )
        self._attr_current_option = str(
            _client_write_priority_set(self.hass, self._entry_id, self._client_id, value)
        )

    async def async_select_option(self, option: str) -> None:
        validated = _client_write_priority_set(
            self.hass, self._entry_id, self._client_id, option
        )
        self._attr_current_option = str(validated)
        self.async_write_ha_state()
        _LOGGER.debug(
            "Write priority for client %s set to %s", self._client_id, validated
        )


class BacnetClientPointReleaseButton(BacnetClientPointBase, ButtonEntity):
    """Relinquish the commanded value: write Null at the configured priority."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:lock-open-variant-outline"
    _name_suffix = " Release"

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client_id: str,
        client_instance: int,
        point_key: str,
    ) -> None:
        super().__init__(
            hass,
            entry_id,
            client_id,
            client_instance,
            point_key,
            entity_domain="button",
        )
        # The primary entity of the point owns the bare unique_id/entity_id.
        self._attr_unique_id = f"{self._attr_unique_id}-release"
        self.entity_id = f"{self.entity_id}_release"

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        pass

    async def async_press(self) -> None:
        point = self._get_point()
        if not _point_is_commandable(point):
            raise HomeAssistantError("Point has no priority array to relinquish")
        # Null relinquishes the configured priority slot; the point's COV
        # subscription picks up the resulting value from the device, so the
        # cache must not be updated optimistically.
        await self._async_write_point(Null(()), optimistic=False)
        _LOGGER.debug("Relinquished %s at the configured priority", self._point_key)


