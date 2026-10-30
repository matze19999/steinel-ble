# Changelog

## 0.8.0

- Merge the startup fix from #5: reconnect, sensor polling and timers run as
  config-entry background tasks and are cancelled on unload, without blocking
  Home Assistant bootstrap (#2).
- Replace the deprecated device-registry lookup with an entry-scoped lookup.

- Support the L 810 SC's separate standard motion and illuminance elements,
  unfiltered lux reads, fast background motion polling, and Sensor Status
  forwarding to its native Light LC controller.
- Add L 810 sensor range/sensitivity and operating-profile controls, fade/delay
  settings, and recovery of a confirmed daylight threshold when firmware
  reports its placeholder value.
- Reserve L 810 Mesh sequence blocks on disk before use, retaining upstream
  beacon/IV handling and imported model bindings.
- Confirm operating-profile and sensor-setting writes before displaying them,
  preserve both motion zones during edits, and localize the new controls.
- Keep automatic threshold recovery disabled for imported devices and maintain
  the IV synchronization boundary when using reserved sequence numbers.

## 0.7.1

- Decode Sensor Extension Format A/B headers correctly so no-motion reports
  clear motion entities instead of leaving them on Detected.
- Match sensor replies by element, opcode and property, handle unknown values,
  and add raw sensor values to diagnostics.
- Fix one-time model and sensor rescans when no other options change.

## 0.7.0

- Expose the existing Light LC settings when the controller and occupancy
  sensors are on different elements of the same lamp, as on the L40 SC.
  Motion or presence detection is sufficient; a separate lux reading is not
  required. Plain lamps without detected occupancy sensing remain excluded.
- Add localized configuration entities for main/basic brightness and duration,
  the light threshold, automatic light control and motion automation. Property
  controls require a bound LC Setup Server; setup only reads existing settings.
- Serialize configuration requests and validate the echoed Property ID so
  concurrent initialization cannot assign one property's reply to another.
- Decode full-scale lightness as 100%, retain acknowledged values after edits,
  and update configuration entities from live status messages and connectivity.
- Include received LC properties and modes in device diagnostics.

## 0.6.3

- Synchronize imported Mesh IV state from authenticated Secure Network and
  Mesh Private beacons before configuring the proxy or sending commands.
- Install the Mesh notification handler before subscribing so an immediate
  initial beacon is not lost. Persist verified IV state without resetting the
  source address or sequence number, and reject stale IV state.
- Handle the IV Update flag: send with the previous IV Index while an update
  is active and decrypt current/previous-index responses with their own nonce.
- Report a clear connection error when traffic has a mismatching IVI and no
  authenticated beacon can be received, instead of running all sensor probes.
- Include verified beacon and transmit-IV state in device diagnostics.

## 0.6.2

- Serialize complete GATT Proxy PDUs so simultaneous access messages and
  transport acknowledgements cannot interleave their SAR fragments.
- Add a concurrency regression test and verify network encryption and
  decryption against an official Bluetooth Mesh wire-format test vector.

## 0.6.1

- Keep sensors and other entities available when a light command's status reply
  times out while the BLE connection remains active. Report the failed command
  as a Home Assistant error instead of an unhandled timeout.
- Wake background reconnection after transport errors and replace a failed
  connection before accepting the next command.
- Name secondary LC-only elements as light controls with their Mesh address,
  and avoid restoring unsupported Light Lightness values on these elements.
- Add credential-free Mesh receive counters to diagnostics, distinguishing
  other NetKeys, IVI mismatches, failed authentication and accepted messages.

## 0.6.0

- Declare all test dependencies for consistent local and CI runs, including
  Voluptuous for configuration-flow tests.
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
