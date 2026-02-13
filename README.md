# BACnet Hub for Home Assistant

Expose Home Assistant entities as BACnet objects on a local BACnet/IP device.

Main capabilities:

- HA -> BACnet live mirroring (state and selected attributes)
- BACnet -> HA writeback for supported mappings
- Automatic label-based import and mapping lifecycle
- Event-driven sync (no periodic polling)

Built with `bacpypes3`.

## Current Architecture

The integration is labels-first and auto-managed:

- Mapping mode is labels-only.
- Entities are discovered from selected Home Assistant labels.
- Sync runs at startup and on registry/label events (debounced).
- Stale mappings and orphan integration entities are cleaned automatically.

On setup, the integration tries to create and preselect a default label:

- Name: `BACnet`
- Icon: `mdi:server-network-outline`
- Color: `light-green`

## Installation

### HACS (custom repository)

1. HACS -> Integrations -> Custom repositories
2. Add this repository as type `Integration`
3. Install `BACnet Hub`
4. Restart Home Assistant
5. Settings -> Devices and Services -> Add Integration -> BACnet Hub

### Manual

Copy `custom_components/bacnet_hub` to `config/custom_components/`, then restart Home Assistant.

## Configuration

### Initial setup

Required fields:

- `instance`: BACnet device instance
- `address`: local BACnet/IP bind address in format `IPv4/prefix:port`
- `device_name`: BACnet Device `objectName`
- `device_description`: BACnet Device `description`

Defaults:

- `device_name`: `HA-BACnet-Hub`
- `device_description`: `BACnet Hub - Home Assistant Custom Integration`

### Options flow

Use integration options to manage:

- device parameters (`instance`, `address`, `device_name`, `device_description`)
- import labels (`import_labels`, multi-select)

At least one valid label must be selected.

## Mapping Model

### Generic entities

Automatic object type detection:

- binary-like domains (`binary_sensor`, `switch`, `light`, `lock`, `cover`, `input_boolean`, `alarm_control_panel`, `device_tracker`, `button`) -> `binaryValue`
- numeric/unit-based states -> `analogValue`
- fallback -> `binaryValue`

### Climate entities

For `climate.*`, multiple BACnet points can be created:

- `hvac_mode`
- `hvac_action`
- `current_temperature`
- `set_temperature` (read from HA attribute `temperature`)

`hvac_mode` becomes:

- `binaryValue` for simple `off/heat`
- otherwise `multiStateValue` with dynamic states list

## Entity IDs and Unique IDs

Published mirror entities use deterministic IDs based on object type + instance.

Examples:

- `sensor.bacnet_hub_analog_value_0`
- `binary_sensor.bacnet_hub_binary_value_6`

Unique IDs are stable and include hub identity:

- `bacnet_hub:hub:<hub_key>:<object-type>:<instance>`

Where object type is kebab-case (`analog-value`, `binary-value`, `multi-state-value`).

## Mirrored Entity Behavior

Published HA entities are diagnostic entities under one BACnet Hub device:

- `analogValue` -> `sensor`
- `binaryValue` -> `binary_sensor`
- `multiStateValue` -> BACnet object only (currently no HA entity platform)

Mirroring details:

- state/value is mirrored from source entity (or mapped source attribute)
- friendly names are refreshed from HA and written into BACnet descriptions
- extra state attributes are mirrored, excluding core presentation keys (`friendly_name`, `icon`, `unit_of_measurement`, `device_class`, `state_class`, `entity_category`)
- `source_entity_id` is added to mirrored attributes
- entity category remains `diagnostic`

## BACnet Writeback (BACnet -> Home Assistant)

Write is allowed only when mapping type matches and required HA service exists.

Supported write targets:

- `light`, `switch`, `fan`, `group` -> `turn_on` / `turn_off`
- `cover` -> `open_cover` / `close_cover`
- `number`, `input_number` -> `set_value`
- `climate` -> `set_hvac_mode`, `set_temperature`

If mapping is read-only, BACnet `WriteProperty` is rejected with `writeAccessDenied`.

## Runtime Details

- BACnet UDP bind conflicts are handled with preflight retries and backoff

## Troubleshooting

- Import not updating: check selected labels, verify labels on entity/device.
- Old/legacy entity IDs still visible: remove stale entity registry entries and reload integration.
- Address already in use: ensure no second BACnet process is bound to the same `IP:port`.

## License

MIT. See `LICENSE`.

Copyright (c) 2025-2026 Alessio Magliarella
