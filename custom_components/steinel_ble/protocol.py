"""STEINEL BLE / Bluetooth Mesh wire protocol.

Pure encode/decode/crypto logic, with no dependency on any particular BLE
stack. Connection handling lives in :mod:`.ble`. This module is a port of
the protocol core validated by the standalone ``steinel_ble.py`` CLI tool
(see ``STEINEL_BLE_KOMMUNIKATION.md`` for the reverse-engineering notes and
``STEINEL_BLE_TOOL.md`` for the CLI that exercised it against real
hardware). Unlike the CLI's ``MeshConfig``, sequence numbers are *not*
pulled from mutable state inside this module: callers reserve and persist a
sequence number first (see ``mesh_store.py``) and pass it in explicitly, so
this module has no hidden IO and is safe to use from async code.
"""

from __future__ import annotations

import dataclasses
import struct
from typing import Any

from .const import STEINEL_COMPANY_ID

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class ProtocolError(RuntimeError):
    """Raised for any malformed frame, PDU or unexpected protocol state."""


def hexstr(data: bytes) -> str:
    return data.hex(" ").upper()


def checked_u8(value: int, name: str = "value") -> int:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must be between 0 and 255")
    return value


def checked_u16(value: int, name: str = "value") -> int:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{name} must be between 0 and 65535")
    return value


# ---------------------------------------------------------------------------
# Proprietary STEINEL direct GATT channel: CRC16 / COBS / framing
# ---------------------------------------------------------------------------

STATUS_NAMES = {
    0x00: "OK",
    0x01: "INVALID_COMMAND",
    0x02: "INVALID_PARAMETER",
    0x03: "INVALID_VALUE",
    0x04: "INVALID_AUTHORIZATION",
    0x05: "INACTIVE_KNX_PARAMETERS",
    0x06: "INVALID_TARGET",
    0x07: "INVALID_COMMAND_LENGTH",
    0x08: "INVALID_OPERATION_ID",
    0xFF: "ERROR",
}

FIRMWARE_STATES = {
    0x00: "INITIALIZATION",
    0x01: "IDLE",
    0x02: "WAIT_UPDATE_PROPERTIES",
    0x03: "WAIT_UPDATE_DATA",
    0x04: "STORE_DATA",
    0x05: "VALIDATE_STORED_DATA",
    0x06: "INVALID_IMAGE_IN_FLASH",
    0x07: "VALID_IMAGE_IN_FLASH",
    0x08: "UPDATE_BUSY",
    0x09: "ERROR",
    0x0A: "ERASING_EXTERNAL_MEMORY",
    0x0B: "COMMUNICATION_TIMEOUT",
    0x0C: "DISCONNECTED",
    0x0D: "FIRMWARE_UPDATE_COMPLETED",
}

CONFIG_STATUS_NAMES = {
    0x00: "SUCCESS",
    0x01: "INVALID_ADDRESS",
    0x02: "INVALID_MODEL",
    0x03: "INVALID_APPKEY_INDEX",
    0x04: "INVALID_NETKEY_INDEX",
    0x05: "INSUFFICIENT_RESOURCES",
    0x06: "KEY_INDEX_ALREADY_STORED",
    0x07: "INVALID_PUBLISH_PARAMETERS",
    0x08: "NOT_A_SUBSCRIBE_MODEL",
    0x09: "STORAGE_FAILURE",
    0x0A: "FEATURE_NOT_SUPPORTED",
    0x0B: "CANNOT_UPDATE",
    0x0C: "CANNOT_REMOVE",
    0x0D: "CANNOT_BIND",
    0x0E: "TEMPORARILY_UNABLE",
    0x0F: "CANNOT_SET",
    0x10: "UNSPECIFIED_ERROR",
    0x11: "INVALID_BINDING",
}

PROVISIONING_FAILURES = {
    0x01: "PROHIBITED",
    0x02: "INVALID_PDU",
    0x03: "INVALID_FORMAT",
    0x04: "UNEXPECTED_PDU",
    0x05: "CONFIRMATION_FAILED",
    0x06: "OUT_OF_RESOURCES",
    0x07: "DECRYPTION_FAILED",
    0x08: "UNEXPECTED_ERROR",
    0x09: "CANNOT_ASSIGN_ADDRESSES",
}


def crc16_steinel(data: bytes) -> int:
    """CRC-16/SPI-FUJITSU (a.k.a. CRC-16/AUG-CCITT), poly 0x1021, init 0x1D0F."""
    crc = 0x1D0F
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    out = bytearray([0])
    code_index = 0
    code = 1
    for byte in data:
        if byte == 0:
            out[code_index] = code
            code_index = len(out)
            out.append(0)
            code = 1
        else:
            out.append(byte)
            code += 1
            if code == 0xFF:
                out[code_index] = code
                code_index = len(out)
                out.append(0)
                code = 1
    out[code_index] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    index = 0
    while index < len(data):
        code = data[index]
        if code == 0:
            raise ProtocolError("zero byte inside COBS payload")
        index += 1
        end = index + code - 1
        if end > len(data):
            raise ProtocolError("truncated COBS block")
        out.extend(data[index:end])
        index = end
        if code != 0xFF and index < len(data):
            out.append(0)
    return bytes(out)


@dataclasses.dataclass(frozen=True)
class DirectPacket:
    opcode: int
    payload: bytes


@dataclasses.dataclass(frozen=True)
class DirectResponse:
    opcode: int
    status: int
    data: bytes

    def require_ok(self) -> DirectResponse:
        if self.status != 0:
            raise CommandError(self.opcode, self.status, self.data)
        return self


class CommandError(ProtocolError):
    def __init__(self, opcode: int, status: int, data: bytes = b"") -> None:
        self.opcode = opcode
        self.status = status
        self.data = data
        super().__init__(
            f"command 0x{opcode:02X}: {STATUS_NAMES.get(status, f'UNKNOWN_0x{status:02X}')}"
        )


def encode_direct_frame(opcode: int, data: bytes = b"") -> bytes:
    checked_u8(opcode, "opcode")
    body = bytes([opcode]) + data
    raw = body + crc16_steinel(body).to_bytes(2, "little")
    return cobs_encode(raw) + b"\x00"


def decode_direct_frame(frame: bytes) -> DirectPacket:
    encoded = frame[:-1] if frame.endswith(b"\x00") else frame
    raw = cobs_decode(encoded)
    if len(raw) < 3:
        raise ProtocolError("frame shorter than opcode + CRC")
    expected = int.from_bytes(raw[-2:], "little")
    actual = crc16_steinel(raw[:-2])
    if expected != actual:
        raise ProtocolError(f"bad CRC: received 0x{expected:04X}, calculated 0x{actual:04X}")
    return DirectPacket(raw[0], raw[1:-2])


def response_from_packet(packet: DirectPacket) -> DirectResponse:
    if not packet.payload:
        raise ProtocolError("response contains no status byte")
    return DirectResponse(packet.opcode, packet.payload[0], packet.payload[1:])


