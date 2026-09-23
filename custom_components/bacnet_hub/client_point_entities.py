from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.text import TextEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.typing import StateType

from .const import DOMAIN, KEY_CLIENT_POINT_ENTITIES
from .helpers.tasks import create_logged_task
from .client_runtime import (
    CLIENT_COV_LEASE_SECONDS,
    CLIENT_COV_RENEW_FACTOR,
    CLIENT_PRIORITY_POLL_INTERVAL,
    CLIENT_WRITE_READBACK_DELAY_SECONDS,
    _client_cov_signal,
    _client_cov_subscribe_semaphore,
    _client_device_info,
    _client_points_get,
    _client_points_set,
    _client_points_signal,
    _client_rescan_signal,
    _cov_process_identifier,
    _entry_points_signal,
    _entry_write_priority,
    _normalize_bacnet_unit,
    _normalize_priority_array,
    _normalize_priority_slot,
    _point_entity_id,
    _point_extra_attributes,
    _point_has_priority_array,
    _point_is_commandable,
    _point_is_writable,
    _point_native_value_from_payload,
    _point_unique_id,
    _property_slug,
    _safe_text,
    _sensor_device_class_from_unit,
    _to_int,
)
from .client_runtime import (
    CLIENT_COV_PROPERTY_SUBSCRIPTIONS_ALL,
    CLIENT_COV_PROPERTY_SUBSCRIPTIONS_COMMANDABLE,
    _async_refresh_cov_subscription,
    _async_release_point,
    _hub_cov_registry,
    _open_cov_property_subscription,
    _open_cov_subscription_context,
    _read_remote_properties,
    _read_remote_property,
    _write_client_point_present_value,
)

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


