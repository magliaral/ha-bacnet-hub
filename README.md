# BACnet Hub for Home Assistant

Expose Home Assistant entities as BACnet objects on a local BACnet/IP device and import remote BACnet client points back into Home Assistant.

Built with `bacpypes3`.

## What This Integration Does

This integration has two roles:

1. Home Assistant -> BACnet (local BACnet device)
- Publishes selected HA entities as BACnet objects.
- Keeps BACnet present values synchronized from HA state changes.
- Supports BACnet -> HA writeback for supported mappings.

2. BACnet -> Home Assistant (remote BACnet clients)
- Discovers remote BACnet devices via `Who-Is/I-Am`.
- Imports supported remote points as HA entities.
- Uses BACnet COV subscriptions for event-driven updates.

## Key Features

- Labels-first auto mapping (no manual mapping UI).
- Automatic mapping lifecycle: add, refresh, remove, cleanup.
- Event-driven sync with debounce on registry/label/area changes.
- Deterministic entity IDs and stable unique IDs.
- Built-in diagnostics for hub and discovered clients.
- Integration service: `bacnet_hub.reload`.

## Requirements

- Home Assistant with custom integrations enabled.
- Network access to BACnet/IP segment.
- Dependency (from `manifest.json`):
  - `bacpypes3==0.0.102`

## Installation

### HACS (custom repository)

1. HACS -> Integrations -> Custom repositories.
2. Add this repository as type `Integration`.
3. Install `BACnet Hub`.
4. Restart Home Assistant.
5. Settings -> Devices & Services -> Add Integration -> `BACnet Hub`.

### Manual

Copy `custom_components/bacnet_hub` to:

`config/custom_components/bacnet_hub`

Then restart Home Assistant.

## Configuration

### Initial setup

Required fields:
- `instance` (BACnet device instance, `0..4194302`)
- `address` (`IPv4[/prefix][:port]`, example: `192.168.31.36/24:47808`)
- `device_name` (BACnet device `objectName`)
- `device_description` (BACnet device `description`)

Defaults:
- `device_name`: `HA-BACnet-Hub`
- `device_description`: `BACnet Hub - Home Assistant Custom Integration`

### Label import model

The integration runs in labels mode and auto-manages mappings from selected labels.

- On setup/options, at least one label must be selected.
- A default label is auto-created if possible:
  - Name: `BACnet`
  - Icon: `mdi:server-network-outline`
  - Color: `light-green`
- Entities can be discovered by direct entity label, by device label, or by labels assigned to the linked area (entity area or device area).

## Home Assistant -> BACnet Mapping

### Automatic object type selection (generic entities)

- Binary-like domains -> `binaryValue`:
  - `binary_sensor`, `switch`, `light`, `lock`, `cover`, `input_boolean`, `alarm_control_panel`, `device_tracker`, `button`
- Numeric/unit-based states -> `analogValue`
- Fallback -> `binaryValue`

### Climate mapping

For `climate.*`, multiple BACnet mappings can be created:

- `hvac_mode`
  - `binaryValue` for simple `off/heat`
  - otherwise `multiStateValue` with dynamic `stateText`
- `hvac_action` -> `binaryValue` (read-only mirror)
- `current_temperature` -> `analogValue`
- `set_temperature` (reads HA attribute `temperature`) -> `analogValue`

### BACnet object support (publisher)

- `analogValue`
- `binaryValue`
- `multiStateValue`

Published mappings are mirrored as observer entities and split by platform:
- `sensor` / `binary_sensor` only (non-interactive read-only observers)
- configuration-like observers use `entity_category=config` (for example climate setpoints/modes)

Note: This avoids confusing UI behavior where toggles/sliders can be clicked but immediately snap back.

## BACnet -> Home Assistant Writeback (for published mappings)

Write requests are accepted only if mapping/service checks pass; otherwise BACnet write is denied (`writeAccessDenied`).

Supported write targets:

- `light`, `switch`, `fan`, `group` -> `turn_on` / `turn_off`
- `cover` -> `open_cover` / `close_cover`
- `number`, `input_number` -> `set_value`
- `climate`:
  - HVAC mode mapping -> `set_hvac_mode`
  - setpoint mapping -> `set_temperature`

## BACnet Client Discovery and Point Import

The integration discovers remote BACnet devices and imports supported points into HA.

### Supported remote BACnet point types

- `analog-input` (`ai`)
- `analog-output` (`ao`)
- `analog-value` (`av`)
- `binary-input` (`bi`)
- `binary-output` (`bo`)
- `binary-value` (`bv`)
- `multi-state-value` (`mv`)
- `characterstring-value` (`csv`)

### Imported platform mapping

- `ai` -> `sensor` (read-only)
- `ao` -> `number` if writable, else `sensor`
- `av` -> `number` (writable)
- `bi` -> `binary_sensor` (read-only)
- `bo` -> `switch` if writable, else `binary_sensor`
- `bv` -> `switch` (writable)
- `mv` -> `select` (writable)
- `csv` -> `text` (writable)

Writable conditions:
- `ao` / `bo` require `priorityArray` support.
- `av`, `bv`, `mv`, `csv` are writable by design.

Implementation detail:
- Imported client point entities are created with `entity_registry_enabled_default = false` (disabled by default until enabled in HA).

## Synchronization Model

- Initial auto-sync runs during setup.
- Mapping refresh is triggered by:
  - `entity_registry_updated`
  - `device_registry_updated`
  - `label_registry_updated`
  - `area_registry_updated`
- Debounce: 2 seconds.
- Stale/orphan published entities are automatically cleaned up.

## Diagnostics

### Hub diagnostics

Provides diagnostic sensors (examples):

- description
- firmware revision
- model name
- object identifier / object name
- system status
- vendor identifier / vendor name
- IP address / subnet mask / MAC address

### Client diagnostics

For discovered clients, diagnostic sensors include similar device and network fields.

## Entity IDs and Unique IDs

Published mirror entities use deterministic IDs based on hub instance + BACnet object instance:

- `sensor.bacnet_doi_<hub_instance>_av_<instance>`
- `binary_sensor.bacnet_doi_<hub_instance>_bv_<instance>`

Client point entities:

- `<platform>.bacnet_doi_<client_instance>_<type_slug>_<object_instance>`

Published unique IDs are stable and hub-scoped:

- `bacnet_hub:hub:<hub_key>:<object-type>:<instance>`

## Services

### `bacnet_hub.reload`

Reload one BACnet Hub config entry.

Fields:
- `entry_id` (optional)
  - If omitted and exactly one BACnet Hub entry exists, that entry is reloaded.

## Limitations

- BACnet/IP focus (`IPv4/prefix:port` bind format).
- Single config entry (`single_config_entry: true`).
- Labels-first auto model; legacy/manual mappings are removed during sync.
- Published `multiStateValue` currently has no dedicated HA mirror platform entity.

## Troubleshooting

- No entities imported:
  - Verify selected labels in options.
  - Verify labels are attached to entity, its device, or the linked area.
- Address bind errors (`address already in use`):
  - Ensure only one BACnet process binds the same IP/port.
- Direct BACnet writes rejected:
  - Check write target is supported and corresponding HA service exists.
- Client points not updating:
  - Confirm remote device supports COV/read for the point.
  - Trigger reload via `bacnet_hub.reload`.

## License

MIT. See `LICENSE`.

Copyright (c) 2025-2026 Alessio Magliarella
