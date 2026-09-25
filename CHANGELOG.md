# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [2.4.1] - 2026-09-26

### Changed

- **Breaking:** the status attributes of imported points are combined into
  one `status_flags` attribute with `in_alarm`, `fault`, `overridden` and
  `out_of_service`, taken from the object's status flags. The separate
  attributes `out_of_service`, `in_alarm`, `fault`, `overridden`,
  `reliability` and `event_state` are gone; templates read for example
  `state_attr(entity, 'status_flags').out_of_service`.
- Leaner COV: out of service now comes from the status flags that arrive
  with every value notification, so `outOfService` is no longer subscribed
  per property. `relinquishDefault` is no longer subscribed either; it is
  read at import and after writes. Only commandable points keep a property
  subscription, for `priorityArray`. Reliability and event state are no
  longer re-read when a status flag changes.

### Fixed

- Removing a point whose object the controller no longer has (for example
  with `bacnet_hub.remove_missing_points`) no longer logs "Task exception
  was never retrieved": the controller's *unknown object* answer to the COV
  cancellation is handled instead of escaping the entity teardown, and it
  is logged at debug level only.

## [2.3.1] - 2026-09-24

### Added

- Service `bacnet_hub.remove_missing_points` removes the entities of points
  their controller no longer lists; such points are flagged during rescans or
  when the device answers *unknown object*, shown as unavailable and no
  longer subscribed.

### Fixed

- One point that could not be subscribed marked every point of its device
  unavailable, and one that succeeded marked all of them available again;
  availability is now tracked per point.

- A SubscribeCOVProperty request that timed out was remembered as
  unsupported for the object type until the next start; only an explicit
  error answer is remembered now, a timeout is retried on the next
  registration.

- A COV renewal the device did not answer (for example while it rebooted)
  left the point permanently without a subscription: the re-subscribe saw a
  registration it believed healthy and returned, and no further renewal was
  scheduled. The failed renewal now clears the registration, a failed
  re-subscribe retries on its own with the existing backoff, and a device
  announcing itself with I-Am triggers an in-place renewal (at most every
  30 seconds per point) because it may have lost its subscriptions.

## [2.3.0] - 2026-09-24

### Added

- Service `bacnet_hub.set_present_value` writes the present value of one or
  more points, including inputs that are out of service (sensor
  simulation); commandable points are written at the configured priority.

- Every imported client point exposes its BACnet status as attributes:
  `out_of_service`, `in_alarm`, `fault`, `overridden`, `reliability` and
  `event_state`, kept current through COV; `reliability` and `event_state`
  are re-read whenever a status flag changes.

- Service `bacnet_hub.set_out_of_service` writes the `outOfService` property
  of one or more points (targeted by entity) and re-reads the point
  immediately.

- Analog `number` entities take minimum, maximum and step from the object's
  `minPresValue`, `maxPresValue` and `resolution` instead of Home Assistant's
  defaults of 0 to 100 and 0.1.

### Fixed

- A device's Error answer to SubscribeCOVProperty (and a few other confirmed
  services) was silently dropped, because bacpypes3 registers no Error type
  for those services and the request appeared unanswered until the 10-second
  timeout. The missing types are now registered at startup, so a declined
  property subscription is reported immediately and polling starts without
  delay.

- A write the device answered with an Error, Reject or Abort was treated as
  success; it is now reported as an error with the device's reason.

- Status flags with no active flag were cached as unknown.

- The result of ReadPropertyMultiple was never used, so every property was
  read with its own request; the response is now mapped, which makes the
  point import and read-backs considerably cheaper on devices that support
  it.

## [2.2.0] - 2026-09-24

### Changed

- Imported client points are enabled as soon as they are discovered, on new
  devices as well as for points that appear on a known controller later.
  Points that earlier versions had registered disabled are enabled once at
  the next start; points you disabled yourself stay disabled.

## [2.1.0] - 2026-09-24

### Breaking Changes

- Removed the per-device **Write priority** select entities
  (`select.bacnet_doi_<client>_write_priority`). The write priority is now a
  single option in the hub's device settings (default `8`, Manual Operator)
  that applies to all client devices; stale select entries are removed at the
  next start.

### Added

- Client points additionally subscribe per property with SubscribeCOVProperty:
  `outOfService` on all points, `priorityArray` and `relinquishDefault` on
  commandable points. External changes to the priority array now arrive as
  COV notifications; the 30-second poll only remains for points whose device
  declines the property subscription.

### Changed

- bacpypes3 is pinned to 0.0.108 (was 0.0.106): fixes an Error-PDU crash on
  unconfirmed requests and a Who-Is future race, adds the source address to
  error responses. The COV client API the hub relies on is unchanged.

- A periodic client rescan no longer tears down and re-creates healthy COV
  subscriptions; they are only rebuilt when the target changed or the
  receive loop failed.

- COV subscriptions ask for confirmed (acknowledged) notifications first and
  fall back to unconfirmed ones when a device rejects the request.

- All COV subscriptions of the hub use its own device instance (default
  `8123`) as subscriber process identifier, so entries in a device's
  `active_cov_subscriptions` list are recognisable as this hub.

### Fixed

