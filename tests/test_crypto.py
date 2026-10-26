"""Tests for Bluetooth Mesh cryptographic primitives."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag


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


def test_official_network_pdu_vector() -> None:
    """Check wire bytes independently of our own encrypt/decrypt round trip."""
    keys = crypto.NetworkKeys.derive(bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6"))
    expected = bytes.fromhex("68eca487516765b5e5bfdacbaf6cb7fb6bff871f035444ce83a670df")
    assert (
        crypto.network_encrypt(
            keys,
            0x12345678,
            0x80,
            1,
            0x1201,
            0xFFFD,
            bytes.fromhex("034b50057e400000010000"),
        )
        == expected
    )
    decoded = crypto.network_decrypt(keys, 0x12345678, expected)
    assert decoded.src == 0x1201
    assert decoded.dst == 0xFFFD
    assert decoded.ctl
    assert decoded.lower_transport == bytes.fromhex("034b50057e400000010000")


@pytest.mark.parametrize(
    ("beacon", "index", "updating"),
    [
        ("01003ecaff672f673370123456788ea261582f364f6f", 0x12345678, False),
        ("01023ecaff672f67337012345679c2af80ad072a135c", 0x12345679, True),
        ("01003ecaff672f67337012345679c62f09e4c957f59d", 0x12345679, False),
    ],
)
def test_official_secure_beacon_vectors(beacon, index, updating):
    decoder = crypto.MeshBeaconDecoder(
        bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6")
    )
    decoded = decoder.decode(bytes.fromhex(beacon))
    assert decoded.iv_index == index
    assert decoded.iv_update is updating
    assert not decoded.key_refresh


@pytest.mark.parametrize("offset", [0, 1, 2, 10, 14, 21])
def test_modified_beacons_are_rejected(offset):
    decoder = crypto.MeshBeaconDecoder(
        bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6")
    )
    beacon = bytearray.fromhex("01003ecaff672f673370123456788ea261582f364f6f")
    beacon[offset] ^= 1
    with pytest.raises((ValueError, InvalidTag)):
        decoder.decode(bytes(beacon))


def test_beacon_from_another_network_is_rejected():
    decoder = crypto.MeshBeaconDecoder(bytes(16))
    with pytest.raises(ValueError):
        decoder.decode(bytes.fromhex("01003ecaff672f673370123456788ea261582f364f6f"))


@pytest.mark.parametrize(
    ("key", "beacon", "index", "updating"),
    [
        (
            "f7a2a44f8e8a8029064f173ddc1e2b00",
            "02435f18f85cf78a3121f58478a561e488e7cbf3174f022a514741",
            0x1010ABCD,
            True,
        ),
        (
            "3bbb6f1fbd53e157417f308ce7aec58f",
            "021b998f82927535ea6f3076f422ce827408ab2f0ffb94cf97f881",
            0,
            False,
        ),
    ],
)
def test_official_private_beacon_vectors(key, beacon, index, updating):
    decoder = crypto.MeshBeaconDecoder(bytes.fromhex(key))
    decoded = decoder.decode(bytes.fromhex(beacon))
    assert decoded.iv_index == index
    assert decoded.iv_update is updating
    damaged = bytes.fromhex(beacon[:-2] + "00")
    with pytest.raises(InvalidTag):
        decoder.decode(damaged)
