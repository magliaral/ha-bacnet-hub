# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Breaking Changes

- Removed the per-point release button entities (`button.bacnet_doi_*_release`).
  Use the new `bacnet_hub.release` service or the bundled
  `custom:bacnet-release-feature` tile feature instead. Existing registry
  entries for the buttons are not deleted actively; they become orphaned and
  disappear on the next reload of the integration.

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
- bacpypes3 is pinned to 0.0.108 (was 0.0.106): fixes an Error-PDU crash on
  unconfirmed requests and a Who-Is future race, adds the source address to
  error responses. The COV client API the hub relies on is unchanged.
- COV subscriptions ask for confirmed (acknowledged) notifications first and
  fall back to unconfirmed ones when a device rejects the request.
- All COV subscriptions of the hub use its own device instance (default
  `8123`) as subscriber process identifier, so entries in a device's
  `active_cov_subscriptions` list are recognisable as this hub.
- Home Assistant 2026.3.0 (the first release on Python 3.14, which is also
  what the test suite runs on) is now the minimum supported version, declared
  in `hacs.json`; the compatibility fallbacks for older target-resolution
  helpers were removed.
- SubscribeCOV requests are capped at 4 concurrent calls per client device.
  Registration now runs in background tasks and lease renewals fire almost
  simultaneously, so the cap keeps devices with many points from receiving a
  burst of requests.

### Fixed

- COV subscriptions used a random subscriber process identifier per point
  that changed on every restart, so devices accumulated duplicate entries
  until the old leases expired; the identifier is now stable.
- COV leases were renewed by cancelling and re-subscribing; they are now
  renewed in place with the same process identifier and object, as the
  standard defines. A declined renewal falls back to a full re-subscribe.
- A subscription that collided with a stale context was left behind on the
  device without a cancel request; failed cancel requests are now logged.
- Undecodable COV values are skipped instead of being cached as state.
- The `bacnet_hub.release` service resolved its targets with the deprecated
  `TargetSelectorData` helper, which HA 2026.9 reports at startup and removes
  in 2026.12.0. It now uses `TargetSelection`.
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
