"""Tests for Steinel Sensor Extension value decoding."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_protocol():
    path = (
        Path(__file__).parents[1] / "custom_components/steinel_ble/sensor_protocol.py"
    )
    spec = importlib.util.spec_from_file_location("steinel_test_sensor_protocol", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


protocol = _load_protocol()


def test_environmental_property_decoding() -> None:
    """Decode standard temperature, humidity, CO2, and pressure values."""
    assert protocol.decode_sensor_value("temperature", b"\x2a").value == 21
    assert protocol.decode_sensor_value("precise_temperature", b"\x34\x08").value == 21
    assert protocol.decode_sensor_value("humidity", b"\x88\x13").value == 50
    assert protocol.decode_sensor_value("co2", b"\x20\x03").value == 800
    pressure = protocol.decode_sensor_value("air_pressure", b"\x10\x27\x0f\x00")
    assert pressure.value == 99304


def test_presence_and_unknown_values() -> None:
    """Presence is boolean and malformed values remain unavailable."""
    assert protocol.decode_sensor_value("presence", b"\x01").value is True
    assert protocol.decode_sensor_value("motion", b"\x01").value is True
    assert protocol.decode_sensor_value("motion", b"\x00").value is False
    assert protocol.decode_sensor_value("illuminance", b"\x10\x27\x00").value == 100
    assert protocol.decode_sensor_value("humidity", b"\x01").value is None
    assert protocol.decode_sensor_value("vendor", b"\xaa\xbb").raw == b"\xaa\xbb"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"\x40\x08\x00", {0x0042: b"\x00"}),
        (b"\x40\x08\xc8", {0x0042: b"\xc8"}),
        (b"\x01\x42\x00\x00", {0x0042: b"\x00"}),
        (b"\xff\x42\x00", {0x0042: b""}),
        (b"\x40\x08\x00\xa0\x09\x01", {0x0042: b"\x00", 0x004D: b"\x01"}),
        (b"\x03\x82\x00\x12\x34", {0x0082: b"\x12\x34"}),
        (b"", {}),
    ],
)
def test_marshalled_sensor_records(payload, expected):
    """Decode the Format A/B headers used by the app's SensorExtDataStatus."""
    assert protocol.decode_sensor_status(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        b"\x40",  # Truncated Format A header.
        b"\x01\x42",  # Truncated Format B header.
        b"\x40\x08",  # Missing Format A value.
        b"\x03\x42\x00\x01",  # Two-byte Format B value truncated to one.
        b"\x40\x08\x00\x01",  # Valid motion record, malformed trailing header.
    ],
)
def test_truncated_sensor_records_are_rejected(payload):
    with pytest.raises(ValueError, match="Truncated"):
        protocol.decode_sensor_status(payload)


@pytest.mark.parametrize("raw", [b"\xc9", b"\xfe", b"\xff", b"", b"\x01\x00"])
def test_unknown_or_malformed_motion_is_not_detected(raw):
    result = protocol.decode_sensor_value("motion", raw)
    assert result.value is None
    assert result.raw == raw


@pytest.mark.parametrize("raw", [b"\x02", b"\xff", b"", b"\x01\x00"])
def test_non_boolean_presence_is_not_detected(raw):
    assert protocol.decode_sensor_value("presence", raw).value is None
