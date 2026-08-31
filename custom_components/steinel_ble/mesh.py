"""Bluetooth Mesh network, transport and access layers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag

from .const import DEFAULT_TTL
from .crypto import (
    NetworkKeys,
    k4,
    network_decrypt,
    network_encrypt,
    upper_transport_decrypt,
    upper_transport_encrypt,
)
from .gatt import MeshGattTransport

_LOGGER = logging.getLogger(__name__)


def encode_opcode(opcode: int) -> bytes:
    """Encode a one-, two-, or three-octet access opcode."""
    if opcode <= 0x7F:
        return bytes((opcode,))
    if opcode <= 0xFFFF:
        return opcode.to_bytes(2, "big")
    return opcode.to_bytes(3, "big")


def decode_opcode(payload: bytes) -> tuple[int, bytes]:
    """Decode an access opcode and return its parameters."""
    if not payload:
        raise ValueError("Empty access payload")
    length = 1 if payload[0] & 0x80 == 0 else 3 if payload[0] & 0xC0 == 0xC0 else 2
    if len(payload) < length:
        raise ValueError("Truncated access opcode")
    return int.from_bytes(payload[:length], "big"), payload[length:]


@dataclass
class ElementComposition:
    """Models implemented by one element."""

    address: int
    sig_models: set[int]
    vendor_models: set[tuple[int, int]]


def parse_composition_data(parameters: bytes, primary: int) -> list[ElementComposition]:
    """Parse a Config Composition Data Status page zero payload."""
    if len(parameters) < 11 or parameters[0] != 0:
        raise ValueError("Unsupported or truncated Composition Data page")
    pos = 11
    elements: list[ElementComposition] = []
    address = primary
    while pos < len(parameters):
        if pos + 4 > len(parameters):
            raise ValueError("Truncated element descriptor")
        _location = int.from_bytes(parameters[pos : pos + 2], "little")
        sig_count, vendor_count = parameters[pos + 2], parameters[pos + 3]
        pos += 4
        needed = sig_count * 2 + vendor_count * 4
        if pos + needed > len(parameters):
            raise ValueError("Truncated model list")
        sig = {
            int.from_bytes(parameters[i : i + 2], "little")
            for i in range(pos, pos + sig_count * 2, 2)
        }
        pos += sig_count * 2
        vendor = {
            (
                int.from_bytes(parameters[i : i + 2], "little"),
                int.from_bytes(parameters[i + 2 : i + 4], "little"),
            )
            for i in range(pos, pos + vendor_count * 4, 4)
        }
        pos += vendor_count * 4
        elements.append(ElementComposition(address, sig, vendor))
        address += 1
    return elements


@dataclass
class _InboundSegments:
    """Segments belonging to one lower-transport message."""

    seq_auth: int
    dst: int
    akf: int
    aid: int
    szmic: int
    seg_n: int
    parts: dict[int, bytes] = field(default_factory=dict)


class MeshNode:
    """A provisioner's network/transport/access endpoint."""

    def __init__(
        self,
        transport: MeshGattTransport,
        net_key: bytes,
        app_key: bytes,
        device_key: bytes,
        iv_index: int,
        sequence: int,
        primary_address: int,
        sequence_changed: Callable[[int], None],
        access_received: Callable[[int, int, bytes], None] | None = None,
    ) -> None:
        self.transport = transport
        self.net_keys = NetworkKeys.derive(net_key)
        self.app_key = app_key
        self.device_key = device_key
        self.iv_index = iv_index
        self.sequence = sequence
        self.primary_address = primary_address
        self.sequence_changed = sequence_changed
        self.access_received = access_received
        self.src = 0x0001
        self._tid = 0
        self._requests: list[
            tuple[int, int | None, asyncio.Future[tuple[int, bytes]]]
        ] = []
        self._segments: dict[tuple[int, int], _InboundSegments] = {}
        self._segment_acks: dict[int, asyncio.Event] = {}
        self._vendor_requests: list[
            tuple[int | None, asyncio.Future[tuple[int, int, bytes]]]
        ] = []
        self._send_lock = asyncio.Lock()

    def next_tid(self) -> int:
        """Return a model transaction ID."""
        value = self._tid
        self._tid = (self._tid + 1) & 0xFF
        return value

    def _take_sequence(self) -> int:
        if self.sequence > 0xFFFFFE:
            raise RuntimeError("Bluetooth Mesh sequence space exhausted")
        value = self.sequence
        self.sequence += 1
        self.sequence_changed(self.sequence)
        return value

    async def handle_pdu(self, pdu_type: int, payload: bytes) -> None:
        """Receive a Proxy Network PDU."""
        if pdu_type != 0x00:
            return
        try:
            network = network_decrypt(self.net_keys, self.iv_index, payload)
        except (ValueError, InvalidTag):
            _LOGGER.debug("Ignoring a Mesh PDU that cannot be authenticated")
            return
        lower = network.lower_transport
        if not lower:
            return
        if network.ctl:
            self._handle_control(network.src, lower)
            return
        segmented = bool(lower[0] & 0x80)
        akf = (lower[0] >> 6) & 1
        aid = lower[0] & 0x3F
        if not segmented:
            await self._handle_upper(
                network.src, network.dst, network.seq, akf, aid, 0, lower[1:]
            )
            return
        if len(lower) < 4:
            return
        value = int.from_bytes(lower[1:4], "big")
        szmic = (value >> 23) & 1
        seq_zero = (value >> 10) & 0x1FFF
        seg_o = (value >> 5) & 0x1F
        seg_n = value & 0x1F
        seq_auth = (network.seq & ~0x1FFF) | seq_zero
        if seq_zero > network.seq & 0x1FFF:
            seq_auth -= 0x2000
        key = (network.src, seq_zero)
        item = self._segments.get(key)
        if item is None:
            item = self._segments[key] = _InboundSegments(
                seq_auth, network.dst, akf, aid, szmic, seg_n
            )
        if item.seg_n != seg_n or seg_o > seg_n:
            return
        item.parts[seg_o] = lower[4:]
        if len(item.parts) != seg_n + 1:
            return
        del self._segments[key]
        await self._send_segment_ack(network.src, seq_zero, (1 << (seg_n + 1)) - 1)
        upper = b"".join(item.parts[index] for index in range(seg_n + 1))
        await self._handle_upper(
            network.src, item.dst, item.seq_auth, item.akf, item.aid, item.szmic, upper
        )

    def _handle_control(self, src: int, lower: bytes) -> None:
        if lower[0] & 0x7F or len(lower) < 7:
            return
        seq_zero = (int.from_bytes(lower[1:3], "big") >> 2) & 0x1FFF
        block_ack = int.from_bytes(lower[3:7], "big")
        if block_ack and (event := self._segment_acks.get(seq_zero)):
            event.set()

    async def _handle_upper(
        self,
        src: int,
        dst: int,
        seq_auth: int,
        akf: int,
        aid: int,
        szmic: int,
        upper: bytes,
    ) -> None:
        key = (
            self.app_key
            if akf and aid == k4(self.app_key)
            else self.device_key
            if not akf
            else None
        )
        if key is None:
            return
        try:
            access = upper_transport_decrypt(
                key,
                0x01 if akf else 0x02,
                seq_auth,
                src,
                dst,
                self.iv_index,
                upper,
                szmic=szmic,
            )
            opcode, parameters = decode_opcode(access)
        except (InvalidTag, ValueError):
            return
        for expected, source, future in tuple(self._requests):
            if (
                not future.done()
                and opcode == expected
                and (source is None or source == src)
            ):
                future.set_result((src, parameters))
                break
        if self.access_received:
            self.access_received(src, opcode, parameters)
        if opcode >= 0xC00000 and opcode & 0xFFFF == 0x6305:
            for source, future in tuple(self._vendor_requests):
                if not future.done() and (source is None or source == src):
                    future.set_result((src, opcode, parameters))
                    break

    async def initialize_proxy_filter(self) -> None:
        """Select a blacklist filter so the proxy forwards all node traffic."""
        seq = self._take_sequence()
        pdu = network_encrypt(
            self.net_keys,
            self.iv_index,
            0x80,
            seq,
            self.src,
            0x0000,
            b"\x00\x01",  # Set Filter Type: blacklist
            nonce_type=0x03,
        )
        await self.transport.send(0x02, pdu)

    async def _send_segment_ack(self, dst: int, seq_zero: int, block_ack: int) -> None:
        params = (seq_zero << 2).to_bytes(2, "big") + block_ack.to_bytes(4, "big")
        await self._send_network(dst, b"\x00" + params, ctl=True)

    async def _send_network(
        self, dst: int, lower: bytes, *, ctl: bool = False, sequence: int | None = None
    ) -> int:
        seq = self._take_sequence() if sequence is None else sequence
        pdu = network_encrypt(
            self.net_keys,
            self.iv_index,
            (0x80 if ctl else 0) | DEFAULT_TTL,
            seq,
            self.src,
            dst,
            lower,
        )
        await self.transport.send(0x00, pdu)
        return seq

    async def send_access(
        self,
        dst: int,
        opcode: int,
        parameters: bytes = b"",
        *,
        device_key: bool = False,
    ) -> None:
        """Encrypt, segment when needed, and send an access message."""
        async with self._send_lock:
            first_seq = self._take_sequence()
            key = self.device_key if device_key else self.app_key
            aid = 0 if device_key else k4(self.app_key)
            upper = upper_transport_encrypt(
                key,
                0x02 if device_key else 0x01,
                first_seq,
                self.src,
                dst,
                self.iv_index,
                encode_opcode(opcode) + parameters,
            )
            if len(upper) <= 15:
                lower = bytes(((0 if device_key else 0x40) | aid,)) + upper
                await self._send_network(dst, lower, sequence=first_seq)
                return
            segments = [upper[pos : pos + 12] for pos in range(0, len(upper), 12)]
            if len(segments) > 32:
                raise ValueError("Access message exceeds lower transport capacity")
            seq_zero = first_seq & 0x1FFF
            event = self._segment_acks[seq_zero] = asyncio.Event()
            try:
                for seg_o, segment in enumerate(segments):
                    header = (
                        (seq_zero << 10) | (seg_o << 5) | (len(segments) - 1)
                    ).to_bytes(3, "big")
                    lower = (
                        bytes((0x80 | (0 if device_key else 0x40) | aid,))
                        + header
                        + segment
                    )
                    await self._send_network(
                        dst, lower, sequence=first_seq if seg_o == 0 else None
                    )
                await asyncio.wait_for(event.wait(), 10)
            finally:
                self._segment_acks.pop(seq_zero, None)

    async def request(
        self,
        dst: int,
        opcode: int,
        parameters: bytes,
        response_opcode: int,
        *,
        device_key: bool = False,
        timeout: float = 15,
    ) -> tuple[int, bytes]:
        """Send access data and wait for its acknowledged response."""
        future: asyncio.Future[tuple[int, bytes]] = (
            asyncio.get_running_loop().create_future()
        )
        request = (response_opcode, dst, future)
        self._requests.append(request)
        try:
            await self.send_access(dst, opcode, parameters, device_key=device_key)
            return await asyncio.wait_for(future, timeout)
        finally:
            self._requests.remove(request)

    async def request_vendor(
        self,
        dst: int,
        opcode: int,
        parameters: bytes = b"",
        *,
        timeout: float = 10,
    ) -> tuple[int, int, bytes]:
        """Send a Steinel vendor message and accept its authenticated reply."""
        future: asyncio.Future[tuple[int, int, bytes]] = (
            asyncio.get_running_loop().create_future()
        )
        request = (dst, future)
        self._vendor_requests.append(request)
        try:
            wire_opcode = int.from_bytes(
                bytes((opcode,)) + (0x0563).to_bytes(2, "little"), "big"
            )
            await self.send_access(dst, wire_opcode, parameters)
            return await asyncio.wait_for(future, timeout)
        finally:
            self._vendor_requests.remove(request)

    async def configure(self, element_count: int) -> list[ElementComposition]:
        """Add the AppKey and discover light models through acknowledged binds."""
        indexes = b"\x00\x00\x00"  # NetKey Index 0, AppKey Index 0
        _, status = await self.request(
            self.primary_address,
            0x00,
            indexes + self.app_key,
            0x8003,
            device_key=True,
        )
        if not status or status[0] not in (0x00, 0x06):
            raise RuntimeError(
                f"Config AppKey Add failed with status {status[:1].hex()}"
            )
        elements = [
            ElementComposition(self.primary_address + offset, set(), set())
            for offset in range(element_count)
        ]
        for element in elements:
            for model in (0x1000, 0x1300, 0x1303, 0x1307, 0x130F):
                bind = (
                    element.address.to_bytes(2, "little")
                    + b"\x00\x00"
                    + model.to_bytes(2, "little")
                )
                _, status = await self.request(
                    self.primary_address, 0x803D, bind, 0x803E, device_key=True
                )
                expected = (
                    element.address.to_bytes(2, "little")
                    + b"\x00\x00"
                    + model.to_bytes(2, "little")
                )
                if len(status) >= 7 and status[0] == 0 and status[1:7] == expected:
                    element.sig_models.add(model)
                else:
                    _LOGGER.debug(
                        "Model 0x%04X is not bindable on element 0x%04X: %s",
                        model,
                        element.address,
                        status.hex(),
                    )
            for model in (0x1001, 0x1003):
                bind = (
                    element.address.to_bytes(2, "little")
                    + b"\x00\x00"
                    + (0x0563).to_bytes(2, "little")
                    + model.to_bytes(2, "little")
                )
                _, status = await self.request(
                    self.primary_address, 0x803D, bind, 0x803E, device_key=True
                )
                expected = bind
                if len(status) >= 9 and status[0] == 0 and status[1:9] == expected:
                    element.vendor_models.add((0x0563, model))
                else:
                    _LOGGER.debug(
                        "Vendor model 0x%04X is not bindable on element 0x%04X: %s",
                        model,
                        element.address,
                        status.hex(),
                    )
        return elements
