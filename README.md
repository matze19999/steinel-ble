<p align="center">
  <img src="custom_components/steinel_ble/brand/logo.png" alt="STEINEL" height="80">
</p>

<h1 align="center">STEINEL Connect BLE for Home Assistant</h1>

<p align="center">
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg" alt="HACS Custom"></a>
  <a href="https://github.com/matze19999/steinel-ble/actions/workflows/validate.yml"><img src="https://github.com/matze19999/steinel-ble/actions/workflows/validate.yml/badge.svg" alt="Validate"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
</p>

A Home Assistant custom integration for **STEINEL Connect** Bluetooth Mesh
lamps (e.g. the L 845 C) - discovery, setup/provisioning, on/off, brightness
(plus colour temperature/colour where the lamp supports it), firmware
updates and a physical identify button, all over **Home Assistant Bluetooth
proxies** as well as local adapters.

The protocol this integration speaks was reverse-engineered from the
official STEINEL Connect Android app and validated against real hardware
(a STEINEL L 845 C). It is not affiliated with, endorsed by, or built from
any official STEINEL SDK - see [Legal](#legal) below.

## Features

- **Discovery** - lamps advertising STEINEL's Bluetooth manufacturer id are
  found automatically by Home Assistant and offered for setup under
  *Settings → Devices & services*.
- **Setup / provisioning** - a factory-reset lamp is provisioned into a
  Home-Assistant-owned Bluetooth Mesh network (a fresh random NetKey/AppKey
  is generated on first use). The shared AppKey is then bound to whichever
  light models the lamp actually supports (Generic OnOff, Light Lightness,
  Light CTL, Light HSL are all attempted; only the ones that succeed are
  exposed), so the integration adapts to what a given lamp can actually do.
- **On/off and brightness** - exposed as a standard `light` entity. Colour
  temperature (Light CTL) or HS colour (Light HSL) are exposed too,
  automatically, wherever those models bound successfully.
- **Firmware update** - an `update` entity per lamp using the fully
  validated Nordic Secure DFU path (see [Firmware update](#firmware-update)
  for why this needs a small one-time manual step rather than being fully
  automatic).
- **Identify** - a button entity that blinks the lamp (proprietary GATT
  opcode `0x17`), also used automatically while confirming a device during
  setup.
- **Factory reset** - a per-lamp button entity (proprietary GATT opcode
  `0xE5`) that erases the lamp's Bluetooth Mesh keys and removes it from
  Home Assistant. Disabled by default (enable it explicitly on the entity
  first) since there is no built-in per-press confirmation dialog for
  button entities in Home Assistant.
- **Works over Bluetooth proxies** - all Bluetooth communication goes
  through Home Assistant's `bluetooth` integration and
  [`bleak-retry-connector`](https://github.com/Bluetooth-Devices/bleak-retry-connector),
  the same mechanism every other HA Bluetooth integration uses, and reuses
  a lamp's connection for a short idle window so consecutive commands don't
  each pay the full BLE connect cost.

## Installation

### HACS (recommended)

1. HACS → the "⋮" menu → *Custom repositories*.
2. Add `https://github.com/matze19999/steinel-ble` as an *Integration*.
3. Install "STEINEL Connect BLE", then restart Home Assistant.

### Manual

Copy `custom_components/steinel_ble` from this repository into your Home
Assistant configuration's `custom_components` directory, then restart Home
Assistant.

## Setting up

1. *Settings → Devices & services*. A factory-reset/unprovisioned STEINEL
   lamp in range is usually offered automatically ("Discovered"); otherwise
   use *Add integration → STEINEL Connect BLE → Discover a new
   (factory-reset) lamp*.
2. The lamp blinks (identify) while you confirm its name.
3. It is provisioned into a new, Home-Assistant-owned mesh network and its
   light models are bound automatically.
4. For every further lamp, use the integration's *Configure → Add a
   device* (there is only ever one hub/mesh network per Home Assistant
   instance; further lamps join it rather than creating new integration
   entries).

If you already have lamps provisioned into a Bluetooth Mesh network from
another tool, use *Configure → Import an existing mesh* and paste its
NetKey/AppKey/node JSON instead of provisioning from scratch.

> **Note:** only one provisioner should actively send on a given mesh
> NetKey's source address at a time. If another tool/app still manages the
> same mesh, stop using it against the same network once imported - two
> senders reusing the same source address can produce duplicate sequence
> numbers, which nodes silently drop as a replay.

## Firmware update

STEINEL's online firmware catalog links firmware records to an internal
product UUID, and there is no confirmed way to resolve the numeric product
id a lamp advertises over BLE to that UUID without an undocumented,
unpublished companion endpoint. Rather than guess at that mapping for
something as consequential as a DFU firmware flash, each lamp's firmware
source is configured explicitly instead: *Settings → Devices & services →
STEINEL Connect BLE → Configure → Configure a firmware update source*, then
provide:

- the package URL (a direct `https://` download) or a local file path,
- the expected version (`MAJOR.MINOR.PATCH`),
- the expected hardware revision and product id (both visible in the
  device's Bluetooth advertisement),
- optionally the expected SHA-256, checked before anything is flashed.

The `update` entity then behaves like any other: it shows "update
available" once the installed version (read passively from the lamp's own
advertisement) differs from the configured target, and pressing *Install*
downloads, verifies, and applies the update through the Nordic Secure DFU
protocol the lamp's bootloader speaks.

## Limitations

- One STEINEL mesh hub per Home Assistant instance. If you also use the
  official STEINEL Connect app, keep it on a separate mesh network - a lamp
  can only belong to one Bluetooth Mesh NetKey at a time, and moving it to
  a different network requires a factory reset.
- Light LC (occupancy/daylight sensing configuration), STEINEL's vendor
  Light-LC/Sensor model extensions, Scenes and Mesh BLOB/DFU firmware
  updates are not exposed as entities yet.
- Only unsegmented Generic/Light Mesh Access messages are used for normal
  operation; the Config messages that need segmentation (AppKey Add, Model
  App Bind) are supported during setup.
- Global Reset and firmware flashing are deliberately conservative: no
  automatic online firmware catalog lookup (see above), and the factory
  reset button is disabled by default.

## Contributing

Issues and pull requests are welcome - in particular, real-world reports
from other STEINEL Connect products (not just the L 845 C this was
developed against) are very helpful, since lamp capabilities are
auto-detected but the underlying Mesh/vendor model behaviour has only been
validated against one product line so far.

## Legal

This is an independent, community-built integration. It is **not**
affiliated with, endorsed by, or sponsored by STEINEL Vertrieb GmbH or any
of its affiliates. "STEINEL" and the STEINEL logo are trademarks of their
respective owner and are used in this repository (`custom_components/steinel_ble/brand/`)
solely to identify the product this integration interoperates with, for
Home Assistant's brand display - see Home Assistant's
[brands repository image specification](https://github.com/home-assistant/brands#image-specification)
for the same policy applied to every third-party integration in Home
Assistant. No STEINEL software, firmware or copyrighted application assets
are included in this repository; the protocol implementation is this
project's own, independent work based on reverse-engineering for
interoperability purposes.

Licensed under the [MIT License](LICENSE).
