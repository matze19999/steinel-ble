"""Bluetooth connection handling via Home Assistant's bluetooth integration.

All connections are resolved through ``homeassistant.components.bluetooth``
and established with ``bleak_retry_connector``. This is the standard pattern
used by Home Assistant Bluetooth integrations and is what makes everything
in this component work transparently over local adapters *and* remote
ESPHome/HA Bluetooth proxies: the ``BLEDevice`` handed back by
``bluetooth.async_ble_device_from_address`` already points at whichever
adapter/proxy currently has the best view of the device, and
``establish_connection`` retries through proxy hiccups (busy slots,
disconnects) automatically.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any, Self

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback

from .const import (
    DEFAULT_CONNECT_TIMEOUT,
    MESH_PROVISIONING_IN,
    MESH_PROVISIONING_OUT,
    MESH_PROXY_IN,
    MESH_PROXY_OUT,
    NORDIC_DFU_BUTTONLESS,
    NORDIC_DFU_CONTROL,
    NORDIC_DFU_PACKET,
    STEINEL_RX,
    STEINEL_TX,
)
from .protocol import (
    DirectFrameStream,
    DirectResponse,
    ProtocolError,
    ProxySarReceiver,
    encode_direct_frame,
    parse_nordic_dfu_response,
    proxy_segments,
    response_from_packet,
)

_LOGGER = logging.getLogger(__name__)


class BleNotAvailable(ProtocolError):
    """No local adapter or Bluetooth proxy currently has a route to this device."""


def async_ble_device(hass: HomeAssistant, address: str) -> BLEDevice:
    device = bluetooth.async_ble_device_from_address(hass, address.upper(), connectable=True)
    if device is None:
        raise BleNotAvailable(
            f"{address} is not currently visible to any local Bluetooth adapter or "
            "Home Assistant Bluetooth proxy"
        )
    return device


async def async_connect(
    hass: HomeAssistant,
    address: str,
    name: str = "",
    timeout: float = DEFAULT_CONNECT_TIMEOUT,
    disconnected_callback: Callable[[BleakClientWithServiceCache], None] | None = None,
) -> BleakClientWithServiceCache:
    # use_services_cache=False: a STEINEL lamp's GATT table changes with its
    # Bluetooth Mesh role (Provisioning service before being provisioned,
    # Proxy service after - see MeshTransport), and this integration
    # actively drives that transition (provision, factory reset). Caching
    # is meant for a device whose services are stable across reconnects,
    # which isn't true here; a stale cache surfaces as
    # BleakCharacteristicNotFoundError for a characteristic that is real,
    # just on a service the device isn't currently exposing. Note this only
    # controls bleak's own/BlueZ-side cache - an ESPHome Bluetooth proxy
    # keeps its own separate GATT cache on the ESP32 itself ("Connecting v3
    # with cache" in its logs) that this flag does not reach; if a proxy's
    # cached table also goes stale after a reset, only that proxy device
    # restarting clears it.
    device = async_ble_device(hass, address)
    return await establish_connection(
        BleakClientWithServiceCache,
        device,
        name or address,
        max_attempts=4,
        timeout=timeout,
        use_services_cache=False,
        disconnected_callback=disconnected_callback,
    )


async def async_wait_for_service(
    hass: HomeAssistant, addresses: set[str], service_uuid: str, timeout: float
) -> BLEDevice:
    """Wait until one of ``addresses`` is seen advertising ``service_uuid``.

    Uses Home Assistant's passive Bluetooth advertisement cache/callbacks so
    it transparently picks up advertisements relayed by any Bluetooth proxy,
    without this integration having to scan itself.
    """
    wanted = {addr.upper() for addr in addresses}
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        if info.address.upper() in wanted and service_uuid in info.service_uuids:
            return info.device

    loop = asyncio.get_running_loop()
    found: asyncio.Future[BLEDevice] = loop.create_future()

    @callback
    def _on_update(service_info: bluetooth.BluetoothServiceInfoBleak, change: Any) -> None:
        if service_info.address.upper() in wanted and not found.done():
            found.set_result(service_info.device)

    unregister = bluetooth.async_register_callback(
        hass,
        _on_update,
        bluetooth.BluetoothCallbackMatcher(service_uuid=service_uuid, connectable=True),
        bluetooth.BluetoothScanningMode.ACTIVE,
    )
    try:
        return await asyncio.wait_for(found, timeout)
    except asyncio.TimeoutError as exc:
        raise ProtocolError(
            f"none of {sorted(wanted)} were seen advertising service {service_uuid} within {timeout:g}s"
        ) from exc
    finally:
        unregister()


async def _clear_backend_cache(client: BleakClientWithServiceCache) -> None:
    """Purge the GATT services cache for ``client``'s address.

    ``BleakClientWithServiceCache.clear_cache()`` (bleak_retry_connector)
    forwards to ``BleakClient.clear_cache()``, which the bleak version this
    integration runs against does not implement - it silently logs a
    warning and returns ``False``, so calling it does nothing. The actual
    cache-clearing implementation (the local in-memory cache and, for an
    ESPHome Bluetooth proxy that advertises the CACHE_CLEARING feature, the
    proxy's own on-device cache too) lives on the platform backend
    instance (``client._backend``) instead, so it has to be reached
    directly rather than through that broken public wrapper.
    """
    backend_clear = getattr(getattr(client, "_backend", None), "clear_cache", None)
    if backend_clear is None:
        _LOGGER.info("%s: backend has no clear_cache() to call", getattr(client, "address", "?"))
        return
    try:
        result = await backend_clear()
    except Exception:
        _LOGGER.info("%s: backend clear_cache() raised", getattr(client, "address", "?"), exc_info=True)
    else:
        _LOGGER.info("%s: backend clear_cache() returned %r", getattr(client, "address", "?"), result)


async def _race_disconnect(waitable: Any, disconnected: asyncio.Event, timeout: float, address: str) -> Any:
    """Await ``waitable`` with ``timeout``, but fail fast if ``disconnected``
    fires first instead of blocking for the full timeout.

    Some ESPHome Bluetooth proxies, under RF stress, tear a connection back
    down moments after opening it (observed as "GetServices mid-stream,
    restarting" followed immediately by a disconnect in the proxy's own
    log) without that reaching bleak as a raised exception. Without this,
    a response that will now never arrive is waited for right up to the
    full per-call timeout - which, for this integration's generous
    proxy-friendly timeouts, means minutes of an apparently hung UI for
    what was actually a fast, detectable failure.
    """
    wait_task = asyncio.ensure_future(waitable)
    disconnect_task = asyncio.ensure_future(disconnected.wait())
    try:
        done, _pending = await asyncio.wait(
            {wait_task, disconnect_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if wait_task in done:
            return wait_task.result()
        if disconnect_task in done:
            raise ProtocolError(f"connection to {address} was lost while waiting for a response")
        raise TimeoutError(f"timed out waiting for a response from {address}")
    finally:
        for task in (wait_task, disconnect_task):
            if not task.done():
                task.cancel()


class DirectClient:
    """Connection to the proprietary STEINEL GATT channel (device info,
    identify, firmware update state machine, global reset)."""

    def __init__(self, hass: HomeAssistant, address: str, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        self._hass = hass
        self.address = address
        self.timeout = timeout
        self.client: BleakClientWithServiceCache | None = None
        self._stream = DirectFrameStream()
        self._waiters: dict[int, asyncio.Future[DirectResponse]] = {}
        self.unsolicited: asyncio.Queue[DirectResponse] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._disconnected = asyncio.Event()

    def _on_disconnected(self, _client: BleakClientWithServiceCache) -> None:
        _LOGGER.info("%s: disconnected_callback fired", self.address)
        self._disconnected.set()

    async def __aenter__(self) -> Self:
        self.client = await async_connect(
            self._hass, self.address, "steinel-direct", self.timeout, disconnected_callback=self._on_disconnected
        )
        # A retried connect (establish_connection reuses one client instance
        # across its own internal attempts) can fire disconnected_callback
        # for an earlier failed attempt before finally succeeding; clear
        # that stale signal now that we actually have a live connection.
        self._disconnected.clear()
        await self._ensure_fresh_services(STEINEL_RX)
        await self.client.start_notify(STEINEL_RX, self._notification)
        return self

    async def _ensure_fresh_services(self, required_uuid: str) -> None:
        """Reconnect once with a forced fresh discovery if ``required_uuid``
        is missing from the just-connected client's services.

        A missing characteristic that should exist means the connection
        served a stale GATT table - most likely an ESPHome Bluetooth
        proxy's own on-device cache still describing this address from
        before its last Global Reset/provisioning state change, which
        ``use_services_cache=False`` alone does not reach (see
        ``async_connect``). Clearing the cache and reconnecting once
        self-heals that without the caller needing to know it happened.
        """
        assert self.client is not None
        if self.client.services.get_characteristic(required_uuid) is not None:
            return
        await _clear_backend_cache(self.client)
        with contextlib.suppress(Exception):
            await self.client.disconnect()
        self._disconnected = asyncio.Event()
        self.client = await async_connect(
            self._hass, self.address, "steinel-direct", self.timeout, disconnected_callback=self._on_disconnected
        )
        self._disconnected.clear()

    async def __aexit__(self, *exc_info: object) -> None:
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.stop_notify(STEINEL_RX)
            with contextlib.suppress(Exception):
                await self.client.disconnect()

    async def clear_cache(self) -> None:
        """Purge cached GATT services for this address (local + Bluetooth
        proxy on-device cache, if the proxy supports it) while still
        connected. Needed after a command that changes what the device
        exposes - e.g. Global Reset switches it from the Proxy service to
        the Provisioning service - since establish_connection's own
        use_services_cache=False does not stop an ESPHome proxy that
        advertises "remote caching" from still serving its previously
        cached GATT table for this address on the next connection."""
        if self.client is not None:
            await _clear_backend_cache(self.client)

    def _notification(self, _sender: Any, data: bytearray) -> None:
        try:
            for packet in self._stream.feed(bytes(data)):
                response = response_from_packet(packet)
                waiter = self._waiters.get(response.opcode)
                if waiter is not None and not waiter.done():
                    waiter.set_result(response)
                else:
                    self.unsolicited.put_nowait(response)
        except Exception:
            _LOGGER.exception("Invalid STEINEL direct-channel notification from %s", self.address)

    async def command(self, opcode: int, data: bytes = b"", timeout: float | None = None) -> DirectResponse:
        if self.client is None:
            raise RuntimeError("not connected")
        frame = encode_direct_frame(opcode, data)
        async with self._lock:
            loop = asyncio.get_running_loop()
            future: asyncio.Future[DirectResponse] = loop.create_future()
            self._waiters[opcode] = future
            try:
                characteristic = self.client.services.get_characteristic(STEINEL_TX)
                if characteristic is None:
                    raise ProtocolError(f"characteristic {STEINEL_TX} is missing")
                max_size = getattr(characteristic, "max_write_without_response_size", 20)
                if len(frame) > max_size:
                    raise ProtocolError(
                        f"frame is {len(frame)} bytes, write-without-response currently only allows "
                        f"{max_size}; a larger ATT MTU is required for this command"
                    )
                await self.client.write_gatt_char(characteristic, frame, response=False)
                response: DirectResponse = await _race_disconnect(
                    future, self._disconnected, timeout or self.timeout, self.address
                )
                return response.require_ok()
            finally:
                self._waiters.pop(opcode, None)


class MeshTransport:
    """Connection to either the Mesh Provisioning or Mesh Proxy GATT service."""

    def __init__(
        self, hass: HomeAssistant, address: str, provisioning: bool = False, timeout: float = DEFAULT_CONNECT_TIMEOUT
    ) -> None:
        self._hass = hass
        self.address = address
        self.timeout = timeout
        self._input_uuid = MESH_PROVISIONING_IN if provisioning else MESH_PROXY_IN
        self._output_uuid = MESH_PROVISIONING_OUT if provisioning else MESH_PROXY_OUT
        self.client: BleakClientWithServiceCache | None = None
        self.queue: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        self._sar = ProxySarReceiver()
        self._disconnected = asyncio.Event()

    def _on_disconnected(self, _client: BleakClientWithServiceCache) -> None:
        _LOGGER.info("%s: disconnected_callback fired", self.address)
        self._disconnected.set()

    async def __aenter__(self) -> Self:
        self.client = await async_connect(
            self._hass, self.address, "steinel-mesh", self.timeout, disconnected_callback=self._on_disconnected
        )
        # A retried connect (establish_connection reuses one client instance
        # across its own internal attempts) can fire disconnected_callback
        # for an earlier failed attempt before finally succeeding; clear
        # that stale signal now that we actually have a live connection.
        self._disconnected.clear()
        await self._ensure_fresh_services()
        await self.client.start_notify(self._output_uuid, self._notification)
        _LOGGER.info("%s: subscribed to notifications on %s", self.address, self._output_uuid)
        return self

    async def _ensure_fresh_services(self) -> None:
        """Reconnect once with a forced fresh discovery if the expected
        Provisioning/Proxy Data Out characteristic is missing.

        A STEINEL lamp switches between the Mesh Provisioning service and
        the Mesh Proxy service as it moves in/out of a mesh (factory
        reset, provisioning). A missing characteristic here means the
        connection served a stale GATT table for the *other* service -
        most likely an ESPHome Bluetooth proxy's own on-device cache
        (``use_services_cache=False`` in ``async_connect`` only covers
        bleak/BlueZ's side, not that). Clearing the cache and reconnecting
        once self-heals that without the caller needing to know it
        happened.
        """
        assert self.client is not None
        if self.client.services.get_characteristic(self._output_uuid) is not None:
            return
        await _clear_backend_cache(self.client)
        with contextlib.suppress(Exception):
            await self.client.disconnect()
        self._disconnected = asyncio.Event()
        self.client = await async_connect(
            self._hass, self.address, "steinel-mesh", self.timeout, disconnected_callback=self._on_disconnected
        )
        self._disconnected.clear()

    async def __aexit__(self, *exc_info: object) -> None:
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.stop_notify(self._output_uuid)
            with contextlib.suppress(Exception):
                await self.client.disconnect()

    def _notification(self, _sender: Any, data: bytearray) -> None:
        _LOGGER.info("%s: raw notification, %d byte(s): %s", self.address, len(data), bytes(data).hex())
        try:
            complete = self._sar.feed(bytes(data))
            if complete is not None:
                self.queue.put_nowait(complete)
        except Exception:
            _LOGGER.exception("Invalid Mesh proxy notification from %s", self.address)

    async def send(self, pdu_type: int, pdu: bytes) -> None:
        if self.client is None:
            raise RuntimeError("not connected")
        characteristic = self.client.services.get_characteristic(self._input_uuid)
        if characteristic is None:
            raise ProtocolError(f"Mesh Data In {self._input_uuid} is missing")
        max_write = getattr(characteristic, "max_write_without_response_size", 20)
        segments = list(proxy_segments(pdu_type, pdu, max_write))
        _LOGGER.info(
            "%s: sending pdu_type=%d, %d byte(s) as %d segment(s) (max_write=%d)",
            self.address,
            pdu_type,
            len(pdu),
            len(segments),
            max_write,
        )
        for segment in segments:
            await self.client.write_gatt_char(characteristic, segment, response=False)
            # Write-without-response has no flow control at the ATT layer,
            # and this integration talks to the device through a Bluetooth
            # proxy (an extra WiFi<->BLE relay hop) rather than a local
            # adapter. Back-to-back writes with zero spacing were observed
            # to reach the proxy fine but never produce any response from
            # the device - most likely a segment silently dropped on the
            # peripheral side because it couldn't keep up. A small gap
            # between writes (this applies both between GATT-level SAR
            # segments here and, since callers loop over multiple send()
            # calls for a single Lower Transport-segmented Access message,
            # between those too) trades a few tens of ms for reliability.
            await asyncio.sleep(0.02)
        _LOGGER.info("%s: send() completed without error", self.address)

    async def receive(self, pdu_type: int, timeout: float | None = None) -> bytes:
        deadline = asyncio.get_running_loop().time() + (self.timeout if timeout is None else timeout)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProtocolError(f"timeout waiting for a Mesh GATT PDU of type {pdu_type}")
            try:
                received_type, pdu = await _race_disconnect(
                    self.queue.get(), self._disconnected, remaining, self.address
                )
            except TimeoutError as exc:
                raise ProtocolError(f"timeout waiting for a Mesh GATT PDU of type {pdu_type}") from exc
            if received_type == pdu_type:
                return pdu

    def drain(self) -> None:
        """Discard any queued-but-unconsumed PDUs.

        Called before starting a new top-level operation on a connection
        that is being reused across multiple commands, so a stale response
        left over from an earlier, already-abandoned wait can never be
        mistaken for the answer to a new one.
        """
        while not self.queue.empty():
            self.queue.get_nowait()


class NordicDfuClient:
    """Nordic Secure DFU object transfer (SELECT/CREATE/CRC/EXECUTE)."""

    def __init__(self, client: BleakClientWithServiceCache, timeout: float = 20.0) -> None:
        self.client = client
        self.timeout = timeout
        self._responses: asyncio.Queue[bytes] = asyncio.Queue()
        self._packet_characteristic: Any = None
        self.on_progress: Any = None  # optional callback(percent:int, offset:int, total:int)

    def _notification(self, _sender: Any, data: bytearray) -> None:
        self._responses.put_nowait(bytes(data))

    async def start(self) -> None:
        services = self.client.services
        control = services.get_characteristic(NORDIC_DFU_CONTROL)
        self._packet_characteristic = services.get_characteristic(NORDIC_DFU_PACKET)
        if control is None or self._packet_characteristic is None:
            raise ProtocolError("device does not expose the Nordic Secure DFU control/packet characteristics")
        await self.client.start_notify(control, self._notification)

    async def command(self, payload: bytes, timeout: float | None = None) -> bytes:
        opcode = payload[0]
        while not self._responses.empty():
            self._responses.get_nowait()
        await self.client.write_gatt_char(NORDIC_DFU_CONTROL, payload, response=True)
        response = await asyncio.wait_for(self._responses.get(), timeout or self.timeout)
        return parse_nordic_dfu_response(response, opcode)

    async def select(self, object_type: int) -> tuple[int, int, int]:
        import struct

        response = await self.command(bytes([0x06, object_type]))
        if len(response) != 12:
            raise ProtocolError(f"Nordic SELECT returned {len(response)} instead of 12 data bytes")
        return struct.unpack("<III", response)

    async def create(self, object_type: int, size: int) -> None:
        import struct

        await self.command(bytes([0x01, object_type]) + struct.pack("<I", size))

    async def checksum(self) -> tuple[int, int]:
        import struct

        response = await self.command(b"\x03")
        if len(response) != 8:
            raise ProtocolError(f"Nordic CRC returned {len(response)} instead of 8 data bytes")
        return struct.unpack("<II", response)

    async def execute(self, final: bool = False) -> None:
        try:
            await self.command(b"\x04")
        except Exception:
            if not final or self.client.is_connected:
                raise

    async def write_bytes(self, data: bytes) -> None:
        characteristic = self._packet_characteristic
        max_write = int(getattr(characteristic, "max_write_without_response_size", 20) or 20)
        max_write = max(20, min(max_write, 244))
        for index in range(0, len(data), max_write):
            await self.client.write_gatt_char(characteristic, data[index : index + max_write], response=False)
            if (index // max_write) % 32 == 31:
                await asyncio.sleep(0.002)

    @staticmethod
    def _check_prefix(payload: bytes, offset: int, crc: int, label: str) -> None:
        import zlib

        if offset > len(payload):
            raise ProtocolError(f"bootloader {label} offset {offset} is past the end of the package")
        expected = zlib.crc32(payload[:offset]) & 0xFFFFFFFF
        if crc != expected:
            raise ProtocolError(
                f"bootloader {label} CRC does not match the package (offset {offset}, "
                f"device 0x{crc:08X}, package 0x{expected:08X}); aborting without overwriting"
            )

    async def transfer_command_object(self, payload: bytes) -> None:
        maximum, offset, crc = await self.select(1)
        if maximum < len(payload):
            raise ProtocolError(f"init packet ({len(payload)} bytes) exceeds the command size limit {maximum}")
        self._check_prefix(payload, offset, crc, "command")
        if offset == 0:
            await self.create(1, len(payload))
        if offset < len(payload):
            await self.write_bytes(payload[offset:])
            reported_offset, reported_crc = await self.checksum()
            self._check_prefix(payload, reported_offset, reported_crc, "command")
            if reported_offset != len(payload):
                raise ProtocolError(f"init packet only transferred up to offset {reported_offset}")
        await self.execute()

    async def transfer_data_objects(self, payload: bytes) -> None:
        maximum, offset, crc = await self.select(2)
        if maximum <= 0:
            raise ProtocolError("bootloader reports an invalid maximum data object size")
        self._check_prefix(payload, offset, crc, "data")
        last_percent = -1
        while offset < len(payload):
            within = offset % maximum
            object_end = min(offset - within + maximum, len(payload))
            if within == 0:
                await self.create(2, object_end - offset)
            await self.write_bytes(payload[offset:object_end])
            reported_offset, reported_crc = await self.checksum()
            self._check_prefix(payload, reported_offset, reported_crc, "data")
            if reported_offset != object_end:
                raise ProtocolError(f"only transferred up to offset {reported_offset}, expected {object_end}")
            offset = reported_offset
            await self.execute(final=offset == len(payload))
            percent = offset * 100 // len(payload)
            if (percent // 5 != last_percent // 5 or offset == len(payload)) and self.on_progress:
                self.on_progress(percent, offset, len(payload))
                last_percent = percent


async def async_wait_for_manufacturer_data(
    hass: HomeAssistant, addresses: set[str], company_id: int, timeout: float
) -> bluetooth.BluetoothServiceInfoBleak:
    """Wait until one of ``addresses`` advertises manufacturer data for ``company_id``."""
    wanted = {addr.upper() for addr in addresses}
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        if info.address.upper() in wanted and company_id in info.manufacturer_data:
            return info

    loop = asyncio.get_running_loop()
    found: asyncio.Future[bluetooth.BluetoothServiceInfoBleak] = loop.create_future()

    @callback
    def _on_update(service_info: bluetooth.BluetoothServiceInfoBleak, change: Any) -> None:
        if (
            service_info.address.upper() in wanted
            and company_id in service_info.manufacturer_data
            and not found.done()
        ):
            found.set_result(service_info)

    unregister = bluetooth.async_register_callback(
        hass,
        _on_update,
        bluetooth.BluetoothCallbackMatcher(manufacturer_id=company_id, connectable=True),
        bluetooth.BluetoothScanningMode.ACTIVE,
    )
    try:
        return await asyncio.wait_for(found, timeout)
    except asyncio.TimeoutError as exc:
        raise ProtocolError(
            f"none of {sorted(wanted)} advertised STEINEL manufacturer data within {timeout:g}s"
        ) from exc
    finally:
        unregister()


async def async_enter_nordic_bootloader(hass: HomeAssistant, address: str, timeout: float) -> None:
    client = await async_connect(hass, address, "steinel-buttonless-dfu", timeout)
    indication: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    def _callback(_sender: Any, data: bytearray) -> None:
        if not indication.done():
            indication.set_result(bytes(data))

    try:
        characteristic = client.services.get_characteristic(NORDIC_DFU_BUTTONLESS)
        if characteristic is None:
            raise ProtocolError("device does not expose the Nordic buttonless DFU characteristic")
        await client.start_notify(characteristic, _callback)
        await client.write_gatt_char(characteristic, b"\x01", response=True)
        try:
            response = await asyncio.wait_for(indication, min(timeout, 8.0))
            if response != b"\x20\x01\x01":
                raise ProtocolError(f"buttonless DFU request was rejected: {response.hex()}")
        except asyncio.TimeoutError:
            if client.is_connected:
                raise ProtocolError("no confirmation for entering the DFU bootloader") from None
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
