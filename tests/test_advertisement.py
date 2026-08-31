"""Tests for STEINEL manufacturer data decoding."""

import importlib.util
from pathlib import Path

_PATH = Path(__file__).parents[1] / "custom_components/steinel_ble/advertisement.py"
_SPEC = importlib.util.spec_from_file_location("steinel_test_advertisement", _PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
parse_steinel_advertisement = _MODULE.parse_steinel_advertisement


def test_regular_identity() -> None:
    data = bytes.fromhex("861e01010007016a74")
    assert parse_steinel_advertisement(data, "L 845 C") == {
        "product_id": 7814,
        "firmware": "1.1.30",
        "bootloader": 7,
        "hardware": 1,
        "hash_id": 29802,
    }


def test_short_identity_is_ignored() -> None:
    assert parse_steinel_advertisement(b"\x86\x1e\x01") == {}


def test_bootloader_identity() -> None:
    assert parse_steinel_advertisement(bytes.fromhex("861e00076a74"), "SfuTg") == {
        "product_id": 7814,
        "bootloader": 7,
        "hash_id": 29802,
        "hardware": 116,
    }
