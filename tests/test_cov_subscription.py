"""Tests for the hub's COV subscription contexts and notification dispatch."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from bacpypes3.apdu import (
    ConfirmedCOVNotificationRequest,
    ErrorRejectAbortNack,
    SimpleAckPDU,
    SubscribeCOVRequest,
    UnconfirmedCOVNotificationRequest,
)
from bacpypes3.basetypes import PropertyIdentifier, PropertyValue
from bacpypes3.constructeddata import Any as BacAny
from bacpypes3.errors import ServicesError
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import ObjectIdentifier, Real

from custom_components.bacnet_hub.client_runtime import (
    DEFAULT_COV_PROCESS_IDENTIFIER,
    CovObjectSubscription,
    _async_refresh_cov_subscription,
    _cov_process_identifier,
    _open_cov_subscription_context,
)
from custom_components.bacnet_hub.server import HubApp

ADDRESS = "192.168.1.10"
OID = "analog-input,0"


class _Rejected(ErrorRejectAbortNack):
    """Stand-in for an Error/Reject/Abort response."""

    reason = "optionalFunctionalityNotSupported"


class FakeApp:
    """Records requests and answers them from a queue (None = SimpleAck)."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self._hub_cov_contexts: dict[Any, Any] = {}
        self.requests: list[Any] = []
        self.responses = list(responses or [])

    async def request(self, apdu: Any) -> Any:
        self.requests.append(apdu)
        return self.responses.pop(0) if self.responses else None


def _key(pid: int = 8123, oid: str = OID) -> tuple[Any, int, Any]:
    return (Address(ADDRESS), pid, ObjectIdentifier(oid))


def _subscribe_requests(app: FakeApp) -> list[SubscribeCOVRequest]:
    return [r for r in app.requests if isinstance(r, SubscribeCOVRequest)]


# --- process identifier -----------------------------------------------------


@pytest.mark.parametrize(
    ("hub_instance", "expected"),
    [(8123, 8123), ("4711", 4711), (1, 1), (4194303, 4194303)],
)
def test_process_identifier_is_hub_instance(hub_instance: Any, expected: int) -> None:
    assert _cov_process_identifier(hub_instance) == expected


@pytest.mark.parametrize("hub_instance", [None, 0, -5, 4194304, "abc"])
def test_process_identifier_falls_back_when_out_of_range(hub_instance: Any) -> None:
    assert _cov_process_identifier(hub_instance) == DEFAULT_COV_PROCESS_IDENTIFIER == 8123


# --- subscribe / cancel ------------------------------------------------------


async def test_open_subscribes_confirmed_and_registers_by_object() -> None:
    app = FakeApp()

    context, err = await _open_cov_subscription_context(
        app, address=ADDRESS, object_identifier=OID, process_id=8123, lifetime=600
    )

    assert err is None and context is not None
    requests = _subscribe_requests(app)
    assert len(requests) == 1
    req = requests[0]
    assert req.subscriberProcessIdentifier == 8123
    assert req.monitoredObjectIdentifier == ObjectIdentifier(OID)
    assert bool(req.issueConfirmedNotifications) is True  # bacpypes3 Boolean coerces to 1
    assert req.lifetime == 600
    assert app._hub_cov_contexts[_key()] is context
    assert context.issue_confirmed_notifications is True
    # the hub owns the renewal schedule; bacpypes3's timer is left armed
    # until the refresh helper or cleanup cancels it
    assert context.refresh_subscription_handle is not None
    context.refresh_subscription_handle.cancel()


async def test_two_objects_share_the_process_id() -> None:
    app = FakeApp()

    ctx_a, _ = await _open_cov_subscription_context(
        app, address=ADDRESS, object_identifier="analog-input,0", process_id=8123, lifetime=600
    )
    ctx_b, _ = await _open_cov_subscription_context(
        app, address=ADDRESS, object_identifier="binary-output,3", process_id=8123, lifetime=600
    )

    assert ctx_a is not None and ctx_b is not None
    assert set(app._hub_cov_contexts) == {_key(oid="analog-input,0"), _key(oid="binary-output,3")}
    for ctx in (ctx_a, ctx_b):
        ctx.refresh_subscription_handle.cancel()


