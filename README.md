# BACnet Hub for Home Assistant

Expose Home Assistant entities as BACnet objects on a local BACnet/IP device.
The integration supports:

- HA -> BACnet live value mirroring
- BACnet -> HA writeback for supported mappings
- Labels-based automatic import and mapping refresh

Built with `bacpypes3`.

## Current Model (Important)

This integration now uses a labels-first workflow:

- Mapping mode is labels-only.
- Entities are auto-discovered from selected Home Assistant labels.
- Auto sync is event-driven (registry/label updates) with debounce.

During setup, the integration tries to create a default label:

- Name: `BACnet`
- Icon: `mdi:server-network-outline`
- Color: `light-green`

If created or found, it is preselected for import.

## Features

- Local BACnet/IP device with configurable:
  - Device Instance
  - Bind address (`IPv4/prefix:port`, for example `192.168.1.10/24:47808`)
- Automatic mapping from Home Assistant labels (multiple labels supported)
- Automatic cleanup:
  - Removes stale mappings
  - Removes orphaned integration entities
  - Resets counters when import labels change
- BACnet object support:
  - `analogValue` (AV)
  - `binaryValue` (BV)
  - `multiStateValue` (MSV)
- Climate-specific mapping support (details below)
- Reload service: `bacnet_hub.reload`

## Installation

### HACS (Custom Repository)

1. HACS -> Integrations -> menu -> Custom repositories.
2. Add this repo as type `Integration`.
3. Install `BACnet Hub`.
4. Restart Home Assistant.
5. Go to Settings -> Devices and Services -> Add Integration -> BACnet Hub.

### Manual

Copy `custom_components/bacnet_hub` to `config/custom_components/`, then restart Home Assistant.

## Configuration

### Initial Setup

Required fields:

- `instance`: BACnet device instance
- `address`: local BACnet/IP bind address (`IPv4/prefix:port`)
- `device_name`: BACnet Device `objectName` (default: `HA-BACnetHub`)
- `device_description`: BACnet Device `description` (default: `BACnet Hub - Home Assistant Custom Integration`)

After setup, open integration options and configure:

- `instance`
- `address`
- `device_name`
- `device_description`
- `labels` (one or multiple)

At least one valid label must be selected.

### Label Import Behavior

- Entities are collected from selected labels.
- Label assignment can be on entity level or device level in Home Assistant.
- Matching entities are auto-mapped.
- Sync cycle:
  - once at startup
  - on registry/label changes (debounced)

## Mapping Behavior

### Generic Domains

Auto type detection for non-climate entities:

- Binary-like domains (`light`, `switch`, `cover`, etc.) -> BV
- Numeric or unit-based states -> AV
- Fallback -> BV

### Climate Domain

For `climate.*`, multiple BACnet points can be generated:

- `hvac_mode`
  - BV when modes are effectively `off` and `heat`
  - otherwise MSV (full mode list)
  - writable via `climate.set_hvac_mode`
- `hvac_action`
  - BV, read-only
  - mapped as: `idle` or `off` -> 0, otherwise 1
- `current_temperature`
  - AV, read-only
  - `covIncrement` default: `0.2`
- `set_temperature` (HA attribute `temperature`)
  - AV, writable via `climate.set_temperature`
  - `covIncrement` default: `0.1`

## Writeback (BACnet -> Home Assistant)

Writes are allowed only when a mapping is compatible and required HA service exists.

Supported write targets:

- `light`, `switch`, `fan`, `group`: `turn_on` and `turn_off`
- `cover`: `open_cover` and `close_cover`
- `number`, `input_number`: `set_value`
- `climate`:
  - `set_hvac_mode`
  - `set_temperature`

If a mapping is not writable, `WriteProperty` is rejected with `writeAccessDenied`.
Local PV and COV are not mutated for denied writes.

## Runtime Notes

- BACnet object names use `entity_id` or `entity_id.source_attr` for attribute-based mappings.
- Friendly names are refreshed from HA and applied to BACnet descriptions.
- Integration entities are created as diagnostics under one BACnet Hub device:
  - AV -> `sensor`
  - BV -> `binary_sensor`
- MSV currently has no dedicated HA entity platform; it still exists as BACnet object.

## Troubleshooting

- Address already in use:
  - Ensure no second BACnet process is bound to the same `IP:port`.
  - The integration performs bind preflight retries, but persistent conflicts must be resolved externally.
- Import seems not updating:
  - Verify selected labels in options.
  - Verify entities or devices actually carry those labels.
  - Trigger integration reload if no relevant registry/label event occurred.
- BACnet write has no effect:
  - Check if domain and action are supported.
  - Check Home Assistant service availability for that domain.
- Entity unavailable in HA:
  - Mapping may be removed automatically on sync if the source no longer exists.

## Service

- `bacnet_hub.reload`
  - Reload one entry by `entry_id`.
  - If only one entry exists, `entry_id` can be omitted.

## Development

- Python: aligned with Home Assistant runtime
- Key dependency: `bacpypes3==0.0.102`
- Integration domain: `bacnet_hub`

## License

MIT. See `LICENSE`.

Copyright (c) 2025-2026 Alessio Magliarella
