# BACnet Hub for Home Assistant

Connects Home Assistant to a BACnet/IP network in both directions: it publishes
selected Home Assistant entities as objects of a local BACnet device, and it
imports the points of BACnet controllers on the network as Home Assistant
entities.

## What it does

**Home Assistant → BACnet.** Entities you tag with a label are published as
BACnet objects (analog, binary or multi-state values) of the hub's own BACnet
device. A building management system or controller can read them and, for
supported entities, write them back.

**BACnet → Home Assistant.** Controllers found on the network appear as
devices in Home Assistant with their inputs, outputs and values as entities.
Changes arrive event-driven through BACnet COV subscriptions; commandable
points can be written from Home Assistant and released again.

## Requirements

- Home Assistant 2026.3.0 or newer.
- A network connection to the BACnet/IP segment (IPv4). BACnet uses UDP
  port 47808 by default; the hub needs that port on its own IP address.
- One BACnet Hub per Home Assistant instance.

## Installation

### HACS

1. HACS → Integrations → three-dot menu → **Custom repositories**.
2. Add `https://github.com/magliaral/ha-bacnet-hub` with type **Integration**.
3. Download **BACnet Hub** and restart Home Assistant.
4. Settings → Devices & services → **Add integration** → **BACnet Hub**.

### Manual

Copy the folder `custom_components/bacnet_hub` into your configuration
directory as `config/custom_components/bacnet_hub`, restart Home Assistant and
add the integration as above.

## Setup

The setup dialog has two steps.

**Device settings** describe the hub's own BACnet device:

| Field | Meaning | Default |
| --- | --- | --- |
| Instance number | BACnet device instance of the hub (`0`–`4194302`). Must be unique on the network. | `8123` |
| Local BACnet/IP address | IP address the hub binds to, as `IPv4[/prefix][:port]`, for example `192.168.31.36/24:47808`. | detected |
| BACnet Object Name | The device's `objectName`. | `HA-BACnet-Hub` |
| BACnet Device Description | The device's `description`. | `BACnet Hub - Home Assistant Custom Integration` |

**Labels** select what gets published. Pick at least one label; every entity
that carries the label directly, through its device, or through the area the
entity or device is assigned to, is published. A label `BACnet` is created for
you if it does not exist yet.

After setup the hub starts discovering BACnet devices on the network. Every
device appears under Settings → Devices & services → BACnet Hub with its
points as entities, ready to use. Points you do not need can be disabled on
the device page; they stay disabled across restarts.

## Options

Settings → Devices & services → BACnet Hub → **Configure** offers the device
settings above plus:

- **Write priority** (`8`–`16`, default `8`): the BACnet priority used for
  every write to a commandable point of any client device. `8` is *Manual
  Operator* and overrides the controller's own program until the slot is
  released; `16` is the lowest priority. Changing it reloads the integration.
- **Labels**: the labels whose entities are published.
- **Enable verbose bacpypes3 debug logging**: writes every BACnet request,
  response and notification to the Home Assistant log. Only for
  troubleshooting; the log grows quickly.

## Publishing Home Assistant entities to BACnet

Published entities are kept in sync automatically: adding or removing a label,
moving a device to another area or renaming an entity is picked up within a
few seconds, and objects whose entity no longer matches are removed.

### Object type per entity

- `binary_sensor`, `switch`, `light`, `lock`, `cover`, `input_boolean`,
  `alarm_control_panel`, `device_tracker`, `button` → `binaryValue`
- Entities with a numeric state or a unit → `analogValue`
- Everything else → `binaryValue`

### Climate entities

A `climate` entity is published as several objects:

- `hvac_mode` → `binaryValue` for plain off/heat thermostats, otherwise
  `multiStateValue` with the mode names as state text
- `hvac_action` → `binaryValue` (read-only)
- `current_temperature` → `analogValue`
- `set_temperature` (the `temperature` attribute) → `analogValue`

### Mirror entities

Every published object also appears in Home Assistant as a read-only mirror
(`sensor` or `binary_sensor`) so you can see what the BACnet side sees.
Setpoints and modes are marked as configuration entities.

### Writes from the BACnet side

A controller may write a published object when the entity supports it; other
writes are rejected with `writeAccessDenied`.

- `light`, `switch`, `fan`, `group` → turned on or off
- `cover` → opened or closed
- `number`, `input_number` → value set
- `climate` → HVAC mode or target temperature set

## Importing BACnet devices and points

The hub answers and sends `Who-Is` on the local segment and imports the points
of every device that responds. New devices are picked up on the fly; the
network is rescanned every 15 minutes.

### Supported point types

| BACnet object | Home Assistant entity |
| --- | --- |
| `analog-input` | `sensor` |
| `analog-output` | `number` if commandable, otherwise `sensor` |
| `analog-value` | `number` |
| `binary-input` | `binary_sensor` |
| `binary-output` | `switch` if commandable, otherwise `binary_sensor` |
| `binary-value` | `switch` |
| `multi-state-value` | `select` |
| `characterstring-value` | `text` |

Outputs are commandable when the object has a `priorityArray`.

### Live updates

The hub subscribes to change-of-value (COV) notifications for every imported
point, so state changes arrive immediately. Where a device supports it, the
priority array and relinquish default are subscribed as well; on devices that
do not, these two properties are re-read one second after every value change
and polled every 30 seconds, so a change to the priority array that does not
change the value itself can take up to 30 seconds to show.

### Point status attributes

Every imported point carries the BACnet status of its object as state
attributes, kept current through COV:

- `out_of_service`: the object's `outOfService` flag (see the
  `bacnet_hub.set_out_of_service` service below).