async def test_open_falls_back_to_unconfirmed_when_rejected() -> None:
    app = FakeApp(responses=[_Rejected(), None])

    context, err = await _open_cov_subscription_context(
        app, address=ADDRESS, object_identifier=OID, process_id=8123, lifetime=600
    )

    assert err is None and context is not None
    requests = _subscribe_requests(app)
    assert [r.issueConfirmedNotifications for r in requests] == [True, False]
    assert {r.subscriberProcessIdentifier for r in requests} == {8123}
    assert context.issue_confirmed_notifications is False
    assert app._hub_cov_contexts[_key()] is context
    context.refresh_subscription_handle.cancel()


async def test_open_reports_error_when_both_modes_rejected() -> None:
    app = FakeApp(responses=[_Rejected(), _Rejected()])

    context, err = await _open_cov_subscription_context(
        app, address=ADDRESS, object_identifier=OID, process_id=8123, lifetime=600
    )

    assert context is None
    assert isinstance(err, ErrorRejectAbortNack)
    assert app._hub_cov_contexts == {}


async def test_open_without_registry_is_unsupported() -> None:
    context, err = await _open_cov_subscription_context(
        SimpleNamespace(), address=ADDRESS, object_identifier=OID, process_id=8123, lifetime=600
    )
    assert context is None
    assert isinstance(err, RuntimeError) and str(err) == "cov_not_supported"


async def test_aexit_sends_cancel_form_and_deregisters() -> None:
    app = FakeApp()
    context, _ = await _open_cov_subscription_context(
        app, address=ADDRESS, object_identifier=OID, process_id=8123, lifetime=600
    )
    assert context is not None

    outcome = await context.__aexit__(None, None, None)

    assert outcome is None
    cancel = _subscribe_requests(app)[-1]
    assert cancel.subscriberProcessIdentifier == 8123
    assert cancel.monitoredObjectIdentifier == ObjectIdentifier(OID)
    assert cancel.issueConfirmedNotifications is None
    assert cancel.lifetime is None
    assert app._hub_cov_contexts == {}
    assert context.refresh_subscription_handle is None


async def test_aexit_returns_device_error_instead_of_raising() -> None:
    app = FakeApp(responses=[None, _Rejected()])
    context, _ = await _open_cov_subscription_context(
        app, address=ADDRESS, object_identifier=OID, process_id=8123, lifetime=600
    )
    assert context is not None

    outcome = await context.__aexit__(None, None, None)

    assert isinstance(outcome, ErrorRejectAbortNack)
    assert app._hub_cov_contexts == {}


async def test_reopening_same_object_closes_stale_context() -> None:
    app = FakeApp()
    stale, _ = await _open_cov_subscription_context(
        app, address=ADDRESS, object_identifier=OID, process_id=8123, lifetime=600
    )
    assert stale is not None

    fresh, err = await _open_cov_subscription_context(
        app, address=ADDRESS, object_identifier=OID, process_id=8123, lifetime=600
    )

    assert err is None and fresh is not None and fresh is not stale
    assert app._hub_cov_contexts[_key()] is fresh
    kinds = [
        (r.lifetime is None and r.issueConfirmedNotifications is None)
        for r in _subscribe_requests(app)
    ]
    # subscribe, cancel(stale), subscribe
    assert kinds == [False, True, False]
    assert stale.refresh_subscription_handle is None
    fresh.refresh_subscription_handle.cancel()


# --- renewal -----------------------------------------------------------------


async def test_refresh_renews_in_place_and_owns_the_timer() -> None:
    app = FakeApp()
    context, _ = await _open_cov_subscription_context(
        app, address=ADDRESS, object_identifier=OID, process_id=8123, lifetime=600
    )
    assert context is not None
    first_handle = context.refresh_subscription_handle
    assert first_handle is not None

    await _async_refresh_cov_subscription(context)

    assert first_handle.cancelled()
    assert context.refresh_subscription_handle is None
    requests = _subscribe_requests(app)
    assert len(requests) == 2
    renew = requests[-1]
    assert renew.subscriberProcessIdentifier == 8123
    assert renew.monitoredObjectIdentifier == ObjectIdentifier(OID)
    assert bool(renew.issueConfirmedNotifications) is True
    assert renew.lifetime == 600
    # still registered, no cancel was sent
    assert app._hub_cov_contexts[_key()] is context


