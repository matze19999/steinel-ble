"""Steinel proprietary direct-GATT framing and Global Reset."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import STEINEL_DIRECT_RX, STEINEL_DIRECT_TX

_LOGGER = logging.getLogger(__name__)

GLOBAL_RESET_OPCODE = 0xE5
GLOBAL_RESET_DATA = bytes.fromhex("55 AA A5 5A")


def crc16_steinel(data: bytes) -> int:
    """Calculate CRC-16/SPI-FUJITSU used by the direct channel."""
    crc = 0x1D0F
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    """Encode a zero-free COBS payload."""
    output = bytearray((0,))
    code_index = 0
    code = 1
    for byte in data:
        if byte == 0:
            output[code_index] = code
            code_index = len(output)
            output.append(0)
            code = 1
        else:
            output.append(byte)
            code += 1
            if code == 0xFF:
                output[code_index] = code
                code_index = len(output)
                output.append(0)
                code = 1
    output[code_index] = code
    return bytes(output)


def cobs_decode(data: bytes) -> bytes:
    """Decode a COBS payload."""
    output = bytearray()
    index = 0
    while index < len(data):
        code = data[index]
        if code == 0:
            raise ValueError("Zero byte inside COBS payload")
        index += 1
        end = index + code - 1
        if end > len(data):
            raise ValueError("Truncated COBS payload")
        output.extend(data[index:end])
        index = end
        if code != 0xFF and index < len(data):
            output.append(0)
    return bytes(output)


def encode_direct_frame(opcode: int, data: bytes = b"") -> bytes:
    """Build a complete COBS/NUL-delimited direct-channel frame."""
    body = bytes((opcode,)) + data
    raw = body + crc16_steinel(body).to_bytes(2, "little")
    return cobs_encode(raw) + b"\x00"


class DirectResetClient:
    """Send a key-independent Global Reset over the Steinel GATT service."""

    def __init__(self, device: BLEDevice, name: str) -> None:
        self.device = device
        self.name = name
        self.client: BleakClientWithServiceCache | None = None
        self._response = asyncio.Event()
        self._buffer = bytearray()

    async def connect(self) -> None:
        """Connect, refresh stale services once, and subscribe."""
        self.client = await establish_connection(
            BleakClientWithServiceCache,
            self.device,
            self.name,
            max_attempts=3,
        )
        if self.client.services.get_characteristic(STEINEL_DIRECT_TX) is None:
            await self.client.clear_cache()
            await self.client.disconnect()
            self.client = await establish_connection(
                BleakClientWithServiceCache,
                self.device,
                self.name,
                max_attempts=3,
            )
        if self.client.services.get_characteristic(STEINEL_DIRECT_TX) is None:
            raise ConnectionError("Steinel direct GATT service is unavailable")
        await self.client.start_notify(STEINEL_DIRECT_RX, self._notification)

    def _notification(self, _sender: object, data: bytearray) -> None:
        self._buffer.extend(data)
        while 0 in self._buffer:
            end = self._buffer.index(0)
            encoded = bytes(self._buffer[:end])
            del self._buffer[: end + 1]
            if not encoded:
                continue
            with contextlib.suppress(ValueError):
                raw = cobs_decode(encoded)
                if (
                    len(raw) >= 4
                    and raw[0] == GLOBAL_RESET_OPCODE
                    and int.from_bytes(raw[-2:], "little") == crc16_steinel(raw[:-2])
                ):
                    self._response.set()

    async def reset(self, response_timeout: float = 8) -> None:
        """Write Global Reset; a missing response is valid during reboot."""
        if self.client is None:
            raise ConnectionError("Steinel direct client is not connected")
        characteristic = self.client.services.get_characteristic(STEINEL_DIRECT_TX)
        if characteristic is None:
            raise ConnectionError("Steinel direct TX characteristic is unavailable")
        await self.client.write_gatt_char(
            characteristic,
            encode_direct_frame(GLOBAL_RESET_OPCODE, GLOBAL_RESET_DATA),
            response=False,
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._response.wait(), response_timeout)
        with contextlib.suppress(Exception):
            await self.client.clear_cache()

    async def disconnect(self) -> None:
        """Release the Bluetooth connection slot."""
        if self.client is None:
            return
        with contextlib.suppress(Exception):
            if self.client.is_connected:
                await self.client.stop_notify(STEINEL_DIRECT_RX)
        with contextlib.suppress(Exception):
            await self.client.disconnect()
        self.client = None
