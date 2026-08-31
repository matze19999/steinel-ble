"""PB-GATT provisioning for Steinel Bluetooth Mesh devices."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import ec

from .crypto import aes_cmac, k1, provisioning_encrypt, s1
from .gatt import MeshGattTransport

PROVISIONING_PDU_TYPE = 0x03


class ProvisioningError(Exception):
    """Raised when a device rejects or violates provisioning."""


@dataclass(frozen=True)
class ProvisioningResult:
    """Keys and addressing resulting from provisioning."""

    device_key: bytes
    element_count: int


PersistPending = Callable[[bytes, int], Awaitable[None]]


class Provisioner:
    """Implement the standard No-OOB and Static-OOB PB-GATT procedure."""

    def __init__(self, transport: MeshGattTransport) -> None:
        self.transport = transport
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def handle_pdu(self, pdu_type: int, payload: bytes) -> None:
        """Receive a reassembled Provisioning PDU."""
        if pdu_type == PROVISIONING_PDU_TYPE:
            await self._queue.put(payload)

    async def _send(self, opcode: int, parameters: bytes = b"") -> None:
        await self.transport.send(PROVISIONING_PDU_TYPE, bytes((opcode,)) + parameters)

    async def _receive(self, opcode: int, timeout: float = 20) -> bytes:
        while True:
            try:
                pdu = await asyncio.wait_for(self._queue.get(), timeout)
            except TimeoutError as err:
                raise ProvisioningError(
                    f"Timed out waiting for provisioning opcode 0x{opcode:02X}"
                ) from err
            if not pdu:
                continue
            if pdu[0] == 0x09:
                reason = pdu[1] if len(pdu) > 1 else -1
                raise ProvisioningError(
                    f"Device reported provisioning failure {reason}"
                )
            if pdu[0] == opcode:
                return pdu[1:]

    async def provision(
        self,
        net_key: bytes,
        net_key_index: int,
        iv_index: int,
        unicast_address: int,
        static_oob: bytes | None,
        persist_pending: PersistPending,
    ) -> ProvisioningResult:
        """Provision the connected device and return its Device Key."""
        invite = b"\x00"
        await self._send(0x00, invite)
        capabilities = await self._receive(0x01)
        if len(capabilities) != 11:
            raise ProvisioningError("Invalid Provisioning Capabilities length")
        elements = capabilities[0]
        algorithms = int.from_bytes(capabilities[1:3], "big")
        if not elements or not algorithms & 1:
            raise ProvisioningError("Device does not support P-256 provisioning")
        if static_oob is not None and not capabilities[3]:
            raise ProvisioningError("Device does not support Static OOB")

        start = b"\x00\x00" + (b"\x01\x00\x00" if static_oob else b"\x00\x00\x00")
        await self._send(0x02, start)

        private_key = ec.generate_private_key(ec.SECP256R1())
        public_numbers = private_key.public_key().public_numbers()
        local_public = public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(
            32, "big"
        )
        await self._send(0x03, local_public)
        remote_public = await self._receive(0x03)
        if len(remote_public) != 64:
            raise ProvisioningError("Invalid device public key")
        try:
            peer = ec.EllipticCurvePublicNumbers(
                int.from_bytes(remote_public[:32], "big"),
                int.from_bytes(remote_public[32:], "big"),
                ec.SECP256R1(),
            ).public_key()
            shared_secret = private_key.exchange(ec.ECDH(), peer)
        except ValueError as err:
            raise ProvisioningError(
                "Device supplied an invalid P-256 public key"
            ) from err

        confirmation_inputs = (
            invite + capabilities + start + local_public + remote_public
        )
        confirmation_salt = s1(confirmation_inputs)
        confirmation_key = k1(shared_secret, confirmation_salt, b"prck")
        auth_value = static_oob if static_oob is not None else bytes(16)
        local_random = os.urandom(16)
        local_confirmation = aes_cmac(confirmation_key, local_random + auth_value)
        await self._send(0x05, local_confirmation)
        remote_confirmation = await self._receive(0x05)
        await self._send(0x06, local_random)
        remote_random = await self._receive(0x06)
        if len(remote_random) != 16 or remote_confirmation != aes_cmac(
            confirmation_key, remote_random + auth_value
        ):
            raise ProvisioningError("Device confirmation did not verify")

        provisioning_salt = s1(confirmation_salt + local_random + remote_random)
        session_key = k1(shared_secret, provisioning_salt, b"prsk")
        session_nonce = k1(shared_secret, provisioning_salt, b"prsn")[3:]
        device_key = k1(shared_secret, provisioning_salt, b"prdk")
        data = (
            net_key
            + net_key_index.to_bytes(2, "big")
            + b"\x00"
            + iv_index.to_bytes(4, "big")
            + unicast_address.to_bytes(2, "big")
        )

        # Losing Provisioning Complete after the device accepts Data must not
        # lose the only copy of its freshly derived Device Key.
        await persist_pending(device_key, elements)
        await self._send(0x07, provisioning_encrypt(session_key, session_nonce, data))
        complete = await self._receive(0x08, timeout=30)
        if complete:
            raise ProvisioningError("Invalid Provisioning Complete PDU")
        return ProvisioningResult(device_key, elements)
