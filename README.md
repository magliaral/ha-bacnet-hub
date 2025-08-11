# BACnet Hub for Home Assistant

Publishes Home Assistant entities as BACnet objects and supports bidirectional writes (ReadProperty/WriteProperty).
Built with **bacpypes3**.

## Features (MVP)
- BACnet/IP device with configurable Device ID, bind address, port
- Map HA entities to BACnet objects (analogValue, binaryValue, ...)
- Read from HA, write back to HA (service mapping)
- Options Flow to add/remove objects after setup

> Roadmap: COV subscriptions, engineering units, priority array, MS/TP via router, tests.

## Installation (HACS custom repository)
1. Go to **HACS → Integrations → … → Custom repositories** and add this repo as type *Integration*.
2. Install **BACnet Hub**.
3. Restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration → BACnet Hub** and follow the setup.

### Manual install
Copy the folder `custom_components/bacnet_hub` into your `config/custom_components/` directory and restart Home Assistant.

## Configuration
After initial setup, open **Options** to add BACnet objects.
Example (conceptual) object mapping:
```yaml
objects:
  - entity_id: sensor.living_temp
    object_type: analogValue
    instance: 1001
    units: degreesCelsius
    writable: false
    mode: state
  - entity_id: switch.living_light
    object_type: binaryValue
    instance: 2001
    writable: true
    mode: state
    write:
      service: switch.turn_on_off
```

## Development
- Requires Python 3.11+ (matching HA dev env)
- The integration declares `bacpypes3` as a requirement in `manifest.json`.

## License
MIT — see [LICENSE](LICENSE).

---
© 2025 Alessio Magliarella


### Note on bacpypes3 versions
- Tested against bacpypes3 ~> 0.0.92 (APIs may evolve). If you see import/runtime errors,
  please open an issue with your exact bacpypes3 version and logs.