- `in_alarm`, `fault`, `overridden`: the object's status flags.
- `reliability` and `event_state`: for example `no-fault-detected` and
  `normal`; re-read whenever a status flag changes.

Attributes the device does not report are omitted. Analog `number` entities
take their minimum, maximum and step from the object's `minPresValue`,
`maxPresValue` and `resolution` when the device provides them.

### Writing and releasing commandable points

Writes from Home Assistant go to the configured **Write priority**. A value
written this way stays in the controller's priority array until it is
released; releasing writes BACnet `Null` to that slot so the controller's own
program takes over again.

Release a point with the `bacnet_hub.release` service or with the bundled tile
feature (see below). Commandable points expose two extra state attributes:

- `priority_array`: all 16 slots, `null` for free ones (not recorded in the
  database).
- `relinquish_default`: the value that applies when no slot is occupied.

## Services

### `bacnet_hub.release`

Releases a priority slot on one or more commandable client points. Target the
points by entity; `priority` is optional and defaults to `8`. Only points with
a priority array are accepted. The point is re-read right after the release,
and with several targets all errors are reported together at the end.

```yaml
service: bacnet_hub.release
target:
  entity_id: switch.bacnet_doi_1031010_bo_1
data:
  priority: 8
```

### `bacnet_hub.set_out_of_service`

Sets the `outOfService` property of one or more points. While a point is out
of service its present value is decoupled from the physical input or output,
so an input can be given a test value and an output no longer drives the
hardware. Target the points by entity; `out_of_service` is required.

```yaml
service: bacnet_hub.set_out_of_service
target:
  entity_id: sensor.bacnet_doi_1031010_ai_0
data:
  out_of_service: true
```

### `bacnet_hub.set_present_value`

Writes the present value of one or more points, including inputs: a BACnet
input accepts a written value only while it is out of service, which is how
a sensor is simulated for testing. Commandable points are written at the
configured write priority, all others without one. `value` is a number for
analog objects, `on`/`off` or `true`/`false` for binary objects, the state
number or state text for multi-state objects, and text for string objects.

```yaml
# Simulate 21.5 V on an analog input
service: bacnet_hub.set_out_of_service
target: {entity_id: sensor.bacnet_doi_1031010_ai_0}
data: {out_of_service: true}
---
service: bacnet_hub.set_present_value
target: {entity_id: sensor.bacnet_doi_1031010_ai_0}
data: {value: 21.5}
```

Note that Developer tools → *Set state* only changes the state inside Home
Assistant and never reaches the device; use this service instead.

### `bacnet_hub.reload`

Reloads the integration. `entry_id` is optional when only one BACnet Hub is
configured.

## Release button on the dashboard

The integration ships a tile feature that adds a **Release** button to a
point's Tile card. No dashboard resource has to be added.

1. Edit or add a **Tile** card for the point, for example
   `switch.bacnet_doi_1031010_bo_1`.
2. Under **Features** choose **BACnet: Release manual override** (German
   frontend: **BACnet: Handbedienung aufheben**). The feature is only offered
   for entities that have a `priority_array` attribute.
3. The priority defaults to `8`; change it in the card's YAML only if you use
   a different write priority.

```yaml
type: tile
entity: switch.bacnet_doi_1031010_bo_1
features:
  - type: toggle
  - type: custom:bacnet-release-feature
    priority: 8
```

The button is enabled while the configured slot is occupied and shown in the
warning color, so an active manual override is easy to spot. Clicking releases
immediately, without a confirmation dialog.

## Entity IDs

Entity IDs are stable and follow the BACnet addressing, which makes them easy
to use in automations:

- Imported client points:
  `<platform>.bacnet_doi_<device instance>_<type>_<object instance>`, for
  example `switch.bacnet_doi_1031010_bo_1` for Binary Output 1 of device
  1031010.
- Mirrors of published objects: `sensor.bacnet_doi_<hub instance>_av_<n>` and
  `binary_sensor.bacnet_doi_<hub instance>_bv_<n>`.

## Diagnostics

The hub device and every discovered controller provide diagnostic sensors
with the BACnet device properties (object name, description, model, firmware,
vendor, system status) and the network settings (IP address, subnet mask, MAC
address).

## Limitations

- BACnet/IP over IPv4 only; no BBMD or foreign device registration.
- One hub per Home Assistant instance.
- Mappings are managed through labels only; there is no manual mapping editor.
- Published `multiStateValue` objects have no mirror entity.
- On devices that do not support COV for the priority array, changes to it
  that leave the value unchanged appear with up to 30 seconds delay.

## Troubleshooting

- **No entities are published:** check that the label is selected in the
  options and attached to the entity, its device or its area.
- **Imported points are missing:** check the device page for disabled
  entities, and make sure the object type is in the list of supported point
  types above.
- **`address already in use` at startup:** another BACnet application on the
  same host uses the port. Change the port in the address field or stop the
  other application.
- **A write from the BACnet side is rejected:** the entity type is not in the
  list of supported write targets above.
- **Client points do not update:** confirm the device supports COV for the
  object, then reload with `bacnet_hub.reload`. For a closer look, enable the
  bacpypes3 debug option, reproduce the problem, and disable it again.

## Upgrading from 1.x

- The per-point **Release** button entities were removed. Automations that
  pressed them call `bacnet_hub.release` with the point as target instead;
  dashboards use the tile feature described above.
- The per-device **Write priority** select entities were removed. The priority
  is now a single option in the hub's device settings.
- Stale registry entries of both are cleaned up automatically at the next
  start of the integration.

## License

MIT. See `LICENSE`.

Copyright (c) 2025-2026 Alessio Magliarella
