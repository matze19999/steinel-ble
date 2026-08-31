"""Tests for the proprietary Steinel direct-GATT framing."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def _load_direct():
    root = Path(__file__).parents[1] / "custom_components/steinel_ble"
    package_name = "steinel_test_direct_package"
    package = types.ModuleType(package_name)
    package.__path__ = [str(root)]
    sys.modules[package_name] = package
    for module_name in ("const", "direct"):
        name = f"{package_name}.{module_name}"
        spec = importlib.util.spec_from_file_location(name, root / f"{module_name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{package_name}.direct"]


direct = _load_direct()


def test_global_reset_frame() -> None:
    """Global Reset must match the byte-exact validated wire frame."""
    frame = direct.encode_direct_frame(
        direct.GLOBAL_RESET_OPCODE, direct.GLOBAL_RESET_DATA
    )
    assert frame.hex() == "08e555aaa55a824300"


def test_cobs_round_trip() -> None:
    """COBS preserves embedded zero bytes."""
    raw = bytes.fromhex("17 01 0a 00 61 9c")
    assert direct.cobs_decode(direct.cobs_encode(raw)) == raw