class DirectFrameStream:
    """Reassembles COBS/NUL-delimited direct-channel frames from notifications."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[DirectPacket]:
        self.buffer.extend(data)
        packets: list[DirectPacket] = []
        while True:
            try:
                end = self.buffer.index(0)
            except ValueError:
                break
            encoded = bytes(self.buffer[:end])
            del self.buffer[: end + 1]
            if encoded:
                packets.append(decode_direct_frame(encoded))
        return packets


def identify_data(active: bool, duration: int = 10) -> bytes:
    checked_u16(duration, "duration")
    return bytes([int(active)]) + struct.pack("<H", duration)


def device_info_data(target: int = 0) -> bytes:
    return bytes([checked_u8(target, "target id")])


# Global Reset (proprietary direct GATT channel, opcode 0xE5). The magic
# constant 0x5AA5AA55 is fixed and wire-order little-endian; see
# STEINEL_BLE_KOMMUNIKATION.md section 6.6/7 and steinel_ble.py's
# GlobalResetCommand for the already-validated reference.
GLOBAL_RESET_OPCODE = 0xE5
GLOBAL_RESET_DATA = bytes.fromhex("55AAA55A")


def parse_direct_result(opcode: int, data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {"opcode": f"0x{opcode:02X}", "data_hex": hexstr(data)}
    if opcode == 0xE0:
        if len(data) != 4:
            raise ProtocolError(f"device info should be 4 bytes, got {len(data)}")
        result.update(target=data[0], firmware=f"{data[1]}.{data[2]}.{data[3]}")
    elif opcode == 0xE2:
        if len(data) != 1:
            raise ProtocolError(f"firmware state should be 1 byte, got {len(data)}")
        result.update(state=data[0], state_name=FIRMWARE_STATES.get(data[0], "UNKNOWN"))
    return result


def parse_steinel_advertisement(data: bytes, name: str | None = None) -> dict[str, Any]:
    """Decode STEINEL manufacturer specific data (company id 0x0563)."""
    if name == "SfuTg":
        if len(data) < 2:
            raise ProtocolError("SfuTg manufacturer data too short")
        result: dict[str, Any] = {
            "format": "SfuTg",
            "product_id": int.from_bytes(data[0:2], "little"),
        }
        if len(data) > 3 and data[3] > 0:
            result["bootloader"] = data[3]
        if len(data) >= 6:
            result["hash_id"] = int.from_bytes(data[4:6], "little")
            if data[5] > 0:
                result["hardware"] = data[5]
        return result
    if len(data) < 4:
        raise ProtocolError("STEINEL manufacturer data shorter than 4 bytes")
    result = {
        "format": "steinel",
        "product_id": int.from_bytes(data[0:2], "little"),
        "firmware": f"{data[3]}.{data[2]}.{data[1]}",
    }
    if len(data) > 5 and data[5] > 0:
        result["bootloader"] = data[5]
    if len(data) > 6 and data[6] > 0:
        result["hardware"] = data[6]
    if len(data) >= 9:
        result["hash_id"] = int.from_bytes(data[7:9], "little")
    return result


def advertised_firmware_matches_catalog(advertised: str, catalog: str) -> bool:
    """True if major.minor of both versions match (the wire encoding overlaps
    the displayed patch byte with the product-id high byte, see the RE docs).
    """
    import re

    advertised_match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", advertised)
    catalog_match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", catalog)
    return bool(
        advertised_match
        and catalog_match
        and advertised_match.group(1, 2) == catalog_match.group(1, 2)
    )


# ---------------------------------------------------------------------------
# Vendor / SIG Mesh Access payload builders
# ---------------------------------------------------------------------------


def vendor_access_payload(opcode: int, params: bytes = b"", company: int = STEINEL_COMPANY_ID) -> bytes:
    if not 0xC0 <= opcode <= 0xFF:
        raise ValueError("a one-byte Mesh vendor opcode must be between 0xC0 and 0xFF")
    checked_u16(company, "company id")
    return bytes([opcode]) + struct.pack("<H", company) + params


def light_property_get(property_id: int) -> bytes:
    return vendor_access_payload(0xC4, struct.pack("<H", checked_u16(property_id, "property id")))


def light_property_set(property_id: int, value: bytes, special_backlight: bool = False) -> bytes:
    prop = checked_u16(property_id, "property id")
    opcode = 0xC6 if special_backlight or prop == 9 else 0xC5
    return vendor_access_payload(opcode, struct.pack("<H", prop) + value)


def sensor_get(property_id: int | None = None) -> bytes:
    params = b"" if property_id is None else struct.pack("<H", checked_u16(property_id, "sensor id"))
    return vendor_access_payload(0xD0, params)


def sensor_setting_get(sensor_id: int, setting_id: int) -> bytes:
    return vendor_access_payload(
        0xD4, struct.pack("<HH", checked_u16(sensor_id, "sensor id"), checked_u16(setting_id, "setting id"))
    )


def sensor_setting_set(sensor_id: int, setting_id: int, value: bytes) -> bytes:
    return vendor_access_payload(
        0xD5,
        struct.pack("<HH", checked_u16(sensor_id, "sensor id"), checked_u16(setting_id, "setting id")) + value,
    )


# STEINEL Light LC Extension (vendor model 0x1001) property IDs, see
# STEINEL_BLE_KOMMUNIKATION.md section 4.1. Values are read/written as raw
# bytes via light_property_get/set (opcodes 0xC4/0xC5); the wire byte width
# is per-property (the app uses a polymorphic serializer) and is not fully
# determined from static analysis alone for every property - see
# LIGHT_PROPERTY_KINDS below for this project's best-effort classification.
LIGHT_PROPERTIES: dict[str, int] = {
    "CONDITIONAL_LUX_MEASUREMENT": 1,
    "CL_DEACTIVATE": 2,
    "PRESENTATION_OFF_ENABLE": 3,
    "PRESENTATION_ON_ENABLE": 4,
    "ECO_MODE_ENABLE": 5,
    "NEIGHBOURGROUPS_ENABLE": 6,
    "NEIGHBOURGROUP_ON_LIGHTNESS": 7,
    "KINDERGARTEN_FUNKTION_TIME": 8,
    "BACKLIGHT_RUN_ON": 9,
    "BACKLIGHT_BASELIGHT": 10,
    "BACKLIGHT_NEIGHBOURGROUP_ON": 11,
    "BASELIGHT_SETTING": 12,
    "IMPULE_MODE_ENABLE": 13,
    "TAGBETRIEB": 14,
    "LUX_TEACH_SCENE_NUMBER": 15,
    "TEST_MODE_SCENE_NUMBER": 16,
    "BRIGHTNESS_SWITCH_OFF": 17,
    "HLK_OUTPUT_ACTIVE": 18,
    "HLK_DELAY_TIME": 19,
    "HLK_SWITCH_ON_DELAY": 20,
    "ACTIVE_SCENE_NUMBER": 21,
    "NIGHTMATIC_MODE": 22,
    "CONSTANT_LIGHT_HYSTERESIS": 23,
    "DC_MODE_LIGHTNESS": 24,
    "OWN_GROUP_ADDRESS": 25,
    "SUB_SCENE_1_LIGHTNESS": 26,
    "SUB_SCENE_2_LIGHTNESS": 27,
    "SUB_SCENE_3_LIGHTNESS": 28,
    "SUB_SCENE_4_LIGHTNESS": 29,
    "SUB_SCENE_5_LIGHTNESS": 30,
    "DEFAULT_SCENE_NUMBER": 31,
    "CL_DIM_MODE": 32,
    "NM_DIM_MINIMUM_LIGHTNESS": 33,
    "NM_DIM_LUX_THRESHOLD_MIN": 34,
    "NM_DIM_LUX_THRESHOLD_MAX": 35,
    "FOLDING_DOOR1_SCENE_NUMBER": 39,
    "FOLDING_DOOR1_LIGHT_SENSOR_ADDRESS": 40,
    "FOLDING_DOOR1_MEMBER_ADDRESSES": 41,
    "FOLDING_DOOR2_SCENE_NUMBER": 42,
    "FOLDING_DOOR2_LIGHT_SENSOR_ADDRESS": 43,
    "FOLDING_DOOR2_MEMBER_ADDRESSES": 44,
    "FOLDING_DOOR3_SCENE_NUMBER": 45,
    "FOLDING_DOOR3_LIGHT_SENSOR_ADDRESS": 46,
    "FOLDING_DOOR3_MEMBER_ADDRESSES": 47,
    "LABORATORY_FUNCTION": 48,
    "SMOOTH_SCENE_TRANSITION": 49,
    "MANUAL_OVERWRITE_PROTECT_ENABLE": 50,
    "GLOBAL_MAX_LIGHTNESS": 51,
    "GLOBAL_MIN_LIGHTNESS": 52,
    "PB_IGNORE_MIN_MAX_LIGHTNESS_ENABLE": 53,
}

# Best-effort value-type classification per property, inferred from each
# property's name and the "toBytes() is polymorphic per semantic value type
# (boolean, percentage, lightness, scene number, group address, address
# list)" note in STEINEL_BLE_KOMMUNIKATION.md section 4.1. Not confirmed
# against real hardware for every property - a wrong guess fails safely
# (the device rejects it with INVALID_VALUE/INVALID_PARAMETER/
# INVALID_COMMAND_LENGTH rather than silently accepting bad data). Kinds:
# "bool" (1 byte 0/1), "u8" (1 byte), "u16" (2 bytes LE), "lightness" (2
# bytes LE, 0..65535), "scene" (2 bytes LE, 0..65535), "raw" (unknown
# width/shape - addresses and address lists - exposed as hex only).
LIGHT_PROPERTY_KINDS: dict[str, str] = {
    "CONDITIONAL_LUX_MEASUREMENT": "u16",
    "CL_DEACTIVATE": "bool",
    "PRESENTATION_OFF_ENABLE": "bool",
    "PRESENTATION_ON_ENABLE": "bool",
    "ECO_MODE_ENABLE": "bool",
    "NEIGHBOURGROUPS_ENABLE": "bool",
    "NEIGHBOURGROUP_ON_LIGHTNESS": "lightness",
    "KINDERGARTEN_FUNKTION_TIME": "u16",
    "BACKLIGHT_RUN_ON": "u16",
    "BACKLIGHT_BASELIGHT": "lightness",
    "BACKLIGHT_NEIGHBOURGROUP_ON": "bool",
    "BASELIGHT_SETTING": "lightness",
    "IMPULE_MODE_ENABLE": "bool",
    "TAGBETRIEB": "bool",
    "LUX_TEACH_SCENE_NUMBER": "scene",
    "TEST_MODE_SCENE_NUMBER": "scene",
    "BRIGHTNESS_SWITCH_OFF": "lightness",
    "HLK_OUTPUT_ACTIVE": "bool",
    "HLK_DELAY_TIME": "u16",
    "HLK_SWITCH_ON_DELAY": "u16",
    "ACTIVE_SCENE_NUMBER": "scene",
    "NIGHTMATIC_MODE": "u8",
    "CONSTANT_LIGHT_HYSTERESIS": "u16",
    "DC_MODE_LIGHTNESS": "lightness",
    "OWN_GROUP_ADDRESS": "raw",
    "SUB_SCENE_1_LIGHTNESS": "lightness",
    "SUB_SCENE_2_LIGHTNESS": "lightness",
    "SUB_SCENE_3_LIGHTNESS": "lightness",
    "SUB_SCENE_4_LIGHTNESS": "lightness",
    "SUB_SCENE_5_LIGHTNESS": "lightness",
    "DEFAULT_SCENE_NUMBER": "scene",
    "CL_DIM_MODE": "u8",
    "NM_DIM_MINIMUM_LIGHTNESS": "lightness",
    "NM_DIM_LUX_THRESHOLD_MIN": "u16",
    "NM_DIM_LUX_THRESHOLD_MAX": "u16",
    "FOLDING_DOOR1_SCENE_NUMBER": "scene",
    "FOLDING_DOOR1_LIGHT_SENSOR_ADDRESS": "raw",
    "FOLDING_DOOR1_MEMBER_ADDRESSES": "raw",
    "FOLDING_DOOR2_SCENE_NUMBER": "scene",
    "FOLDING_DOOR2_LIGHT_SENSOR_ADDRESS": "raw",
    "FOLDING_DOOR2_MEMBER_ADDRESSES": "raw",
    "FOLDING_DOOR3_SCENE_NUMBER": "scene",
    "FOLDING_DOOR3_LIGHT_SENSOR_ADDRESS": "raw",
    "FOLDING_DOOR3_MEMBER_ADDRESSES": "raw",
    "LABORATORY_FUNCTION": "bool",
    "SMOOTH_SCENE_TRANSITION": "bool",
    "MANUAL_OVERWRITE_PROTECT_ENABLE": "bool",
    "GLOBAL_MAX_LIGHTNESS": "lightness",
    "GLOBAL_MIN_LIGHTNESS": "lightness",
    "PB_IGNORE_MIN_MAX_LIGHTNESS_ENABLE": "bool",
}

# Properties worth showing by default; everything else in LIGHT_PROPERTIES
# is still fully accessible but starts disabled (entity_registry_enabled_
# default=False) so a lamp's entity list isn't dominated by KNX/folding-door/
# laboratory-mode settings most installations never touch.
LIGHT_PROPERTIES_DEFAULT_ENABLED = {
    "ECO_MODE_ENABLE",
    "GLOBAL_MAX_LIGHTNESS",
    "GLOBAL_MIN_LIGHTNESS",
    "NIGHTMATIC_MODE",
}

# STEINEL Sensor Extension (vendor model 0x1003) data property IDs, see
# STEINEL_BLE_KOMMUNIKATION.md section 4.2. IDs below 0xFF00 are official
# Bluetooth SIG Device Property IDs with SIG-standardised characteristic
# encodings (see parse_sensor_value); the 0xFFxx range is STEINEL-vendor and
# has no published encoding, so those are only exposed as raw hex.
SENSOR_PROPERTIES: dict[str, int] = {
    "PRESENT_AMBIENT_RELATIVE_HUMIDITY": 0x0076,
    "AIR_PRESSURE": 0x0082,
    "VOC": 0x0078,
    "CO2": 0x0077,
    "SMOKE": 0xFFFA,
    "NOISE": 0x0079,
    "MOTION_SENSED": 0x0042,
    "PEOPLE_COUNT": 0x004C,
    "PRESENCE_DETECTED": 0x004D,
    "TIME_SINCE_MOTION_SENSED": 0x0068,
    "TIME_SINCE_PRESENCE_DETECTED": 0x0069,
    "PRESENT_AMBIENT_TEMPERATURE": 0x004F,
    "PRECISE_PRESENT_AMBIENT_TEMPERATURE": 0x0075,
    "DEW_POINT": 0x0087,
    "INDOOR_AIR_QUALITY": 0xFFF2,
    "RISK": 0xFFF0,
    "RGB_LED": 0xFFEF,
    "ALARM": 0xFFEE,
    "STALE_AIR": 0xFFE8,
    "COMFORT_ZONE": 0xFFE7,
    "PIXEL_POSITION": 0xFFE2,
    "DOOR_COUNT": 0xFFDA,
}

SENSOR_SETTINGS: dict[str, int] = {
    "SENSOR_HW_ENABLE": 0xFFFF,
    "DETECTION_DELAY": 0xFFF8,
    "PEOPLE_COUNT_THRESHOLD": 0xFFF7,
    "SENSITIVITY_MODE": 0xFFF6,
    "RANGE4": 0xFFF5,
    "PRESENCE_DETECTION_MODE": 0xFFF4,
    "ACTIVE_ZONES": 0xFFF3,
    "RANGE5": 0xFFF1,
    "NOTIFICATION_TYPE": 0xFFED,
    "INSTALLATION_TYPE": 0xFFEC,
    "TEMPERATURE_OFFSET": 0xFFEB,
    "TRUE_PRESENCE": 0xFFEA,
    "RAG_RATING_THRESHOLD": 0xFFE9,
    "LED_PATTERN": 0xFFE6,
    "COMFORT_ZONE_SETTING": 0xFFE5,
    "KEEP_NOISE_MAP": 0xFFE4,
    "DO_INIT": 0xFFE3,
    "PIXEL_AREA_DEFINITION": 0xFFE1,
    "SET_BY": 0xFFDB,
    "DOOR_COUNT_RESET": 0xFFD9,
    "DUALTECH_TRIGGER": 0xFFDE,
    "CHANNEL": 0xFFDD,
    "DEFAULT_RESET": 0xFFDC,
}

# SENSOR_PROPERTIES entries with a published Bluetooth SIG Device Property
# encoding (see parse_sensor_value) - the ones worth polling and exposing as
# typed sensor/binary_sensor entities. The remaining, 0xFFxx STEINEL-vendor
# entries have no published encoding and are only meaningful as raw hex.
STANDARD_SENSOR_PROPERTIES: tuple[str, ...] = (
    "PRESENCE_DETECTED",
    "MOTION_SENSED",
    "PEOPLE_COUNT",
    "TIME_SINCE_MOTION_SENSED",
    "TIME_SINCE_PRESENCE_DETECTED",
    "PRESENT_AMBIENT_TEMPERATURE",
    "PRECISE_PRESENT_AMBIENT_TEMPERATURE",
    "PRESENT_AMBIENT_RELATIVE_HUMIDITY",
    "CO2",
    "VOC",
    "NOISE",
    "AIR_PRESSURE",
    "DEW_POINT",
)


def humanize_property_name(name: str) -> str:
    """"ECO_MODE_ENABLE" -> "Eco Mode Enable", for use as an entity name."""
    return name.replace("_", " ").title()


@dataclasses.dataclass(frozen=True)
class SensorValue:
    raw: bytes
    value: float | int | bool | None
    unit: str | None


def parse_sensor_value(property_name: str, data: bytes) -> SensorValue:
    """Best-effort decode using the official Bluetooth SIG Device Property
    characteristic encodings for the standard (non-0xFFxx) property IDs.
    Unknown/vendor properties, or a payload that doesn't match the expected
    width, are returned with value=None (raw hex is always available)."""

    def u(n: int) -> int | None:
        return int.from_bytes(data[:n], "little") if len(data) >= n else None

    def s(n: int) -> int | None:
        return int.from_bytes(data[:n], "little", signed=True) if len(data) >= n else None

    if property_name == "PRESENCE_DETECTED":
        v = u(1)
        return SensorValue(data, None if v is None else bool(v), None)
    if property_name == "MOTION_SENSED":
        v = u(1)
        return SensorValue(data, v, "%")
    if property_name in ("PRESENT_AMBIENT_TEMPERATURE", "DEW_POINT"):
        v = s(1)
        return SensorValue(data, None if v is None or v == 0x7F else v * 0.5, "°C")
    if property_name == "PRECISE_PRESENT_AMBIENT_TEMPERATURE":
        v = s(2)
        return SensorValue(data, None if v is None else v * 0.01, "°C")
    if property_name == "PRESENT_AMBIENT_RELATIVE_HUMIDITY":
        v = u(2)
        return SensorValue(data, None if v is None else v * 0.01, "%")
    if property_name in ("CO2", "VOC"):
        v = u(2)
        return SensorValue(data, v, "ppm")
    if property_name == "NOISE":
        v = u(1)
        return SensorValue(data, v, "dB")
    if property_name == "AIR_PRESSURE":
        v = u(4)
        return SensorValue(data, None if v is None else v * 0.1, "Pa")
    if property_name == "PEOPLE_COUNT":
        v = u(2)
        return SensorValue(data, v, None)
    if property_name in ("TIME_SINCE_MOTION_SENSED", "TIME_SINCE_PRESENCE_DETECTED"):
        v = u(2)
        return SensorValue(data, v, "s")
    return SensorValue(data, None, None)


def sig_opcode(opcode: int) -> bytes:
    if 0 <= opcode <= 0x7F:
        return bytes([opcode])
    if 0x8000 <= opcode <= 0xBFFF:
        return opcode.to_bytes(2, "big")
    raise ValueError("only one- or two-byte SIG Mesh Access opcodes are supported")


# Bluetooth-SIG-assigned Generic/Light Mesh Model opcodes (public spec values).
OP_ONOFF_GET = 0x8201
OP_ONOFF_SET = 0x8202
OP_ONOFF_SET_UNACK = 0x8203
OP_ONOFF_STATUS = 0x8204
OP_LIGHTNESS_GET = 0x824B
OP_LIGHTNESS_SET = 0x824C
OP_LIGHTNESS_SET_UNACK = 0x824D
OP_LIGHTNESS_STATUS = 0x824E
OP_CTL_GET = 0x825D
OP_CTL_SET = 0x825E
OP_CTL_SET_UNACK = 0x825F
OP_CTL_STATUS = 0x8260
OP_HSL_GET = 0x826D
OP_HSL_SET = 0x8276
OP_HSL_SET_UNACK = 0x8277
OP_HSL_STATUS = 0x8278

MESH_GET_OPCODES = {
    "onoff": OP_ONOFF_GET,
    "lightness": OP_LIGHTNESS_GET,
    "ctl": OP_CTL_GET,
    "hsl": OP_HSL_GET,
}


def transition_fields(transition: int | None, delay: int | None) -> bytes:
    if transition is None and delay is None:
        return b""
    if transition is None or delay is None:
        raise ValueError("transition and delay must be given together")
    return bytes([checked_u8(transition, "transition"), checked_u8(delay, "delay")])


def onoff_set_payload(value: bool, tid: int, unack: bool = True) -> bytes:
    return sig_opcode(OP_ONOFF_SET_UNACK if unack else OP_ONOFF_SET) + bytes([int(value), tid & 0xFF])


def lightness_set_payload(lightness: int, tid: int, unack: bool = True) -> bytes:
    return sig_opcode(OP_LIGHTNESS_SET_UNACK if unack else OP_LIGHTNESS_SET) + struct.pack(
        "<HB", checked_u16(lightness, "lightness"), tid & 0xFF
    )


def ctl_set_payload(lightness: int, temperature: int, delta_uv: int, tid: int, unack: bool = True) -> bytes:
    if not -32768 <= delta_uv <= 32767:
        raise ValueError("delta uv must be between -32768 and 32767")
    return sig_opcode(OP_CTL_SET_UNACK if unack else OP_CTL_SET) + struct.pack(
        "<HHhB",
        checked_u16(lightness, "lightness"),
        checked_u16(temperature, "ctl temperature"),
        delta_uv,
        tid & 0xFF,
    )


def hsl_set_payload(lightness: int, hue: int, saturation: int, tid: int, unack: bool = True) -> bytes:
    return sig_opcode(OP_HSL_SET_UNACK if unack else OP_HSL_SET) + struct.pack(
        "<HHHB",
        checked_u16(lightness, "lightness"),
        checked_u16(hue, "hue"),
        checked_u16(saturation, "saturation"),
        tid & 0xFF,
    )


@dataclasses.dataclass(frozen=True)
class OnOffStatus:
    present: bool
    target: bool | None
    remaining_time_raw: int | None


def parse_onoff_status(data: bytes) -> OnOffStatus:
    if len(data) not in (1, 3):
        raise ProtocolError(f"Generic OnOff Status should be 1 or 3 bytes, got {len(data)}")
    if len(data) == 1:
        return OnOffStatus(bool(data[0]), None, None)
    return OnOffStatus(bool(data[0]), bool(data[1]), data[2])


@dataclasses.dataclass(frozen=True)
class LightnessStatus:
    present: int
    target: int | None
    remaining_time_raw: int | None


def parse_lightness_status(data: bytes) -> LightnessStatus:
    if len(data) not in (2, 5):
        raise ProtocolError(f"Light Lightness Status should be 2 or 5 bytes, got {len(data)}")
    present = int.from_bytes(data[0:2], "little")
    if len(data) == 2:
        return LightnessStatus(present, None, None)
    return LightnessStatus(present, int.from_bytes(data[2:4], "little"), data[4])


@dataclasses.dataclass(frozen=True)
class CtlStatus:
    present_lightness: int
    present_temperature: int
    target_lightness: int | None
    target_temperature: int | None
    remaining_time_raw: int | None


def parse_ctl_status(data: bytes) -> CtlStatus:
    if len(data) not in (4, 9):
        raise ProtocolError(f"Light CTL Status should be 4 or 9 bytes, got {len(data)}")
    present_lightness, present_temperature = struct.unpack_from("<HH", data, 0)
    if len(data) == 4:
        return CtlStatus(present_lightness, present_temperature, None, None, None)
    target_lightness, target_temperature = struct.unpack_from("<HH", data, 4)
    return CtlStatus(present_lightness, present_temperature, target_lightness, target_temperature, data[8])


@dataclasses.dataclass(frozen=True)
class HslStatus:
    lightness: int
    hue: int
    saturation: int
    remaining_time_raw: int | None


def parse_hsl_status(data: bytes) -> HslStatus:
    if len(data) not in (6, 7):
        raise ProtocolError(f"Light HSL Status should be 6 or 7 bytes, got {len(data)}")
    lightness, hue, saturation = struct.unpack_from("<HHH", data, 0)
    return HslStatus(lightness, hue, saturation, data[6] if len(data) == 7 else None)


# ---------------------------------------------------------------------------
# Config model messages (Config AppKey Add / Config Model App Bind)
# ---------------------------------------------------------------------------

OP_CONFIG_APPKEY_ADD = 0x00
OP_CONFIG_APPKEY_STATUS = sig_opcode(0x8003)
OP_CONFIG_MODEL_APP_BIND = sig_opcode(0x803D)
OP_CONFIG_MODEL_APP_STATUS = sig_opcode(0x803E)


def packed_key_indexes(net_key_index: int, app_key_index: int) -> bytes:
    if not 0 <= net_key_index <= 0xFFF or not 0 <= app_key_index <= 0xFFF:
        raise ValueError("NetKey/AppKey index must be between 0 and 0xFFF")
    return bytes(
        [
            net_key_index & 0xFF,
            ((net_key_index >> 8) & 0x0F) | ((app_key_index & 0x0F) << 4),
            (app_key_index >> 4) & 0xFF,
        ]
    )


def config_app_key_add(net_key_index: int, app_key_index: int, app_key: bytes) -> bytes:
    return bytes([OP_CONFIG_APPKEY_ADD]) + packed_key_indexes(net_key_index, app_key_index) + app_key


def config_model_app_bind(element: int, app_key_index: int, model: int) -> bytes:
    checked_u16(element, "element address")
    checked_u16(model, "SIG model id")
    if not 0 <= app_key_index <= 0xFFF:
        raise ValueError("AppKey index must be between 0 and 0xFFF")
    return OP_CONFIG_MODEL_APP_BIND + struct.pack("<HHH", element, app_key_index, model)


def config_model_app_bind_vendor(element: int, app_key_index: int, company: int, model: int) -> bytes:
    """Like config_model_app_bind, but for a vendor model: the Mesh spec's
    4-octet vendor Model Identifier is Company ID followed by Model ID."""
    checked_u16(element, "element address")
    checked_u16(company, "company id")
    checked_u16(model, "vendor model id")
    if not 0 <= app_key_index <= 0xFFF:
        raise ValueError("AppKey index must be between 0 and 0xFFF")
    return OP_CONFIG_MODEL_APP_BIND + struct.pack("<HHHH", element, app_key_index, company, model)


# ---------------------------------------------------------------------------
# Mesh crypto primitives (s1/k1/k2/k4, AES-CMAC/CCM/ECB)
# ---------------------------------------------------------------------------


def _need_crypto() -> tuple[Any, Any, Any]:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.ciphers.aead import AESCCM
        from cryptography.hazmat.primitives.cmac import CMAC
    except ImportError as exc:  # pragma: no cover - always bundled with HA core
        raise RuntimeError("Mesh cryptography requires the 'cryptography' package") from exc
    return AESCCM, CMAC, (Cipher, algorithms, modes)


def _need_ec() -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Mesh provisioning requires the 'cryptography' package") from exc
    return ec


def _aes_cmac(key: bytes, message: bytes) -> bytes:
    _, CMAC, crypto = _need_crypto()
    _, algorithms, _ = crypto
    cmac = CMAC(algorithms.AES(key))
    cmac.update(message)
    return cmac.finalize()


def _aes_ecb(key: bytes, block: bytes) -> bytes:
    _, _, crypto = _need_crypto()
    Cipher, algorithms, modes = crypto
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(block) + encryptor.finalize()


def aes_cmac(key: bytes, message: bytes) -> bytes:
    return _aes_cmac(key, message)


def mesh_s1(message: bytes) -> bytes:
    return _aes_cmac(bytes(16), message)


def mesh_k1(n: bytes, salt: bytes, p: bytes) -> bytes:
    if len(salt) != 16:
        raise ValueError("k1 salt must be 16 bytes")
    return _aes_cmac(_aes_cmac(salt, n), p)


def mesh_k2(net_key: bytes, p: bytes = b"\x00") -> tuple[int, bytes, bytes]:
    salt = mesh_s1(b"smk2")
    t = _aes_cmac(salt, net_key)
    t1 = _aes_cmac(t, p + b"\x01")
    t2 = _aes_cmac(t, t1 + p + b"\x02")
    t3 = _aes_cmac(t, t2 + p + b"\x03")
    return t1[-1] & 0x7F, t2, t3


def mesh_k4(app_key: bytes) -> int:
    salt = mesh_s1(b"smk4")
    t = _aes_cmac(salt, app_key)
    return _aes_cmac(t, b"id6\x01")[-1] & 0x3F


# ---------------------------------------------------------------------------
# Provisioning (PB-GATT)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ProvisioningCapabilities:
    elements: int
    algorithms: int
    public_key_oob: bool
    static_oob: bool
    output_oob_size: int
    output_oob_actions: int
    input_oob_size: int
    input_oob_actions: int

    @classmethod
    def parse(cls, pdu: bytes) -> ProvisioningCapabilities:
        if len(pdu) != 12 or pdu[0] != 0x01:
            raise ProtocolError(f"Provisioning Capabilities must be opcode 0x01 + 11 bytes: {hexstr(pdu)}")
        if pdu[1] == 0:
            raise ProtocolError("provisionee reports zero elements")
        return cls(
            elements=pdu[1],
            algorithms=int.from_bytes(pdu[2:4], "big"),
            public_key_oob=bool(pdu[4]),
            static_oob=bool(pdu[5]),
            output_oob_size=pdu[6],
            output_oob_actions=int.from_bytes(pdu[7:9], "big"),
            input_oob_size=pdu[9],
            input_oob_actions=int.from_bytes(pdu[10:12], "big"),
        )


@dataclasses.dataclass(frozen=True)
class ProvisioningSecrets:
    confirmation_salt: bytes
    confirmation_key: bytes
    provisioning_salt: bytes
    session_key: bytes
    session_nonce: bytes
    device_key: bytes


def provisioning_public_key(private_key: Any) -> bytes:
    numbers = private_key.public_key().public_numbers()
    return numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")


def provisioning_ecdh(private_key: Any, peer_public_key: bytes) -> bytes:
    if len(peer_public_key) != 64:
        raise ProtocolError("Provisioning Public Key must be 64 bytes (X || Y)")
    ec = _need_ec()
    x = int.from_bytes(peer_public_key[:32], "big")
    y = int.from_bytes(peer_public_key[32:], "big")
    try:
        peer = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        return private_key.exchange(ec.ECDH(), peer)
    except ValueError as exc:
        raise ProtocolError("provisionee public key is not a valid P-256 point") from exc


def provisioning_confirmation_inputs(
    invite: bytes, capabilities: bytes, start: bytes, provisioner_key: bytes, device_key: bytes
) -> bytes:
    if invite[:1] != b"\x00" or capabilities[:1] != b"\x01" or start[:1] != b"\x02":
        raise ValueError("Invite/Capabilities/Start have the wrong provisioning opcode")
    if len(invite) != 2 or len(capabilities) != 12 or len(start) != 6:
        raise ValueError("Invite/Capabilities/Start have the wrong length")
    if len(provisioner_key) != 64 or len(device_key) != 64:
        raise ValueError("Provisioning public keys must each be 64 bytes")
    return invite[1:] + capabilities[1:] + start[1:] + provisioner_key + device_key


def provisioning_secrets(
    ecdh_secret: bytes, confirmation_inputs: bytes, provisioner_random: bytes, device_random: bytes
) -> ProvisioningSecrets:
    if len(ecdh_secret) != 32 or len(provisioner_random) != 16 or len(device_random) != 16:
        raise ValueError("ECDH secret must be 32 bytes, Provisioning Random each 16 bytes")
    confirmation_salt = mesh_s1(confirmation_inputs)
    confirmation_key = mesh_k1(ecdh_secret, confirmation_salt, b"prck")
    provisioning_salt = mesh_s1(confirmation_salt + provisioner_random + device_random)
    return ProvisioningSecrets(
        confirmation_salt=confirmation_salt,
        confirmation_key=confirmation_key,
        provisioning_salt=provisioning_salt,
        session_key=mesh_k1(ecdh_secret, provisioning_salt, b"prsk"),
        session_nonce=mesh_k1(ecdh_secret, provisioning_salt, b"prsn")[3:],
        device_key=mesh_k1(ecdh_secret, provisioning_salt, b"prdk"),
    )


def provisioning_data(net_key: bytes, net_key_index: int, flags: int, iv_index: int, unicast: int) -> bytes:
    if len(net_key) != 16:
        raise ValueError("NetKey must be 16 bytes")
    if not 0 <= net_key_index <= 0xFFF:
        raise ValueError("NetKey index must be between 0 and 0xFFF")
    checked_u8(flags, "provisioning flags")
    if not 0 <= iv_index <= 0xFFFFFFFF:
        raise ValueError("IV index out of uint32 range")
    if not 1 <= unicast <= 0x7FFF:
        raise ValueError("unicast address must be between 0x0001 and 0x7FFF")
    return net_key + net_key_index.to_bytes(2, "big") + bytes([flags]) + iv_index.to_bytes(4, "big") + unicast.to_bytes(2, "big")


def validate_unicast_allocation(
    nodes: dict[str, dict[str, Any]], provisioner_source: int, unicast: int, elements: int
) -> None:
    end = unicast + elements - 1
    if not 1 <= unicast <= end <= 0x7FFF:
        raise ValueError("the element unicast range is outside 0x0001..0x7FFF")
    if unicast <= provisioner_source <= end:
        raise ValueError("the new node's address range overlaps the provisioner source address")
    for address_text, node in nodes.items():
        base = int(str(node.get("unicast", address_text)), 0)
        node_end = base + int(node.get("elements", 1)) - 1
        if not (end < base or unicast > node_end):
            raise ValueError(f"address range 0x{unicast:04X}..0x{end:04X} overlaps node {address_text}")


def allocate_unicast(nodes: dict[str, dict[str, Any]], elements: int, start: int = 0x0100) -> int:
    """Pick the next free contiguous unicast range after all known nodes."""
    candidate = start
    while True:
        try:
            validate_unicast_allocation(nodes, provisioner_source=0, unicast=candidate, elements=elements)
            return candidate
        except ValueError:
            candidate += 1
            if candidate > 0x7FFF:
                raise ProtocolError("no free unicast address range left") from None


# ---------------------------------------------------------------------------
# Network / Access layer (Mesh Proxy)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DecodedMeshAccess:
    source: int
    destination: int
    sequence: int
    ttl: int
    access_payload: bytes


class MeshNetworkCodec:
    """Stateless-per-call Mesh network/access encoder & decoder.

    Sequence numbers are supplied by the caller (already reserved and
    persisted) rather than tracked internally, so this class never performs
    IO and is safe to reuse across concurrent async operations.
    """

    def __init__(self, net_key: bytes, app_key: bytes, source: int, iv_index: int, ttl: int = 5) -> None:
        if len(net_key) != 16 or len(app_key) != 16:
            raise ValueError("NetKey and AppKey must each be 16 bytes")
        self.net_key = net_key
        self.app_key = app_key
        self.source = source
        self.iv_index = iv_index
        self.ttl = ttl
        self.nid, self.encryption_key, self.privacy_key = mesh_k2(net_key)
        self.aid = mesh_k4(app_key)

    @staticmethod
    def _application_nonce(sequence: int, source: int, destination: int, iv_index: int) -> bytes:
        return b"\x01\x00" + sequence.to_bytes(3, "big") + source.to_bytes(2, "big") + destination.to_bytes(2, "big") + iv_index.to_bytes(4, "big")

    @staticmethod
    def _network_nonce(ctl_ttl: int, sequence: int, source: int, iv_index: int) -> bytes:
        return b"\x00" + bytes([ctl_ttl]) + sequence.to_bytes(3, "big") + source.to_bytes(2, "big") + b"\x00\x00" + iv_index.to_bytes(4, "big")

    @staticmethod
    def _device_nonce(sequence: int, source: int, destination: int, iv_index: int) -> bytes:
        return b"\x02\x00" + sequence.to_bytes(3, "big") + source.to_bytes(2, "big") + destination.to_bytes(2, "big") + iv_index.to_bytes(4, "big")

    def _encode_network(self, destination: int, lower_transport: bytes, sequence: int, ttl: int, ctl: int = 0) -> bytes:
        source = self.source
        iv_index = self.iv_index
        ctl_ttl = (ctl << 7) | ttl
        net_nonce = self._network_nonce(ctl_ttl, sequence, source, iv_index)
        plaintext = destination.to_bytes(2, "big") + lower_transport
        AESCCM, _, _ = _need_crypto()
        encrypted = AESCCM(self.encryption_key, tag_length=8 if ctl else 4).encrypt(net_nonce, plaintext, None)
        privacy_random = encrypted[:7]
        privacy_plaintext = b"\x00" * 5 + iv_index.to_bytes(4, "big") + privacy_random
        pecb = _aes_ecb(self.privacy_key, privacy_plaintext)
        clear_header = bytes([ctl_ttl]) + sequence.to_bytes(3, "big") + source.to_bytes(2, "big")
        obfuscated = bytes(a ^ b for a, b in zip(clear_header, pecb))
        return bytes([((iv_index & 1) << 7) | self.nid]) + obfuscated + encrypted

    def encode_access(self, destination: int, access_payload: bytes, sequence: int, ttl: int | None = None) -> bytes:
        checked_u16(destination, "destination address")
        if destination == 0:
            raise ValueError("mesh destination 0x0000 is unassigned")
        if len(access_payload) > 11:
            raise ValueError(f"access payload is {len(access_payload)} bytes; unsegmented allows at most 11")
        effective_ttl = self.ttl if ttl is None else ttl
        if not 0 <= effective_ttl <= 0x7F:
            raise ValueError("TTL must be between 0 and 127")
        AESCCM, _, _ = _need_crypto()
        app_nonce = self._application_nonce(sequence, self.source, destination, self.iv_index)
        upper = AESCCM(self.app_key, tag_length=4).encrypt(app_nonce, access_payload, None)
        lower = bytes([0x40 | self.aid]) + upper
        return self._encode_network(destination, lower, sequence, effective_ttl)

    def encode_device_access(
        self,
        destination: int,
        access_payload: bytes,
        device_key: bytes,
        first_sequence: int,
        ttl: int | None = None,
    ) -> list[bytes]:
        """Encode a (possibly segmented) device-key-encrypted message.

        ``first_sequence`` must be the first of ``segment_count`` (see
        :func:`device_access_segment_count`) already-reserved consecutive
        sequence numbers.
        """
        checked_u16(destination, "destination address")
        if not 1 <= destination <= 0x7FFF:
            raise ValueError("device-key destination must be a unicast address")
        if len(device_key) != 16:
            raise ValueError("device key must be 16 bytes")
        effective_ttl = self.ttl if ttl is None else ttl
        if not 0 <= effective_ttl <= 0x7F:
            raise ValueError("TTL must be between 0 and 127")
        segment_count = device_access_segment_count(access_payload)
        AESCCM, _, _ = _need_crypto()
        upper = AESCCM(device_key, tag_length=4).encrypt(
            self._device_nonce(first_sequence, self.source, destination, self.iv_index), access_payload, None
        )
        if segment_count == 1:
            return [self._encode_network(destination, b"\x00" + upper, first_sequence, effective_ttl)]
        seq_zero = first_sequence & 0x1FFF
        seg_n = segment_count - 1
        result = []
        for seg_o in range(segment_count):
            segment_header = (seq_zero << 10) | (seg_o << 5) | seg_n
            lower = b"\x80" + segment_header.to_bytes(3, "big") + upper[seg_o * 12 : (seg_o + 1) * 12]
            result.append(self._encode_network(destination, lower, first_sequence + seg_o, effective_ttl))
        return result

    def decode_device_access(self, network_pdu: bytes, device_key: bytes) -> DecodedMeshAccess:
        if len(device_key) != 16:
            raise ValueError("device key must be 16 bytes")
        source, destination, sequence, ttl, lower = self._decode_network(network_pdu)
        if not lower or lower[0] & 0x80:
            raise ProtocolError("segmented device-key responses are not decoded")
        if lower[0] & 0x40:
            raise ProtocolError("AppKey message received instead of device-key message")
        AESCCM, _, _ = _need_crypto()
        try:
            access = AESCCM(device_key, tag_length=4).decrypt(
                self._device_nonce(sequence, source, destination, self.iv_index), lower[1:], None
            )
        except Exception as exc:
            raise ProtocolError("device-key TransMIC invalid") from exc
        return DecodedMeshAccess(source, destination, sequence, ttl, access)

    def _decode_network(self, network_pdu: bytes) -> tuple[int, int, int, int, bytes]:
        if len(network_pdu) < 14:
            raise ProtocolError("network PDU too short")
        if network_pdu[0] & 0x7F != self.nid:
            raise ProtocolError("NID does not match the configured NetKey")
        iv_index = self.iv_index
        if (iv_index & 1) != (network_pdu[0] >> 7):
            raise ProtocolError("IVI does not match the configured IV index")
        privacy_random = network_pdu[7:14]
        privacy_plaintext = b"\x00" * 5 + iv_index.to_bytes(4, "big") + privacy_random
        pecb = _aes_ecb(self.privacy_key, privacy_plaintext)
        clear_header = bytes(a ^ b for a, b in zip(network_pdu[1:7], pecb))
        ctl_ttl = clear_header[0]
        ctl = ctl_ttl >> 7
        sequence = int.from_bytes(clear_header[1:4], "big")
        source = int.from_bytes(clear_header[4:6], "big")
        net_nonce = self._network_nonce(ctl_ttl, sequence, source, iv_index)
        AESCCM, _, _ = _need_crypto()
        try:
            decrypted = AESCCM(self.encryption_key, tag_length=8 if ctl else 4).decrypt(net_nonce, network_pdu[7:], None)
        except Exception as exc:
            raise ProtocolError("network MIC invalid, or wrong NetKey/IV index") from exc
        destination = int.from_bytes(decrypted[:2], "big")
        if ctl:
            raise ProtocolError("control message cannot be decoded as an access message")
        return source, destination, sequence, ctl_ttl & 0x7F, decrypted[2:]

    def encode_proxy_configuration(self, transport_pdu: bytes, sequence: int) -> bytes:
        if not 1 <= len(transport_pdu) <= 12:
            raise ValueError("proxy transport PDU must be opcode + at most 11 parameter bytes")
        source = self.source
        iv_index = self.iv_index
        nonce = b"\x03\x00" + sequence.to_bytes(3, "big") + source.to_bytes(2, "big") + b"\x00\x00" + iv_index.to_bytes(4, "big")
        AESCCM, _, _ = _need_crypto()
        encrypted = AESCCM(self.encryption_key, tag_length=8).encrypt(nonce, b"\x00\x00" + transport_pdu, None)
        privacy_random = encrypted[:7]
        privacy_plaintext = b"\x00" * 5 + iv_index.to_bytes(4, "big") + privacy_random
        pecb = _aes_ecb(self.privacy_key, privacy_plaintext)
        clear_header = b"\x80" + sequence.to_bytes(3, "big") + source.to_bytes(2, "big")
        obfuscated = bytes(a ^ b for a, b in zip(clear_header, pecb))
        return bytes([((iv_index & 1) << 7) | self.nid]) + obfuscated + encrypted

    def decode_access(self, network_pdu: bytes) -> DecodedMeshAccess:
        source, destination, sequence, ttl, lower = self._decode_network(network_pdu)
        if not lower or lower[0] & 0x80:
            raise ProtocolError("segmented lower-transport messages are not decoded")
        if not lower[0] & 0x40:
            raise ProtocolError("device-key message received instead of AppKey message")
        if lower[0] & 0x3F != self.aid:
            raise ProtocolError("AID does not match the configured AppKey")
        app_nonce = self._application_nonce(sequence, source, destination, self.iv_index)
        AESCCM, _, _ = _need_crypto()
        try:
            access = AESCCM(self.app_key, tag_length=4).decrypt(app_nonce, lower[1:], None)
        except Exception as exc:
            raise ProtocolError("TransMIC invalid, or wrong AppKey") from exc
        return DecodedMeshAccess(source, destination, sequence, ttl, access)


def device_access_segment_count(access_payload: bytes) -> int:
    upper_length = len(access_payload) + 4
    segment_count = (upper_length + 11) // 12
    if segment_count > 32:
        raise ValueError("device-key access message needs more than 32 segments")
    return segment_count


# ---------------------------------------------------------------------------
# Proxy PDU segmentation (SAR)
# ---------------------------------------------------------------------------


def proxy_segments(pdu_type: int, pdu: bytes, max_write: int) -> list[bytes]:
    if not 0 <= pdu_type <= 0x3F:
        raise ValueError("proxy PDU type must be between 0 and 63")
    chunk_size = max_write - 1
    if chunk_size < 1:
        raise ValueError("GATT write size must be at least 2 bytes")
    chunks = [pdu[i : i + chunk_size] for i in range(0, len(pdu), chunk_size)] or [b""]
    if len(chunks) == 1:
        return [bytes([pdu_type]) + chunks[0]]
    result = [bytes([0x40 | pdu_type]) + chunks[0]]
    result.extend(bytes([0x80 | pdu_type]) + chunk for chunk in chunks[1:-1])
    result.append(bytes([0xC0 | pdu_type]) + chunks[-1])
    return result


class ProxySarReceiver:
    def __init__(self) -> None:
        self.current_type: int | None = None
        self.buffer = bytearray()

    def feed(self, segment: bytes) -> tuple[int, bytes] | None:
        if not segment:
            raise ProtocolError("empty proxy segment")
        sar, pdu_type = segment[0] >> 6, segment[0] & 0x3F
        payload = segment[1:]
        if sar == 0:
            if self.current_type is not None:
                self.current_type = None
                self.buffer.clear()
                raise ProtocolError("complete PDU received during an ongoing SAR message")
            return pdu_type, payload
        if sar == 1:
            self.current_type = pdu_type
            self.buffer = bytearray(payload)
            return None
        if self.current_type != pdu_type:
            self.current_type = None
            self.buffer.clear()
            raise ProtocolError("proxy SAR type does not match the started message")
        if sar == 2:
            self.buffer.extend(payload)
            return None
        if sar == 3:
            self.buffer.extend(payload)
            complete = bytes(self.buffer)
            self.current_type = None
            self.buffer.clear()
            return pdu_type, complete
        raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Nordic Secure DFU init-packet parsing (protobuf subset)
# ---------------------------------------------------------------------------


def _protobuf_fields(data: bytes) -> dict[int, list[int | bytes]]:
    fields: dict[int, list[int | bytes]] = {}
    offset = 0

    def varint() -> int:
        nonlocal offset
        value = 0
        shift = 0
        while offset < len(data) and shift <= 63:
            byte = data[offset]
            offset += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7
        raise ProtocolError("invalid protobuf varint in the DFU init packet")

    while offset < len(data):
        key = varint()
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ProtocolError("invalid protobuf field number in the DFU init packet")
        if wire == 0:
            value: int | bytes = varint()
        elif wire == 2:
            length = varint()
            end = offset + length
            if end > len(data):
                raise ProtocolError("truncated protobuf field in the DFU init packet")
            value = data[offset:end]
            offset = end
        else:
            raise ProtocolError(f"unsupported protobuf wire type {wire} in the DFU init packet")
        fields.setdefault(number, []).append(value)
    return fields


def parse_nordic_init_packet(data: bytes) -> dict[str, int]:
    try:
        signed = _protobuf_fields(data)[2][0]
        command = _protobuf_fields(bytes(signed))[1][0]
        command_fields = _protobuf_fields(bytes(command))
        if command_fields[1][0] != 1:
            raise ProtocolError("Nordic packet does not contain an init command")
        init = _protobuf_fields(bytes(command_fields[2][0]))
        result = {
            "firmware_number": int(init[1][0]),
            "hardware": int(init[2][0]),
            "application_size": int(init[7][0]),
        }
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ProtocolError("Nordic init packet is missing an expected field") from exc
    return result


def semantic_firmware_number(version: str) -> int:
    import re

    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise ValueError("firmware version must be MAJOR.MINOR.PATCH")
    major, minor, patch = map(int, match.groups())
    if minor > 99 or patch > 99:
        raise ValueError("minor and patch must be at most 99 for Nordic DFU")
    return major * 10000 + minor * 100 + patch


def parse_nordic_dfu_response(data: bytes, request_opcode: int) -> bytes:
    if len(data) < 3 or data[0] != 0x60 or data[1] != request_opcode:
        raise ProtocolError(f"unexpected Nordic DFU response to 0x{request_opcode:02X}: {data.hex()}")
    if data[2] != 1:
        extra = f", extended 0x{data[3]:02X}" if data[2] == 11 and len(data) > 3 else ""
        raise ProtocolError(f"Nordic DFU status 0x{data[2]:02X}{extra} on opcode 0x{request_opcode:02X}")
    return data[3:]


@dataclasses.dataclass(frozen=True)
class NordicFirmwarePackage:
    sha256: str
    init_packet: bytes
    image: bytes
    firmware_number: int
    hardware: int


def load_nordic_firmware_package(raw: bytes) -> NordicFirmwarePackage:
    """Parse a signed Nordic Secure DFU ``.zip``/``.sfu`` package already read into memory."""
    import hashlib
    import io
    import json
    import zipfile

    digest = hashlib.sha256(raw).hexdigest()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            application = manifest["manifest"]["application"]
            bin_name = application["bin_file"]
            dat_name = application["dat_file"]
            import posixpath

            if posixpath.basename(bin_name) != bin_name or posixpath.basename(dat_name) != dat_name:
                raise ProtocolError("DFU manifest references unsafe file paths")
            image = archive.read(bin_name)
            init_packet = archive.read(dat_name)
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid Nordic DFU package: {exc}") from exc
    if not image or not init_packet:
        raise ProtocolError("DFU package contains an empty image or init packet")
    metadata = parse_nordic_init_packet(init_packet)
    if metadata["application_size"] != len(image):
        raise ProtocolError(
            f"init packet expects {metadata['application_size']} image bytes, package contains {len(image)}"
        )
    return NordicFirmwarePackage(
        sha256=digest,
        init_packet=init_packet,
        image=image,
        firmware_number=metadata["firmware_number"],
        hardware=metadata["hardware"],
    )


def increment_ble_address(address: str) -> str:
    parts = address.upper().split(":")
    if len(parts) != 6:
        raise ValueError(f"not a Bluetooth MAC address: {address}")
    try:
        value = (int(parts[-1], 16) + 1) & 0xFF
    except ValueError as exc:
        raise ValueError(f"not a Bluetooth MAC address: {address}") from exc
    return ":".join(parts[:-1] + [f"{value:02X}"])
