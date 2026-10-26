# Changelog

## 0.6.0

- Import already provisioned devices from a STEINEL Connect Mesh JSON export.
  Existing keys, unicast addresses and model bindings are reused without
  resetting or provisioning the lamp, preserving native groups and settings.
- Select the exported device and its bound AppKey, with a separate Home
  Assistant source address and explicit IV Index and sequence state.
- Keep imported model bindings unchanged during setup, reload and model rescans.
- Expose secondary Light LC elements from imported compositions as lights.

## 0.5.0

- Fixed STEINEL Sensor Extension replies whose echoed Property ID was
  previously interpreted as the motion value.
- Exposed motion as a binary motion sensor and added ambient illuminance.
- Added unsolicited Sensor Extension status processing for faster motion and
  presence updates.
- Added Light LC controls for motion automation, main light, basic light,
  timing and the light threshold.

## 0.3.0

- Query on/off, lightness, colour temperature and HSL state immediately after
  connecting so light entities no longer start as `unknown` when the device
  answers its supported model status requests.
- Added an optional idle-disconnect mode that releases scarce Bluetooth proxy
  slots after a configurable delay. Persistent connections remain the default
  for the fastest command response.

## 0.2.2

- Use a registry-backed tracker entity so reachability appears directly on the
  corresponding STEINEL device page in Home Assistant.

## 0.2.1

- Added a Bluetooth `device_tracker` entity for every configured STEINEL
  device. It reports `home` while its Mesh Proxy connection is usable and
  `not_home` while the device is unreachable or reconnecting.

## 0.2.0

- Added automatic background reconnection with exponential backoff.
- Made provisioning retries, command timeouts, sensor polling and brightness
  restoration configurable.
- Added repeated Provisioning Invite handling and reset repair guidance.
- Added credential-redacted Home Assistant diagnostics.
- Detect supported STEINEL sensor properties before creating entities.
- Expanded protocol, composition, segmentation and error-handling tests.
- Added GitHub validation and restored Home Assistant brand assets.

## 0.1.0

- Initial Bluetooth Mesh provisioning, proxy, light and sensor support.
