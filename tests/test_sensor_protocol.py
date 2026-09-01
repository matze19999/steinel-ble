"""Tests for Steinel Sensor Extension value decoding."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


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
    assert protocol.strip_property_prefix(0x0042, b"\x42\x00\x01") == b"\x01"
    assert protocol.decode_sensor_value("humidity", b"\x01").value is None
    assert protocol.decode_sensor_value("vendor", b"\xaa\xbb").raw == b"\xaa\xbb"
