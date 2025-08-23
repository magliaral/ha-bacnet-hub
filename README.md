# BACnet Hub for Home Assistant

Publishes Home Assistant entities as BACnet objects and supports bidirectional writes (ReadProperty/WriteProperty).  
Built with **bacpypes3**.

> This acts as a **local BACnet/IP device** inside Home Assistant. You “publish” existing HA entities as BACnet objects (e.g., `analogValue`, `binaryValue`) so third-party BACnet clients can read them — and, if enabled, write back into HA.

---

## Features

- **Local BACnet/IP device**
  - Configurable **Device ID** and **bind address** (`IPv4/prefix:port`, default `:47808`)
- **Entity ↔ BACnet object mapping** in the **Options flow**
  - Choose an HA entity, mark it **writable** or read-only
  - Auto-detect **object type** (`analogValue` vs `binaryValue`)
  - Auto-assign **instance numbers** per type (simple counters)
  - Capture the entity’s **friendly name**
  - For `analogValue`, the **unit** is carried over when available
- **Live mirroring**
  - **HA → BACnet**: source state updates `presentValue`
  - **BACnet → HA**: writeback of `presentValue` (optional), mapped to HA services (see below)
- **Home Assistant platforms**
  - `sensor` for `analogValue` (mirrors device/state class & unit where possible)
  - `binary_sensor` for `binaryValue`
- **Options UI**
  - Add / edit / delete published objects at any time
  - Device settings (Device ID + bind address) adjustable after setup
- **Maintenance**
  - Service: `bacnet_hub.reload` to reload a single entry

---

## Installation

### HACS (custom repository)

1. HACS → **Integrations** → ••• → **Custom repositories**  
   Add this repo as type **Integration**.
2. Install **BACnet Hub**.
3. Restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration → BACnet Hub** and complete the dialog.

> HACS expects a `manifest.json` with standard keys (domain, version, …). See HACS docs if needed. :contentReference[oaicite:2]{index=2}

### Manual

Copy `custom_components/bacnet_hub` into your `config/custom_components/` folder, then restart Home Assistant.

---

## Configuration

### Initial setup

- **Device ID** (BACnet Device Instance)
- **Bind address** as `IPv4/prefix:port` (e.g., `192.168.1.10/24:47808`). A sane default is suggested.
- After the first setup succeeds, open **Options** to publish objects.

### Publishing objects (Options → Publish)

**Add** a mapping by selecting:

- **Entity**: any existing HA entity (e.g., `sensor.living_temp`, `light.kitchen`)
- **Writable**:  
  - **off** → read-only BACnet point (typ. Monitoring)  
  - **on** → BACnet writes are **applied back** into HA

Under the hood:

- **Type detection**
  - Binary-ish domains (`binary_sensor`, `switch`, `light`, `cover`, `lock`, `input_boolean`, …) → `binaryValue`
  - Otherwise, if the entity has a **unit** or a numeric state → `analogValue`
  - Else fallback → `binaryValue`
- **Instance numbers** increment per BACnet type (`av`, `bv`).
- The entity’s **friendly name** is stored for display in the list.
- For `analogValue`, the **unit** is taken from the source entity when present.

You can later **edit** or **delete** mappings; editing keeps the existing instance number.

---

## Runtime behavior

### HA → BACnet (state mirroring)

- Each published object is created as a **BACpypes3 local object**:
  - `objectIdentifier`: (`analogValue`|`binaryValue`, *instance*)
  - `objectName`: set to the **HA entity_id**
  - `description`: set to the **friendly name**
  - `units` (for `analogValue`): when resolvable from HA’s unit
- The component listens to HA’s state changes and **updates `presentValue`** accordingly.

### BACnet → HA (writeback)

If **writable = true**, writes to `presentValue` are translated to HA service calls:

- `binaryValue`:
  - HA **domain-aware** mapping:
    - `light` → `light.turn_on` / `light.turn_off`
    - `switch` / `fan` → `*.turn_on` / `*.turn_off`
    - `cover` → `cover.open_cover` / `cover.close_cover`
- `analogValue`:
  - For `number` / `input_number`: `*.set_value` with the written float

> Writes to read-only mappings are ignored.

### Home Assistant entities created

- For `analogValue` → **`sensor` entity**
  - Mirrors `device_class`, `state_class`, `unit` where possible
- For `binaryValue` → **`binary_sensor` entity**
  - Mirrors `device_class` where possible; if source has no icon and **domain is `light`**, show `mdi:lightbulb` / `mdi:lightbulb-outline` depending on state
- All entities are attached to a single **device** called *“BACnet Hub (Local Device)”*.

---

## Tips & Troubleshooting

- **UDP 47808** must be reachable; avoid port clashes with other BACnet services.
- BACnet/IP typically expects both peers to be on the **same L2 segment** (unless you route BACnet/BBMD yourself).
- Some BACnet browsers (e.g., YABE) can lock comms if they continuously poll; close them while testing if you get timeouts.
- If a source entity has **no state** yet, the mirrored object may start with `None`/default values until the first update arrives.
- Use the service **`bacnet_hub.reload`** to cleanly restart the entry after changing options.

---

## Roadmap

- COV subscriptions
- More complete **Engineering Units** coverage / overrides
- 
---

## Development

- Python 3.11+ recommended (align with your HA environment)
- Uses **bacpypes3**. See docs & samples for deeper BACnet/stack details. :contentReference[oaicite:3]{index=3}

Dev hints:

- Entities expose `unique_id`s and are grouped under the integration device.
- `presentValue` mirroring uses a lightweight change hook; an optional periodic watcher is included as a fallback.

---

## License

MIT — see [LICENSE](LICENSE).

© 2025 Alessio Magliarella
