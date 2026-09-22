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
- Home Assistant 2026.3.0 (the first release on Python 3.14, which is also
  what the test suite runs on) is now the minimum supported version, declared
  in `hacs.json`; the compatibility fallbacks for older target-resolution
  helpers were removed.
- SubscribeCOV requests are capped at 4 concurrent calls per client device.
  Registration now runs in background tasks and lease renewals fire almost
  simultaneously, so the cap keeps devices with many points from receiving a
  burst of requests.

### Fixed

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
