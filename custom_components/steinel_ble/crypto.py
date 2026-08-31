"""Bluetooth Mesh cryptographic primitives.

The functions in this module follow Mesh Protocol 1.1. Multi-octet protocol
fields are supplied in their on-air byte order.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.cmac import CMAC


def aes_cmac(key: bytes, data: bytes) -> bytes:
    """Calculate AES-CMAC."""
    mac = CMAC(algorithms.AES(key))
    mac.update(data)
    return mac.finalize()


def s1(data: bytes) -> bytes:
    """Mesh s1 salt generation function."""
    return aes_cmac(bytes(16), data)


def k1(n: bytes, salt: bytes, info: bytes) -> bytes:
    """Mesh k1 derivation function."""
    return aes_cmac(aes_cmac(salt, n), info)


def k2(n: bytes, p: bytes = b"\x00") -> tuple[int, bytes, bytes]:
    """Derive NID, encryption key and privacy key from a NetKey."""
    t = aes_cmac(s1(b"smk2"), n)
    t1 = aes_cmac(t, p + b"\x01")
    t2 = aes_cmac(t, t1 + p + b"\x02")
    t3 = aes_cmac(t, t2 + p + b"\x03")
    material = int.from_bytes(t1 + t2 + t3, "big") & ((1 << 263) - 1)
    raw = material.to_bytes(33, "big")
    return raw[0] & 0x7F, raw[1:17], raw[17:33]


def k3(n: bytes) -> bytes:
    """Derive an eight-octet network ID."""
    t = aes_cmac(s1(b"smk3"), n)
    return aes_cmac(t, b"id64\x01")[-8:]


def k4(n: bytes) -> int:
    """Derive a six-bit application key identifier."""
    t = aes_cmac(s1(b"smk4"), n)
    return aes_cmac(t, b"id6\x01")[-1] & 0x3F


def aes_ecb(key: bytes, block: bytes) -> bytes:
    """Encrypt one AES block in ECB mode."""
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(block) + encryptor.finalize()


def provisioning_encrypt(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """Encrypt provisioning data and append its 64-bit MIC."""
    return AESCCM(key, tag_length=8).encrypt(nonce, data, None)


def provisioning_decrypt(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """Decrypt provisioning data."""
    return AESCCM(key, tag_length=8).decrypt(nonce, data, None)


@dataclass(frozen=True)
class NetworkKeys:
    """Material derived from a network key."""

    nid: int
    encryption_key: bytes
    privacy_key: bytes

    @classmethod
    def derive(cls, net_key: bytes) -> NetworkKeys:
        """Derive network credentials."""
        return cls(*k2(net_key))


def mesh_nonce(
    nonce_type: int,
    seq: int,
    src: int,
    dst: int,
    iv_index: int,
    *,
    ctl_ttl: int = 0,
    aszmic: int = 0,
) -> bytes:
    """Create a 13-octet Bluetooth Mesh nonce."""
    if nonce_type == 0x00:
        second = ctl_ttl
    elif nonce_type in (0x01, 0x02):
        second = (aszmic & 1) << 7
    else:
        second = 0
    return (
        bytes((nonce_type, second))
        + seq.to_bytes(3, "big")
        + src.to_bytes(2, "big")
        + dst.to_bytes(2, "big")
        + iv_index.to_bytes(4, "big")
    )


def upper_transport_encrypt(
    key: bytes,
    nonce_type: int,
    seq: int,
    src: int,
    dst: int,
    iv_index: int,
    access_payload: bytes,
    *,
    szmic: int = 0,
) -> bytes:
    """Encrypt an access payload for upper transport."""
    nonce = mesh_nonce(nonce_type, seq, src, dst, iv_index, aszmic=szmic)
    return AESCCM(key, tag_length=8 if szmic else 4).encrypt(
        nonce, access_payload, None
    )


def upper_transport_decrypt(
    key: bytes,
    nonce_type: int,
    seq: int,
    src: int,
    dst: int,
    iv_index: int,
    payload: bytes,
    *,
    szmic: int = 0,
) -> bytes:
    """Decrypt an upper transport access payload."""
    nonce = mesh_nonce(nonce_type, seq, src, dst, iv_index, aszmic=szmic)
    return AESCCM(key, tag_length=8 if szmic else 4).decrypt(nonce, payload, None)


def network_encrypt(
    keys: NetworkKeys,
    iv_index: int,
    ctl_ttl: int,
    seq: int,
    src: int,
    dst: int,
    lower_transport: bytes,
    *,
    nonce_type: int = 0x00,
) -> bytes:
    """Encrypt and obfuscate a network PDU."""
    nonce = mesh_nonce(nonce_type, seq, src, 0, iv_index, ctl_ttl=ctl_ttl)
    plain = dst.to_bytes(2, "big") + lower_transport
    encrypted = AESCCM(
        keys.encryption_key, tag_length=8 if ctl_ttl & 0x80 else 4
    ).encrypt(nonce, plain, None)
    privacy_plain = b"\x00" * 5 + iv_index.to_bytes(4, "big") + encrypted[:7]
    pecb = aes_ecb(keys.privacy_key, privacy_plain)
    header = bytes((ctl_ttl,)) + seq.to_bytes(3, "big") + src.to_bytes(2, "big")
    obfuscated = bytes(a ^ b for a, b in zip(header, pecb[:6], strict=True))
    return bytes((((iv_index & 1) << 7) | keys.nid,)) + obfuscated + encrypted


@dataclass(frozen=True)
class DecryptedNetworkPDU:
    """Decoded fields from a network PDU."""

    ctl: bool
    ttl: int
    seq: int
    src: int
    dst: int
    lower_transport: bytes


def network_decrypt(
    keys: NetworkKeys, iv_index: int, pdu: bytes
) -> DecryptedNetworkPDU:
    """Deobfuscate and decrypt a network PDU."""
    if len(pdu) < 14 or pdu[0] & 0x7F != keys.nid:
        raise ValueError("Network PDU does not use this NetKey")
    encrypted = pdu[7:]
    privacy_plain = b"\x00" * 5 + iv_index.to_bytes(4, "big") + encrypted[:7]
    pecb = aes_ecb(keys.privacy_key, privacy_plain)
    header = bytes(a ^ b for a, b in zip(pdu[1:7], pecb[:6], strict=True))
    ctl_ttl = header[0]
    seq = int.from_bytes(header[1:4], "big")
    src = int.from_bytes(header[4:6], "big")
    nonce = mesh_nonce(0x00, seq, src, 0, iv_index, ctl_ttl=ctl_ttl)
    plain = AESCCM(keys.encryption_key, tag_length=8 if ctl_ttl & 0x80 else 4).decrypt(
        nonce, encrypted, None
    )
    return DecryptedNetworkPDU(
        bool(ctl_ttl & 0x80),
        ctl_ttl & 0x7F,
        seq,
        src,
        int.from_bytes(plain[:2], "big"),
        plain[2:],
    )