- A device that never answers a SubscribeCOVProperty request (seen on a
  bacnet-stack based controller for `priorityArray`) blocked the point's
  registration forever: bacpypes3 leaves the timeout to the caller, so the
  COV receive loop was never started, the point's notifications were never
  consumed and the per-device subscribe slots stayed occupied, leaving other
  points of the same device without any subscription. Every COV request is
  now bounded to 10 seconds, the receive loop starts as soon as the object
  subscription is accepted, and a property a device declines or ignores is
  remembered per device and object type instead of being requested again on
  every renewal.

- The `debug_bacpypes` option only raised logger levels, which bacpypes3
  ignores; it now sets bacpypes3's module debug flags, so application,
  COV and Who-Is/I-Am handling are actually logged. Each COV registration
  also logs the accepted property subscriptions at debug level.

- COV subscriptions used a random subscriber process identifier per point
  that changed on every restart, so devices accumulated duplicate entries
  until the old leases expired; the identifier is now stable.

- COV leases were renewed by cancelling and re-subscribing; they are now
  renewed in place with the same process identifier and object, as the
  standard defines. A declined renewal falls back to a full re-subscribe.

- A subscription that collided with a stale context was left behind on the
  device without a cancel request; failed cancel requests are now logged.

- Undecodable COV values are skipped instead of being cached as state.

## [2.0.3] - 2026-09-22

### Changed

- Home Assistant 2026.3.0 (the first release on Python 3.14, which is also
  what the test suite runs on) is now the minimum supported version, declared
  in `hacs.json`; the compatibility fallbacks for older target-resolution
  helpers were removed.

### Fixed

- The `bacnet_hub.release` service resolved its targets with the deprecated
  `TargetSelectorData` helper, which HA 2026.9 reports at startup and removes
  in 2026.12.0. It now uses `TargetSelection`.

## [2.0.2] - 2026-09-22

### Changed

- SubscribeCOV requests are capped at 4 concurrent calls per client device.
  Registration now runs in background tasks and lease renewals fire almost
  simultaneously, so the cap keeps devices with many points from receiving a
  burst of requests.

### Fixed

- The periodic client rediscovery timer ran its handler in an executor thread
  and called `hass.async_create_task` from there, causing
  `RuntimeError: ... calls hass.async_create_task from a thread other than the
  event loop` and `coroutine ... was never awaited` warnings every interval.
  Timer handlers are now event-loop callbacks.

- COV receive loops, client discovery, read-back and event-sync tasks were
  created as setup-tracked tasks, which held up the Home Assistant bootstrap
  (`Setup timed out for bootstrap waiting on ... _async_cov_receive_loop`).
  They are now Home Assistant background tasks and the COV subscription no
  longer blocks platform setup with one network round trip per entity.

## [2.0.1] - 2026-09-06

### Fixed

- Client devices are linked to the hub with `via_device_id` on Home Assistant
  2026.9, where the deprecated `via_device` made the core stop adding entities
  after the first client point per object type.

## [2.0.0] - 2026-09-01

### Breaking Changes

- Removed the per-point release button entities (`button.bacnet_doi_*_release`).
  Use the new `bacnet_hub.release` service or the bundled
  `custom:bacnet-release-feature` tile feature instead. Their registry
  entries are removed automatically at the next start of the integration.

### Added

- Service `bacnet_hub.release`: releases a priority array slot (default 8,
  Manual Operator) of one or more commandable client points and re-reads the
  point immediately so the state updates without waiting for COV. Errors are
  collected per entity and reported bundled.

- Writable client point entities expose `priority_array` (16 slots, `null`
  for free slots; excluded from the recorder) and `relinquish_default` as
  state attributes.

- Bundled Lovelace tile feature `custom:bacnet-release-feature` served by the
  integration itself — no manual resource setup required.

- The priority array is re-read after COV `presentValue` changes, and
  commandable points poll `priorityArray`/`relinquishDefault` every 30
  seconds, so external changes are reflected even when the value itself
  does not change.

### Changed

- Default write priority is now `8` (Manual Operator) instead of `16`.

Releases before 2.0.0 are documented in the GitHub releases.

[Unreleased]: https://github.com/magliaral/ha-bacnet-hub/compare/v2.4.1...HEAD
[2.4.1]: https://github.com/magliaral/ha-bacnet-hub/compare/v2.3.1...v2.4.1
[2.3.1]: https://github.com/magliaral/ha-bacnet-hub/compare/v2.3.0...v2.3.1
[2.3.0]: https://github.com/magliaral/ha-bacnet-hub/compare/v2.2.0...v2.3.0
[2.2.0]: https://github.com/magliaral/ha-bacnet-hub/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/magliaral/ha-bacnet-hub/compare/v2.0.3...v2.1.0
[2.0.3]: https://github.com/magliaral/ha-bacnet-hub/compare/v2.0.2...v2.0.3
[2.0.2]: https://github.com/magliaral/ha-bacnet-hub/compare/v2.0.1...v2.0.2
[2.0.1]: https://github.com/magliaral/ha-bacnet-hub/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/magliaral/ha-bacnet-hub/compare/v1.4.0...v2.0.0