class BacnetClientPointBase:
    """Lean point entity base: identity, naming, availability, write transport.

    Carries no COV machinery; the COV runtime lives in
    BacnetClientPointEntityBase for the primary point entities.
    """

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_entity_registry_enabled_default = False
    _POINT_UNAVAILABLE_KEY = "_cov_unavailable"
    _POINT_UNAVAILABLE_REASON_KEY = "_cov_unavailable_reason"

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
        self._readback_task: asyncio.Task | None = None

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
        self._attr_name = base_name
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
        if self._readback_task is not None and not self._readback_task.done():
            self._readback_task.cancel()
        self._readback_task = None

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
            self._attr_name = base_name

        self._apply_point_state(point)

        # Priority attributes only for writable points exposing a priorityArray.
        if _point_is_writable(point):
            attrs = _point_extra_attributes(point)
            if attrs:
                self._attr_extra_state_attributes = attrs

        self.async_write_ha_state()

    def _apply_point_state(self, point: dict[str, Any]) -> None:
        raise NotImplementedError

    async def _async_write_point(self, value: Any, *, optimistic: bool) -> None:
        """Write presentValue at the client's configured priority.

        With optimistic=True the local cache is updated to the written value
        for a snappy UI; a relinquish (Null) must pass optimistic=False. In
        both cases a delayed read-back verifies the cache against the device:
        a write below the highest active priority slot does not change
        presentValue, and COV stays silent when nothing changed.
        """
        point = self._get_point()
        if not point:
            raise HomeAssistantError("Point payload unavailable")

        app, address, object_type, object_instance = self._resolve_write_target(point)

        write_priority = (
            _entry_write_priority(self.hass, self._entry_id)
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

        if optimistic:
            self._update_present_value_cache(value)

        if self._readback_task is not None and not self._readback_task.done():
            self._readback_task.cancel()
        self._readback_task = create_logged_task(
            self.hass,
            self._async_readback_present_value(
                app, address, object_type, int(object_instance)
            ),
            logger=_LOGGER,
            message=f"presentValue read-back for {self._point_key}",
        )

    def _resolve_write_target(self, point: dict[str, Any]) -> tuple[Any, str, str, int]:
        """Resolve (app, address, object_type, object_instance) for a write."""
        server = self.hass.data.get(DOMAIN, {}).get("servers", {}).get(self._entry_id)
        app = getattr(server, "app", None) if server is not None else None
        if app is None:
            raise HomeAssistantError("BACnet app unavailable")

        address = _safe_text(point.get("client_address"))
        object_type = _safe_text(point.get("object_type"))
        object_instance = _to_int(point.get("object_instance"))
        if not address or not object_type or object_instance is None:
            raise HomeAssistantError("Point addressing incomplete")
        return app, address, object_type, int(object_instance)

    async def _async_readback_present_value(
        self, app: Any, address: str, object_type: str, object_instance: int
    ) -> None:
        await asyncio.sleep(CLIENT_WRITE_READBACK_DELAY_SECONDS)
        objid = f"{object_type},{int(object_instance)}"
        try:
            if _point_has_priority_array(self._get_point()):
                # Commandable points: also refresh priorityArray and
                # relinquishDefault so the attributes stay in sync.
                values = await _read_remote_properties(
                    app,
                    address,
                    objid,
                    ["presentValue", "priorityArray", "relinquishDefault"],
                )
                # Failed reads come back as None; never clobber the cache with them.
                updates: dict[str, Any] = {}
                if values.get("presentValue") is not None:
                    updates["present_value"] = values["presentValue"]
                priority_array = _normalize_priority_array(values.get("priorityArray"))
                if priority_array is not None:
                    updates["priority_array"] = priority_array
                relinquish_default = _normalize_priority_slot(
                    values.get("relinquishDefault")
                )
                if relinquish_default is not None:
                    updates["relinquish_default"] = relinquish_default
                if not updates:
                    return
            else:
                value = await _read_remote_property(app, address, objid, "presentValue")
                updates = {"present_value": value}
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug(
                "presentValue read-back failed for %s", self._point_key, exc_info=True
            )
            return
        self._update_point_cache(updates)

    def _update_present_value_cache(self, value: Any) -> None:
        self._update_point_cache({"present_value": value})

    def _update_point_cache(self, updates: dict[str, Any]) -> None:
        point = self._get_point()
        if not point:
            return
        point.update(updates)
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

    # Keep the 16-slot priority array out of the recorder database; the
    # attribute would otherwise be written on every state change.
    _unrecorded_attributes = frozenset({"priority_array"})

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
        self._cov_refresh_task: asyncio.Task | None = None
        # Properties covered by an accepted SubscribeCOVProperty; the others
        # keep their polling fallback.
        self._cov_property_active: set[str] = set()
        self._cov_lease_unsub: Callable[[], None] | None = None
        self._cov_lock = asyncio.Lock()
        self._cov_registered = False
        self._cov_last_target: tuple[str, str] | None = None
        self._cov_retry_delay_seconds: float = 10.0
        self._cov_retry_not_before_ts: float = 0.0
        self._cov_rescan_not_before_ts: float = 0.0
        self._priority_poll_unsub: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Register for the bacnet_hub.release service, which resolves its
        # entity targets across platforms via this map.
        self.hass.data.setdefault(DOMAIN, {}).setdefault(
            KEY_CLIENT_POINT_ENTITIES, {}
        )[self.entity_id] = self
        cov_signal = _client_cov_signal(self._entry_id, self._client_id)
        self._unsub_cov_dispatcher = async_dispatcher_connect(
            self.hass,
            cov_signal,
            self._handle_cov_reregister,
        )
        # Object-wide COV carries no priorityArray/relinquishDefault. They
        # are subscribed per property; the poll stays as fallback for devices
        # that decline SubscribeCOVProperty (see _handle_priority_poll).
        if _point_has_priority_array(self._get_point()):
            self._priority_poll_unsub = async_track_time_interval(
                self.hass,
                self._handle_priority_poll,
                CLIENT_PRIORITY_POLL_INTERVAL,
            )
        # Subscribe in the background: a network round trip per entity must
        # not delay platform setup (and thereby the HA bootstrap).
        self._start_cov_reregister_task()
        self._handle_points_update()

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        entities = self.hass.data.get(DOMAIN, {}).get(KEY_CLIENT_POINT_ENTITIES, {})
        # Only drop our own registration; a reload may already have replaced it.
        if entities.get(self.entity_id) is self:
            entities.pop(self.entity_id, None)
        if self._priority_poll_unsub is not None:
            self._priority_poll_unsub()
            self._priority_poll_unsub = None
        if self._unsub_cov_dispatcher is not None:
            self._unsub_cov_dispatcher()
            self._unsub_cov_dispatcher = None
        # A pending re-register must not resurrect the COV subscription after
        # the entity is gone.
        if self._cov_reregister_task is not None and not self._cov_reregister_task.done():
            self._cov_reregister_task.cancel()
        self._cov_reregister_task = None
        async with self._cov_lock:
            await self._async_stop_cov_runtime(final=True)

    async def async_release(self, priority: int) -> None:
        """Release the given priority slot: write Null to presentValue.

        Called by the bacnet_hub.release service; the point is re-read right
        after the write so the HA state updates without waiting for COV.
        """
        point = self._get_point()
        if not point:
            raise HomeAssistantError(f"{self.entity_id}: point payload unavailable")
        if not _point_is_commandable(point):
            raise ServiceValidationError(
                f"{self.entity_id}: point has no priority array to release"
            )
        app, address, object_type, object_instance = self._resolve_write_target(point)
        updates = await _async_release_point(
            app, address, object_type, object_instance, int(priority)
        )
        if updates:
            self._update_point_cache(updates)
        # Slow devices ack the write before the array is committed, so the
        # immediate re-read may still return the old slot value; verify with
        # the standard delayed read-back as well. A release that does not
        # change presentValue triggers no COV, so this is the only correction.
        self._schedule_priority_array_refresh()
        _LOGGER.debug(
            "Released %s at priority %s", self._point_key, int(priority)
        )

    def _cov_covers_priority_properties(self) -> bool:
        return set(CLIENT_COV_PROPERTY_SUBSCRIPTIONS_COMMANDABLE) <= self._cov_property_active

    @callback
    def _handle_priority_poll(self, _now: Any) -> None:
        if self._cov_covers_priority_properties():
            return
        self._schedule_priority_array_refresh()

    def _schedule_priority_array_refresh(self) -> None:
        """Schedule a delayed re-read of presentValue/priorityArray/relinquishDefault.

        Triggered by the periodic poll, by COV presentValue changes and after
        a release. BACnet sends no COV for the priority properties, and the
        read-back's delay doubles as a debounce for bursts.
        """
        point = self._get_point()
        if not _point_has_priority_array(point):
            return
        try:
            app, address, object_type, object_instance = self._resolve_write_target(point)
        except HomeAssistantError:
            return
        if self._readback_task is not None and not self._readback_task.done():
            self._readback_task.cancel()
        self._readback_task = create_logged_task(
            self.hass,
            self._async_readback_present_value(
                app, address, object_type, object_instance
            ),
            logger=_LOGGER,
            message=f"priorityArray refresh for {self._point_key}",
        )

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
            # Subscribe failures are handled with backoff inside
            # _async_register_cov; anything reaching here is unexpected.
            _LOGGER.warning("COV registration failed for %s", self._point_key, exc_info=True)

    async def _async_stop_cov_runtime(self, *, final: bool = False) -> None:
        if self._cov_lease_unsub is not None:
            try:
                self._cov_lease_unsub()
            except Exception:
                pass
            self._cov_lease_unsub = None
        if self._cov_refresh_task is not None and not self._cov_refresh_task.done():
            if self._cov_refresh_task is not asyncio.current_task():
                self._cov_refresh_task.cancel()
        self._cov_refresh_task = None
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
            await self._async_cleanup_cov_context(
                self._cov_context, call_aexit=True, final=final
            )
        self._cov_context = None
        self._cov_property_active = set()
        self._cov_registered = False

    async def _async_cleanup_cov_context(
        self, context_obj: Any, *, call_aexit: bool, final: bool = False
    ) -> None:
        if context_obj is None:
            return

        if call_aexit:
            # __aexit__ sends the cancel request and returns the device's
            # error response instead of raising.
            outcome: Any = None
            try:
                outcome = await context_obj.__aexit__(None, None, None)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                outcome = err
            if outcome is not None:
                log = _LOGGER.warning if final else _LOGGER.debug
                log(
                    "COV cancel for %s not accepted (%s); the device keeps the "
                    "subscription until its lease expires",
                    self._point_key,
                    outcome,
                )

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
        elif self._cov_registered and self._cov_context is not None:
            # Already subscribed to this target and the lease is renewed in
            # place; a rescan must not tear the subscription down.
            return
        if now < self._cov_retry_not_before_ts:
            return

        server = self.hass.data.get(DOMAIN, {}).get("servers", {}).get(self._entry_id)
        app = getattr(server, "app", None) if server is not None else None
        if app is None:
            self._set_client_points_unavailable(True, reason="bacnet_app_unavailable")
            return

        if _hub_cov_registry(app) is None:
            self._cov_registered = False
            self._set_client_points_unavailable(True, reason="cov_not_supported")
            return

        process_id = _cov_process_identifier(getattr(server, "instance", None))
        async with self._cov_lock:
            await self._async_stop_cov_runtime()

            subscribe_sem = _client_cov_subscribe_semaphore(
                self.hass, self._entry_id, self._client_id
            )
            async with subscribe_sem:
                opened_context, last_err = await _open_cov_subscription_context(
                    app,
                    address=address,
                    object_identifier=object_identifier,
                    process_id=process_id,
                    lifetime=CLIENT_COV_LEASE_SECONDS,
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

            wanted = list(CLIENT_COV_PROPERTY_SUBSCRIPTIONS_ALL)
            if _point_has_priority_array(point):
                wanted.extend(CLIENT_COV_PROPERTY_SUBSCRIPTIONS_COMMANDABLE)
            active: set[str] = set()
            async with subscribe_sem:
                for property_name in wanted:
                    err = await _open_cov_property_subscription(
                        self._cov_context, property_name
                    )
                    if err is None:
                        active.add(property_name)
                    else:
                        _LOGGER.debug(
                            "Device %s declined SubscribeCOVProperty %s for %s (%s); "
                            "falling back to polling",
                            address,
                            property_name,
                            object_identifier,
                            err,
                        )
            self._cov_property_active = active

            self._cov_task = create_logged_task(
                self.hass,
                self._async_cov_receive_loop(),
                logger=_LOGGER,
                message=f"COV receive loop for {self._point_key}",
            )
            self._schedule_cov_lease_refresh()

    def _schedule_cov_lease_refresh(self) -> None:
        if self._cov_lease_unsub is not None:
            try:
                self._cov_lease_unsub()
            except Exception:
                pass
            self._cov_lease_unsub = None

        # Renew in place before the lease expires (at CLIENT_COV_RENEW_FACTOR
        # of the lifetime): the same process id and object are re-subscribed,
        # which the device treats as a renewal of the existing entry.
        delay = max(30.0, float(CLIENT_COV_LEASE_SECONDS) * float(CLIENT_COV_RENEW_FACTOR))

        @callback
        def _lease_due(_now) -> None:
            self._cov_lease_unsub = None
            if self._cov_refresh_task is not None and not self._cov_refresh_task.done():
                return
            self._cov_refresh_task = create_logged_task(
                self.hass,
                self._async_refresh_cov_lease(),
                logger=_LOGGER,
                message=f"COV lease refresh for {self._point_key}",
            )

        self._cov_lease_unsub = async_call_later(self.hass, delay, _lease_due)

    async def _async_refresh_cov_lease(self) -> None:
        async with self._cov_lock:
            context = self._cov_context
            if context is None or not self._cov_registered:
                return
            try:
                await _async_refresh_cov_subscription(context)
            except asyncio.CancelledError:
                raise
            except BaseException as err:  # ErrorRejectAbortNack is a BaseException
                _LOGGER.debug(
                    "COV renewal for %s failed (%s); re-subscribing", self._point_key, err
                )
            else:
                self._schedule_cov_lease_refresh()
                return
        # Renewal declined: fall back to a full re-subscribe with backoff.
        self._start_cov_reregister_task()

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

            if isinstance(value, Exception):
                # bacpypes3 hands back a DecodingError instead of raising when
                # it cannot resolve the property type; never cache that.
                _LOGGER.debug(
                    "Undecodable COV value for %s/%s: %s", self._point_key, prop, value
                )
                continue

            key = _property_slug(prop)
            if not key:
                continue
            if key not in {
                "presentvalue",
                "statusflags",
                "priorityarray",
                "relinquishdefault",
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
            elif key == "priorityarray":
                priority_array = _normalize_priority_array(value)
                if priority_array is None:
                    continue
                point["priority_array"] = priority_array
            elif key == "relinquishdefault":
                relinquish_default = _normalize_priority_slot(value)
                if relinquish_default is None:
                    continue
                point["relinquish_default"] = relinquish_default
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

            if key == "presentvalue" and not self._cov_covers_priority_properties():
                self._schedule_priority_array_refresh()


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

