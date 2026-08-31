# Changelog

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