async def test_refresh_propagates_rejection() -> None:
    app = FakeApp(responses=[None, _Rejected()])
    context, _ = await _open_cov_subscription_context(
        app, address=ADDRESS, object_identifier=OID, process_id=8123, lifetime=600
    )
    assert context is not None

    with pytest.raises(ErrorRejectAbortNack):
        await _async_refresh_cov_subscription(context)
    assert context.refresh_subscription_handle is None


# --- notification dispatch in HubApp ---------------------------------------


def _notification(cls: type, pid: int = 8123, oid: str = OID, value: float = 21.5) -> Any:
    apdu = cls(
        subscriberProcessIdentifier=pid,
        initiatingDeviceIdentifier=ObjectIdentifier("device,1031010"),
        monitoredObjectIdentifier=ObjectIdentifier(oid),
        timeRemaining=500,
        listOfValues=[
            PropertyValue(
                propertyIdentifier=PropertyIdentifier.presentValue,
                value=BacAny(Real(value)),
            )
        ],
    )
    apdu.pduSource = Address(ADDRESS)
    return apdu


def _hub_app() -> Any:
    """A HubApp shell without the network stack, enough for the dispatch."""
    app = HubApp.__new__(HubApp)
    app._hub_cov_contexts = {}
    app._cov_contexts = {}
    app.responses: list[Any] = []

    async def _response(pdu: Any) -> None:
        app.responses.append(pdu)

    app.response = _response
    return app


async def test_confirmed_notification_is_routed_by_object_and_acked() -> None:
    app = _hub_app()
    ctx_a = CovObjectSubscription(app, Address(ADDRESS), ObjectIdentifier(OID), 8123, True, 600)
    ctx_b = CovObjectSubscription(
        app, Address(ADDRESS), ObjectIdentifier("binary-output,3"), 8123, True, 600
    )
    app._hub_cov_contexts[ctx_a.registry_key] = ctx_a
    app._hub_cov_contexts[ctx_b.registry_key] = ctx_b

    await app.do_ConfirmedCOVNotificationRequest(
        _notification(ConfirmedCOVNotificationRequest, oid="binary-output,3", value=1.0)
    )

    assert ctx_a.queue.qsize() == 0
    assert ctx_b.queue.qsize() == 1
    assert len(app.responses) == 1 and isinstance(app.responses[0], SimpleAckPDU)


async def test_unconfirmed_notification_is_routed_without_ack() -> None:
    app = _hub_app()
    ctx = CovObjectSubscription(app, Address(ADDRESS), ObjectIdentifier(OID), 8123, False, 600)
    app._hub_cov_contexts[ctx.registry_key] = ctx

    await app.do_UnconfirmedCOVNotificationRequest(
        _notification(UnconfirmedCOVNotificationRequest)
    )

    assert ctx.queue.qsize() == 1
    assert app.responses == []


async def test_unknown_subscription_falls_back_to_bacpypes3_error() -> None:
    app = _hub_app()

    with pytest.raises(ServicesError):
        await app.do_ConfirmedCOVNotificationRequest(
            _notification(ConfirmedCOVNotificationRequest, pid=999)
        )
    # unconfirmed: bacpypes3 silently drops unknown subscriptions
    await app.do_UnconfirmedCOVNotificationRequest(
        _notification(UnconfirmedCOVNotificationRequest, pid=999)
    )
    assert app.responses == []


async def test_queued_value_decodes_to_present_value() -> None:
    """get_value() is inherited; make sure our context still resolves values."""
    app = _hub_app()
    app.device_info_cache = SimpleNamespace(get_device_info=_no_device_info)
    ctx = CovObjectSubscription(app, Address(ADDRESS), ObjectIdentifier(OID), 8123, True, 600)
    app._hub_cov_contexts[ctx.registry_key] = ctx

    await app.do_UnconfirmedCOVNotificationRequest(
        _notification(UnconfirmedCOVNotificationRequest, value=21.5)
    )
    prop, value = await asyncio.wait_for(ctx.get_value(), timeout=1.0)

    assert prop == PropertyIdentifier.presentValue
    assert float(value) == pytest.approx(21.5)


