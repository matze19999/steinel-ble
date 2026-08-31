"""GATT transport for Mesh Provisioning and Mesh Proxy PDUs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

_LOGGER = logging.getLogger(__name__)

PDUHandler = Callable[[int, bytes], Awaitable[None]]
DisconnectHandler = Callable[[], None]


class MeshGattTransport:
    """Short-lived, proxy-compatible GATT transport."""

    def __init__(
        self,
        device: BLEDevice,
        name: str,
        data_in: str,
        data_out: str,
        handler: PDUHandler,
        disconnected: DisconnectHandler | None = None,
    ) -> None:
        self.device = device
        self.name = name
        self.data_in = data_in
        self.data_out = data_out
        self.handler = handler
        self.disconnected = disconnected
        self.client: BleakClientWithServiceCache | None = None
        self._rx_type: int | None = None
        self._rx = bytearray()
        self._tasks: set[asyncio.Task[None]] = set()

    async def connect(self, *, subscribe: bool = True) -> None:
        """Connect and optionally subscribe to the Data Out characteristic."""
        self.client = await establish_connection(
            BleakClientWithServiceCache,
            self.device,
            self.name,
            max_attempts=3,
        )
        if self.disconnected:
            self.client.set_disconnected_callback(
                lambda _client: self.disconnected and self.disconnected()
            )
        if subscribe:
            await self.subscribe()

    async def subscribe(self) -> None:
        """Subscribe after the live GATT role has been verified."""
        if self.client is None:
            raise ConnectionError("Mesh GATT transport is not connected")
        await self.client.start_notify(self.data_out, self._notification)

    async def disconnect(self) -> None:
        """Unsubscribe and release the Bluetooth connection slot."""
        client, self.client = self.client, None
        if client is None:
            return
        try:
            if client.is_connected and client.services.get_characteristic(
                self.data_out
            ):
                await client.stop_notify(self.data_out)
        except Exception:  # best effort during disconnect
            _LOGGER.debug("Unable to stop Mesh notification", exc_info=True)
        finally:
            await client.disconnect()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _notification(self, _sender: object, value: bytearray) -> None:
        if not value:
            return
        sar, pdu_type = value[0] >> 6, value[0] & 0x3F
        if sar == 0:
            payload = bytes(value[1:])
        elif sar == 1:
            self._rx_type = pdu_type
            self._rx = bytearray(value[1:])
            return
        elif sar == 2:
            if self._rx_type != pdu_type:
                self._reset_rx()
                return
            self._rx.extend(value[1:])
            return
        else:
            if self._rx_type != pdu_type:
                self._reset_rx()
                return
            self._rx.extend(value[1:])
            payload = bytes(self._rx)
            self._reset_rx()
        task = asyncio.create_task(self.handler(pdu_type, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _reset_rx(self) -> None:
        self._rx_type = None
        self._rx.clear()

    async def send(self, pdu_type: int, payload: bytes) -> None:
        """Segment and send a Proxy Protocol PDU."""
        if self.client is None or not self.client.is_connected:
            raise ConnectionError("Mesh GATT transport is not connected")
        characteristic = self.client.services.get_characteristic(self.data_in)
        if characteristic is None:
            raise ConnectionError(f"GATT characteristic {self.data_in} is unavailable")
        capacity = max(1, characteristic.max_write_without_response_size - 1)
        parts = [
            payload[pos : pos + capacity] for pos in range(0, len(payload), capacity)
        ]
        if not parts:
            parts = [b""]
        for index, part in enumerate(parts):
            if len(parts) == 1:
                sar = 0
            elif index == 0:
                sar = 1
            elif index == len(parts) - 1:
                sar = 3
            else:
                sar = 2
            await self.client.write_gatt_char(
                characteristic, bytes(((sar << 6) | pdu_type,)) + part, response=False
            )

    def has_service(self, uuid: str) -> bool:
        """Return whether live service resolution contains a UUID."""
        return bool(self.client and self.client.services.get_service(uuid))

    async def clear_cache(self) -> bool:
        """Clear a stale local or remote GATT service cache when supported."""
        client = self.client
        if client is None:
            return False
        clear = getattr(client, "clear_cache", None)
        if clear is None:
            return False
        result = await clear()
        return bool(result)
