"""Tests for Bluetooth Mesh cryptographic primitives."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_crypto():
    path = Path(__file__).parents[1] / "custom_components/steinel_ble/crypto.py"
    spec = importlib.util.spec_from_file_location("steinel_test_crypto", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


crypto = _load_crypto()


def test_mesh_key_derivation_vector() -> None:
    """Validate k2, k3 and k4 against the Mesh Protocol sample NetKey."""
    net_key = bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6")
    nid, encryption_key, privacy_key = crypto.k2(net_key)
    assert nid == 0x68
    assert encryption_key.hex() == "0953fa93e7caac9638f58820220a398e"
    assert privacy_key.hex() == "8b84eedec100067d670971dd2aa700cf"
    assert crypto.k3(net_key).hex() == "3ecaff672f673370"
    assert crypto.k4(net_key) == 0x1D


def test_network_pdu_round_trip() -> None:
    """Network encryption must preserve and authenticate every header field."""
    keys = crypto.NetworkKeys.derive(bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6"))
    pdu = crypto.network_encrypt(
        keys,
        0x12345678,
        5,
        0x010203,
        0x0001,
        0x1201,
        b"lower transport payload",
    )
    decoded = crypto.network_decrypt(keys, 0x12345678, pdu)
    assert decoded.ctl is False
    assert decoded.ttl == 5
    assert decoded.seq == 0x010203
    assert decoded.src == 0x0001
    assert decoded.dst == 0x1201
    assert decoded.lower_transport == b"lower transport payload"


def test_upper_transport_round_trip() -> None:
    """Application nonce encryption must produce a valid TransMIC."""
    key = bytes.fromhex("63964771734fbd76e3b40519d1d94a48")
    encrypted = crypto.upper_transport_encrypt(
        key, 1, 7, 1, 0x0100, 0, b"\x82\x02\x01\x22"
    )
    assert (
        crypto.upper_transport_decrypt(key, 1, 7, 1, 0x0100, 0, encrypted)
        == b"\x82\x02\x01\x22"
    )