async def _no_device_info(_address: Any) -> None:
    return None


# --- SubscribeCOVProperty (Phase B) ------------------------------------------

from bacpypes3.apdu import SubscribeCOVPropertyRequest  # noqa: E402

from custom_components.bacnet_hub.client_runtime import (  # noqa: E402
    _open_cov_property_subscription,
)


def _property_requests(app: FakeApp) -> list[SubscribeCOVPropertyRequest]:
    return [r for r in app.requests if isinstance(r, SubscribeCOVPropertyRequest)]


async def _object_context(app: FakeApp) -> CovObjectSubscription:
    context, err = await _open_cov_subscription_context(
        app, address=ADDRESS, object_identifier="binary-output,3", process_id=8123, lifetime=600
    )
    assert err is None and context is not None
    context.refresh_subscription_handle.cancel()
    return context


async def test_property_subscription_shares_pid_and_object() -> None:
    app = FakeApp()
    context = await _object_context(app)

    err = await _open_cov_property_subscription(context, "priorityArray")

    assert err is None
    assert len(context.property_subscriptions) == 1
    req = _property_requests(app)[0]
    assert req.subscriberProcessIdentifier == 8123
    assert req.monitoredObjectIdentifier == ObjectIdentifier("binary-output,3")
    assert req.monitoredPropertyIdentifier.propertyIdentifier == PropertyIdentifier.priorityArray
    assert req.monitoredPropertyIdentifier.propertyArrayIndex is None
    assert bool(req.issueConfirmedNotifications) is True
    assert req.lifetime == 600
    assert req.covIncrement is None
    # not registered on its own: notifications go to the object context
    assert list(app._hub_cov_contexts.values()) == [context]


async def test_property_subscription_inherits_unconfirmed_mode() -> None:
    app = FakeApp(responses=[_Rejected(), None])
    context, _ = await _open_cov_subscription_context(
        app, address=ADDRESS, object_identifier=OID, process_id=8123, lifetime=600
    )
    assert context is not None and context.issue_confirmed_notifications is False
    context.refresh_subscription_handle.cancel()

    assert await _open_cov_property_subscription(context, "outOfService") is None
    assert bool(_property_requests(app)[0].issueConfirmedNotifications) is False


async def test_declined_property_subscription_is_reported_not_kept() -> None:
    app = FakeApp()
    context = await _object_context(app)
    app.responses = [_Rejected()]

    err = await _open_cov_property_subscription(context, "relinquishDefault")

    assert isinstance(err, ErrorRejectAbortNack)
    assert context.property_subscriptions == []


async def test_object_aexit_cancels_property_subscriptions_first() -> None:
    app = FakeApp()
    context = await _object_context(app)
    assert await _open_cov_property_subscription(context, "priorityArray") is None
    assert await _open_cov_property_subscription(context, "relinquishDefault") is None
    app.requests.clear()

    outcome = await context.__aexit__(None, None, None)

    assert outcome is None
    assert context.property_subscriptions == []
    assert app._hub_cov_contexts == {}
    kinds = [type(r).__name__ for r in app.requests]
    assert kinds == [
        "SubscribeCOVPropertyRequest",
        "SubscribeCOVPropertyRequest",
        "SubscribeCOVRequest",
    ]
    for req in app.requests[:2]:
        assert req.lifetime is None and req.issueConfirmedNotifications is None
        assert req.monitoredPropertyIdentifier is not None


async def test_refresh_renews_property_subscriptions_too() -> None:
    app = FakeApp()
    context = await _object_context(app)
    assert await _open_cov_property_subscription(context, "outOfService") is None
    app.requests.clear()

    await _async_refresh_cov_subscription(context)

    kinds = [type(r).__name__ for r in app.requests]
    assert kinds == ["SubscribeCOVRequest", "SubscribeCOVPropertyRequest"]
    assert all(r.lifetime == 600 for r in app.requests)
