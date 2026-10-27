"""Steinel Sensor Extension property definitions and decoders."""

from __future__ import annotations

from dataclasses import dataclass

SENSOR_PROPERTIES: dict[str, int] = {
    "motion": 0x0042,
    "people_count": 0x004C,
    "presence": 0x004D,
    "illuminance": 0x004E,
    "temperature": 0x004F,
    "time_since_motion": 0x0068,
    "time_since_presence": 0x0069,
    "humidity": 0x0076,
    "precise_temperature": 0x0075,
    "co2": 0x0077,
    "voc": 0x0078,
    "noise": 0x0079,
    "air_pressure": 0x0082,
    "dew_point": 0x0087,
}


@dataclass(frozen=True)
class SensorValue:
    """A decoded standard property value."""

    value: float | int | bool | None
    raw: bytes


def decode_sensor_status(data: bytes) -> dict[int, bytes]:
    """Unpack Sensor (Extension) Data Status MPIDs and their raw values.

    STEINEL's D1 status uses the same Format A/B headers as SIG Sensor Status,
    rather than a plain property-ID prefix. Parse the entire message before
    returning so a truncated trailing record cannot partially update a sensor.
    """
    values: dict[int, bytes] = {}
    offset = 0
    while offset < len(data):
        first = data[offset]
        header_size = 3 if first & 1 else 2
        if len(data) - offset < header_size:
            raise ValueError("Truncated sensor property header")
        if first & 1:
            length = first >> 1
            length = 0 if length == 0x7F else length + 1
            property_id = int.from_bytes(data[offset + 1 : offset + 3], "little")
        else:
            header = int.from_bytes(data[offset : offset + 2], "little")
            length = ((header >> 1) & 0x0F) + 1
            property_id = header >> 5
        offset += header_size
        end = offset + length
        if end > len(data):
            raise ValueError("Truncated sensor property value")
        values[property_id] = data[offset:end]
        offset = end
    return values


def decode_sensor_value(name: str, data: bytes) -> SensorValue:
    """Decode Bluetooth SIG Device Property characteristic encodings."""
    if name == "motion":
        # Motion Sensed is Percentage 8 (0..200 = 0..100%), not a Boolean.
        # 0xFF means unknown; 201..254 are prohibited.
        valid = len(data) == 1 and data[0] <= 200
        return SensorValue(bool(data[0]) if valid else None, data)
    if name == "presence":
        valid = len(data) == 1 and data[0] in (0, 1)
        return SensorValue(bool(data[0]) if valid else None, data)
    if name == "noise" and data:
        return SensorValue(data[0], data)
    if name == "illuminance" and len(data) >= 3:
        raw = int.from_bytes(data[:3], "little")
        return SensorValue(None if raw == 0xFFFFFF else raw * 0.01, data)
    if name in ("temperature", "dew_point") and data:
        value = int.from_bytes(data[:1], "little", signed=True)
        return SensorValue(None if data[0] == 0x7F else value * 0.5, data)
    if name == "precise_temperature" and len(data) >= 2:
        return SensorValue(int.from_bytes(data[:2], "little", signed=True) * 0.01, data)
    if name == "humidity" and len(data) >= 2:
        return SensorValue(int.from_bytes(data[:2], "little") * 0.01, data)
    if (
        name
        in (
            "co2",
            "voc",
            "people_count",
            "time_since_motion",
            "time_since_presence",
        )
        and len(data) >= 2
    ):
        return SensorValue(int.from_bytes(data[:2], "little"), data)
    if name == "air_pressure" and len(data) >= 4:
        return SensorValue(int.from_bytes(data[:4], "little") * 0.1, data)
    return SensorValue(None, data)
