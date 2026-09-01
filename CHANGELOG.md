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
