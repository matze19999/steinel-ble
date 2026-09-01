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


def strip_property_prefix(property_id: int, data: bytes) -> bytes:
    """Remove the echoed property ID used by STEINEL vendor responses."""
    prefix = property_id.to_bytes(2, "little")
    return data[2:] if data.startswith(prefix) else data


def decode_sensor_value(name: str, data: bytes) -> SensorValue:
    """Decode Bluetooth SIG Device Property characteristic encodings."""
    if name in ("presence", "motion") and data:
        return SensorValue(bool(data[0]), data)
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
