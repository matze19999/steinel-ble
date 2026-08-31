<p align="center">
  <img src="https://raw.githubusercontent.com/matze19999/steinel-ble/main/custom_components/steinel_ble/brand/logo.png" alt="STEINEL" height="80">
</p>

<h1 align="center">STEINEL Connect BLE for Home Assistant</h1>

<p align="center">
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg" alt="HACS Custom"></a>
  <a href="https://github.com/matze19999/steinel-ble/actions/workflows/validate.yml"><img src="https://github.com/matze19999/steinel-ble/actions/workflows/validate.yml/badge.svg" alt="Validate"></a>
  <a href="https://github.com/matze19999/steinel-ble/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
</p>

A Home Assistant custom integration for local control of **STEINEL Connect**
Bluetooth Mesh devices, including the L 845 C. It supports discovery,
provisioning, on/off and brightness control, and automatically exposes colour
temperature, colour and sensor features when the device implements the
required models.

Communication works through Home Assistant Bluetooth adapters and **ESPHome
Bluetooth proxies**. No cloud account or STEINEL gateway is required.

The protocol was independently reverse-engineered from the official STEINEL
Connect Android app and validated against real hardware. This project is not
affiliated with or endorsed by STEINEL and does not use an official STEINEL
SDK. See [Legal](#legal).

## Features

- **Bluetooth discovery** - devices advertising the Bluetooth Mesh
  Provisioning or Proxy service are discovered automatically by Home
  Assistant.
- **PB-GATT provisioning** - an unprovisioned lamp receives a randomly
  generated NetKey, AppKey and Device Key owned by its Home Assistant config
  entry.
- **Automatic recovery from another mesh** - if a lamp is already provisioned
  into an unknown Bluetooth Mesh network, the integration attempts STEINEL's
  proprietary Global Reset and then provisions the device without requiring
  the old mesh keys.
- **Adaptive model support** - Composition Data is read after provisioning and
  the AppKey is bound only to models actually present on the device. Supported
  models include Generic OnOff, Light Lightness, Light LC, Light CTL and Light
  HSL.
- **Standard Home Assistant light entities** - on/off and brightness are
  available where supported. Colour temperature and HS colour are exposed
  automatically for compatible lamps.
- **Remembered brightness** - turning a lamp off and back on restores the last
  brightness acknowledged by Home Assistant, including devices that otherwise
  return to their configured 100% OnPowerUp level. Acknowledged light state is
  retained across Home Assistant and integration restarts.
- **Optional sensor entities** - devices with the STEINEL Sensor Extension
  model can expose presence, motion, people count, temperature, humidity, CO₂,
  VOC, noise, air pressure, dew point, and time-since-motion/presence values.
  Unsupported properties remain unavailable. All properties of a device are
  refreshed by one shared polling cycle so they do not establish competing BLE
  sessions; light commands take priority over background polling.
- **Bluetooth proxy support** - connections use Home Assistant's Bluetooth
  stack and
  [`bleak-retry-connector`](https://github.com/Bluetooth-Devices/bleak-retry-connector),
  so an ESPHome proxy such as the XIAO ESP32S3 can provide the BLE connection.
- **Acknowledged state updates** - acknowledged Bluetooth Mesh status messages
  update the entity state in Home Assistant.
- **Initial state retrieval** - after connecting, the integration queries the
  on/off, lightness, CTL and HSL states supported by the element, avoiding an
  `unknown` light state when the device responds.
- **Reachability tracking** - every configured device has a Bluetooth
  `device_tracker` that reports `home` while its Mesh Proxy connection is
  usable or the device has advertised recently. This remains meaningful when
  idle disconnect is enabled and no permanent GATT connection is held.
- **Advertised device information** - product ID, firmware, hardware revision,
  bootloader and device hash are read passively from manufacturer data where
  the firmware advertises them.
- **Proxy connection queue** - connection establishment is serialized across
  STEINEL entries, avoiding simultaneous connection attempts that can exhaust
  a small ESPHome proxy's BLE slots.
- **Guided reset repair** - a failed foreign-mesh reset creates an actionable
  Home Assistant repair that explains the power-cycle timing and retries setup
  immediately after confirmation.
- **Diagnostics and configurable behaviour** - credential-free diagnostics,
  background reconnection, command timeouts, retry counts, sensor polling and
  brightness restoration can be managed from the integration options.

## Installation

This repository is not currently part of the default HACS store. Add it as a
HACS custom repository or install it manually.

### HACS (recommended)

[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=matze19999&repository=steinel-ble&category=integration)

The badge requires [HACS](https://hacs.xyz) and the *My Home Assistant*
integration. Alternatively:

1. Open HACS and select the **⋮** menu in the top-right corner.
2. Select *Custom repositories*.
3. Add `https://github.com/matze19999/steinel-ble` as type *Integration*.
4. Find and install **STEINEL Connect Bluetooth Mesh**.
5. Restart Home Assistant.

The integration is then available under *Settings → Devices & services → Add
integration → STEINEL Connect Bluetooth Mesh*. Home Assistant may also offer
it automatically after discovering a compatible device.

### Manual

Copy `custom_components/steinel_ble` into the `custom_components` directory of
your Home Assistant configuration and restart Home Assistant.

## Requirements

- Home Assistant 2026.8 or newer
- A connectable local Bluetooth adapter or ESPHome Bluetooth proxy in range
- A compatible STEINEL Connect Bluetooth Mesh device

If the device requires a 16-byte Static OOB key, enter its 32 hexadecimal
characters during setup. Otherwise leave the field empty to use No OOB
provisioning.

## Setting up a device

1. Power the STEINEL device and keep it within reliable Bluetooth range of the
   adapter or proxy.
2. Open *Settings → Devices & services*. Select the discovered device, or use
   *Add integration → STEINEL Connect Bluetooth Mesh* and enter its Bluetooth
   address.
3. Confirm the device and optionally enter its Static OOB key.
4. The integration provisions the device, reads its Composition Data, adds the
   AppKey, binds its supported models and creates the applicable entities.

Each configured lamp stores its own Bluetooth Mesh credentials in its Home
Assistant config entry. Include Home Assistant's `.storage` configuration in
backups: losing the NetKey, AppKey or Device Key also loses authenticated
access to the provisioned device.

### Device already belongs to another mesh

The integration can reset a provisioned STEINEL device without knowing its
existing mesh keys. The lamp only accepts this command during a short window
after mains power is applied, and it must not already be connected to another
Bluetooth client.

When Home Assistant discovers a provisioned device, the setup dialog displays
the required preparation steps:

1. Completely close the STEINEL app and any other app that may connect to the
   lamp. Merely leaving the app in the background may not be sufficient.
2. Briefly disconnect the lamp from mains power.
3. Restore power and submit the Home Assistant setup form immediately.
4. Home Assistant attempts the Global Reset, waits for the Mesh Provisioning
   service and then continues setup automatically.

If the reset window was missed, remove or reload the failed integration entry
and repeat the power-cycle procedure. Do not open the STEINEL app during this
process.

## Options and diagnostics

Open *Settings → Devices & services → STEINEL Connect Bluetooth Mesh*, select
the device and choose *Configure* to adjust:

- BLE connection and Provisioning Invite attempts;
- the acknowledged Mesh command timeout;
- the sensor polling interval;
- remembered-brightness restoration and its delay; and
- optional release of an idle BLE connection and the idle delay; and
- the time after which a missing Bluetooth advertisement marks the device as
  unreachable; and
- a one-time rescan of Composition Data and sensor properties.

Changing an option reloads only that device. Persistent BLE connections remain
the default and provide the fastest response. Enabling idle disconnect frees a
proxy connection slot after the configured period, but the next command must
first reconnect and therefore responds more slowly. In testing with a XIAO
ESP32S3 Bluetooth proxy and an L 845 C, a command after an idle disconnect took
approximately 12 seconds. The actual delay depends on signal quality, proxy
load and the STEINEL device. Unexpected connection losses are handled in the
background with exponential backoff.

Home Assistant's device diagnostics download includes model composition,
detected sensor properties, active and passive reachability, last
advertisement, connection state, reconnect count, Bluetooth source and RSSI.
NetKey, AppKey, Device Key and Static OOB values are always redacted.

## How it works

After provisioning, the integration:

1. reconnects through the best connectable Bluetooth path;
2. configures the Mesh Proxy filter for the node;
3. reads the node's Composition Data;
4. adds the application key;
5. binds supported Generic, Light and known STEINEL vendor models; and
6. creates entities only for applicable elements and model families.

Both outgoing and incoming segmented lower-transport messages are supported.
Normal control uses acknowledged access messages so returned device states can
be reflected in Home Assistant.

## Troubleshooting

Enable debug logging when investigating a connection, reset or provisioning
problem:

```yaml
logger:
  logs:
    custom_components.steinel_ble: debug
```

Bluetooth Mesh Provisioning and Proxy advertisements can take time to change
after provisioning or reset. The integration clears a stale GATT service cache
where supported and retries setup automatically.

For a failed automatic reset, first verify that:

- the STEINEL mobile app is fully closed;
- no phone or other Bluetooth client is connected to the lamp;
- the lamp was power-cycled immediately before setup was confirmed;
- the Bluetooth proxy has a free connection slot and a reliable signal; and
- the device is still visible to Home Assistant as connectable.

## Limitations

- Automatic Global Reset depends on a short device-specific startup window and
  may require repeating the power-cycle procedure.
- The L 845 C is the primary hardware used for real-device validation. Other
  devices are detected by their advertised services and model composition, but
  not every STEINEL product and firmware version has been tested.
- Sensor support depends on STEINEL's vendor Sensor Extension model. Entities
  for properties not implemented by a device remain unavailable.
- Scene management, Light LC parameter configuration, firmware updates and
  Bluetooth Mesh BLOB/DFU are not currently exposed.
- There is no import workflow for an existing mesh. A device is either
  provisioned as new or reset from its previous network during setup.

## Development

Run the local checks with:

```bash
ruff check custom_components tests
ruff format --check custom_components tests
pytest
```

## Contributing

Issues and pull requests are welcome. Reports from STEINEL Connect products
other than the L 845 C are particularly useful. Please include the model,
firmware version, discovered Composition Data and relevant debug logs where
possible, but never publish mesh keys or other credentials.

## Legal

This is an independent, community-built integration. It is **not** affiliated
with, endorsed by, or sponsored by STEINEL Vertrieb GmbH or any of its
affiliates. "STEINEL" and the STEINEL logo are trademarks of their respective
owner and are used solely to identify the products with which this integration
interoperates.

No STEINEL software, firmware or copyrighted application assets are included
in this repository. The protocol implementation is independent work based on
reverse-engineering for interoperability purposes.

Licensed under the [MIT License](https://github.com/matze19999/steinel-ble/blob/main/LICENSE).
