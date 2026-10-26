"""Tests for Mesh composition and GATT segmentation."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest


def _load_package():
    root = Path(__file__).parents[1] / "custom_components/steinel_ble"
    package_name = "steinel_test_mesh_package"
    package = types.ModuleType(package_name)
    package.__path__ = [str(root)]
    sys.modules[package_name] = package
    for module_name in ("const", "crypto", "gatt", "mesh", "provisioning"):
        name = f"{package_name}.{module_name}"
        spec = importlib.util.spec_from_file_location(name, root / f"{module_name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return (
        sys.modules[f"{package_name}.const"],
        sys.modules[f"{package_name}.gatt"],
        sys.modules[f"{package_name}.mesh"],
        sys.modules[f"{package_name}.provisioning"],
    )


const, gatt, mesh, provisioning = _load_package()
crypto = sys.modules[f"{mesh.__package__}.crypto"]


def test_opcode_round_trip() -> None:
    for opcode in (0x12, 0x8202, 0xD06305):
        encoded = mesh.encode_opcode(opcode) + b"payload"
        assert mesh.decode_opcode(encoded) == (opcode, b"payload")


def test_initial_state_get_opcodes() -> None:
    """Initial state retrieval uses the assigned Bluetooth Mesh opcodes."""
    assert const.OP_GENERIC_ONOFF_GET == 0x8201
    assert const.OP_LIGHT_LIGHTNESS_GET == 0x824B
    assert const.OP_LIGHT_CTL_GET == 0x825D
    assert const.OP_LIGHT_HSL_GET == 0x826D


def test_composition_data_multiple_elements() -> None:
    header = b"\x00" + bytes(10)
    first = b"\x00\x00\x02\x01\x00\x10\x00\x13\x63\x05\x03\x10"
    second = b"\x00\x00\x01\x00\x0f\x13"
    elements = mesh.parse_composition_data(header + first + second, 0x0100)
    assert [element.address for element in elements] == [0x0100, 0x0101]
    assert elements[0].sig_models == {0x1000, 0x1300}
    assert elements[0].vendor_models == {(0x0563, 0x1003)}
    assert elements[1].sig_models == {0x130F}


def test_gatt_segments_using_characteristic_capacity() -> None:
    class Characteristic:
        max_write_without_response_size = 6

    class Services:
        def get_characteristic(self, _uuid):
            return Characteristic()

    class Client:
        is_connected = True
        services = Services()

        def __init__(self):
            self.writes = []

        async def write_gatt_char(self, _characteristic, data, response):
            self.writes.append((bytes(data), response))

    transport = gatt.MeshGattTransport(None, "test", "in", "out", None)
    transport.client = Client()
    asyncio.run(transport.send(3, b"0123456789"))
    assert [write[0][0] for write in transport.client.writes] == [0x43, 0xC3]
    assert b"".join(write[0][1:] for write in transport.client.writes) == b"0123456789"


def test_provisioning_failure_is_reported() -> None:
    async def run():
        provisioner = provisioning.Provisioner(None)
        await provisioner._queue.put(b"\x09\x03")
        try:
            await provisioner._receive(1)
        except provisioning.ProvisioningError as err:
            assert "failure 3" in str(err)
        else:
            raise AssertionError("Provisioning failure was ignored")

    asyncio.run(run())


def _receiving_node():
    return mesh.MeshNode(
        None, bytes(16), bytes([1]) * 16, bytes([2]) * 16, 0, 0, 2, Mock(), Mock()
    )


def test_receive_diagnostics_distinguish_network_failures():
    node = _receiving_node()
    good = mesh.network_encrypt(node.net_keys, 0, 5, 1, 2, 1, b"\x40test")
    other_key = bytes([good[0] ^ 1]) + good[1:]
    other_iv = bytes([good[0] ^ 0x80]) + good[1:]
    invalid_mic = good[:-1] + bytes([good[-1] ^ 1])

    async def run():
        await node.handle_pdu(0, other_key)
        await node.handle_pdu(0, other_iv)
        await node.handle_pdu(0, invalid_mic)

    asyncio.run(run())
    assert dict(node.rx_stats) == {
        "network_other_key": 1,
        "network_iv_mismatch": 1,
        "network_invalid": 1,
    }
    node.access_received.assert_not_called()


def test_receive_diagnostics_count_authenticated_access():
    node = _receiving_node()
    upper = mesh.upper_transport_encrypt(node.app_key, 1, 1, 2, 1, 0, b"\x82\x04\x01")
    lower = bytes([0x40 | mesh.k4(node.app_key)]) + upper
    pdu = mesh.network_encrypt(node.net_keys, 0, 5, 1, 2, 1, lower)
    asyncio.run(node.handle_pdu(0, pdu))
    assert node.rx_stats["network_authenticated"] == 1
    assert node.rx_stats["access_authenticated"] == 1
    node.access_received.assert_called_once_with(2, 0x8204, b"\x01")


def test_concurrent_proxy_sends_do_not_interleave_fragments():
    class Client:
        is_connected = True

        def __init__(self):
            characteristic = types.SimpleNamespace(max_write_without_response_size=6)
            self.services = types.SimpleNamespace(
                get_characteristic=lambda _: characteristic
            )
            self.writes = []

        async def write_gatt_char(self, _characteristic, data, response):
            self.writes.append(bytes(data))
            await asyncio.sleep(0)

    async def run():
        transport = gatt.MeshGattTransport(None, "test", "in", "out", None)
        client = transport.client = Client()
        await asyncio.gather(
            transport.send(0, b"first message"),
            transport.send(0, b"second message"),
        )
        receiver = gatt.MeshGattTransport(None, "test", "in", "out", None)
        received = []

        async def receive(pdu_type, payload):
            received.append((pdu_type, payload))

        receiver.handler = receive
        for fragment in client.writes:
            receiver._notification(None, bytearray(fragment))
        await asyncio.gather(*receiver._tasks)
        assert received == [(0, b"first message"), (0, b"second message")]

    asyncio.run(run())


def _secure_beacon(index, flags=0, key=bytes(16)):
    decoder = crypto.MeshBeaconDecoder(key)
    body = bytes([flags]) + decoder.network_id + index.to_bytes(4, "big")
    return b"\x01" + body + crypto.aes_cmac(decoder.beacon_key, body)[:8]


def test_authenticated_beacon_corrects_iv_before_filter_without_resetting_sequence():
    node = _receiving_node()
    node.sequence = 78
    node.src = 0x5555
    sent = []

    async def send(pdu_type, pdu):
        sent.append((pdu_type, pdu))

    node.transport = types.SimpleNamespace(send=send)

    async def run():
        await node.handle_pdu(1, _secure_beacon(3))
        assert await node.synchronize_iv_index(timeout=0.01)
        assert node.iv_index == 3
        assert node.iv_state_verified
        assert node.sequence == 78
        node.sequence_changed.assert_not_called()
        await node.initialize_proxy_filter()

    asyncio.run(run())
    assert node.sequence == 79
    assert node.src == 0x5555
    assert sent == [
        (
            2,
            mesh.network_encrypt(
                node.net_keys, 3, 0x80, 78, 0x5555, 0, b"\x00\x01", nonce_type=3
            ),
        )
    ]


def test_unverified_iv_bit_and_invalid_beacon_cannot_change_iv_state():
    node = _receiving_node()
    mismatch = mesh.network_encrypt(node.net_keys, 1, 5, 1, 2, 1, b"test")

    async def run():
        await node.handle_pdu(0, mismatch)
        await node.handle_pdu(1, _secure_beacon(1, key=bytes([4]) * 16))
        assert not await node.synchronize_iv_index(timeout=0.001)

    asyncio.run(run())
    assert node.iv_index == 0
    assert not node.iv_state_verified
    assert node.sequence == 0


def test_older_beacon_cannot_roll_back_persisted_iv_index():
    node = _receiving_node()
    node.iv_index = 3

    async def run():
        await node.handle_pdu(1, _secure_beacon(1))
        assert not await node.synchronize_iv_index(timeout=0.001)

    asyncio.run(run())
    assert node.iv_index == 3
    assert node.rx_stats["beacon_stale"] == 1


def test_completed_iv_update_cannot_be_reversed_by_stale_beacon():
    node = _receiving_node()
    node.iv_index = 3
    node.iv_state_verified = True

    async def run():
        await node.handle_pdu(1, _secure_beacon(3, flags=2))
        assert not await node.synchronize_iv_index(timeout=0.001)

    asyncio.run(run())
    assert not node.iv_update
    assert node.rx_stats["beacon_stale"] == 1


def test_iv_cannot_change_in_the_middle_of_outgoing_traffic():
    node = _receiving_node()

    async def run():
        await node.handle_pdu(1, _secure_beacon(3))
        await node.synchronize_iv_index(timeout=0.01)
        node._take_sequence()
        await node.handle_pdu(1, _secure_beacon(4))
        with pytest.raises(RuntimeError, match="precede outgoing"):
            await node.synchronize_iv_index(timeout=0.01)

    asyncio.run(run())
    assert node.iv_index == 3
    assert node.beacon.iv_index == 4


def test_iv_update_receives_both_indexes_and_transmits_previous_index():
    node = _receiving_node()
    writes = []

    async def send(_type, pdu):
        writes.append(pdu)

    node.transport = types.SimpleNamespace(send=send)

    async def run():
        await node.handle_pdu(1, _secure_beacon(3, flags=2))
        assert await node.synchronize_iv_index(timeout=0.01)
        assert node.transmit_iv_index == 2
        await node.send_access(2, 0x8201)
        for index in (2, 3):
            upper = mesh.upper_transport_encrypt(
                node.app_key, 1, 100, 2, 1, index, b"\x82\x04\x01"
            )
            lower = bytes([0x40 | mesh.k4(node.app_key)]) + upper
            pdu = mesh.network_encrypt(node.net_keys, index, 5, 100, 2, 1, lower)
            await node.handle_pdu(0, pdu)

    asyncio.run(run())
    assert node.rx_stats["access_authenticated"] == 2
    assert node.access_received.call_count == 2
    outgoing = mesh.network_decrypt(node.net_keys, 2, writes[0])
    assert outgoing.dst == 2
    plain = mesh.upper_transport_decrypt(
        node.app_key, 1, outgoing.seq, node.src, 2, 2, outgoing.lower_transport[1:]
    )
    assert plain == b"\x82\x01"


def test_initial_network_traffic_cannot_send_before_iv_synchronization():
    node = _receiving_node()
    node.iv_sync_pending = True
    upper = mesh.upper_transport_encrypt(node.app_key, 1, 1, 2, 1, 0, b"\x82\x04\x01")
    lower = bytes([0x40 | mesh.k4(node.app_key)]) + upper
    pdu = mesh.network_encrypt(node.net_keys, 0, 5, 1, 2, 1, lower)
    asyncio.run(node.handle_pdu(0, pdu))
    assert node.rx_stats["network_authenticated"] == 1
    assert node.sequence == 0
    node.access_received.assert_not_called()


def test_segmented_messages_keep_their_own_iv_index_during_update():
    node = _receiving_node()
    node.iv_index = 3
    node.iv_update = True
    acks = []

    async def send(_type, pdu):
        acks.append(pdu)

    node.transport = types.SimpleNamespace(send=send)
    sequences = {}
    for iv, marker in ((2, b"a"), (3, b"b")):
        upper = mesh.upper_transport_encrypt(
            node.app_key, 1, 100, 2, 1, iv, b"\x82\x04" + marker * 23
        )
        chunks = [upper[pos : pos + 12] for pos in range(0, len(upper), 12)]
        sequences[iv] = []
        for offset, chunk in enumerate(chunks):
            header = ((100 << 10) | (offset << 5) | (len(chunks) - 1)).to_bytes(
                3, "big"
            )
            lower = bytes([0xC0 | mesh.k4(node.app_key)]) + header + chunk
            sequences[iv].append(
                mesh.network_encrypt(node.net_keys, iv, 5, 100 + offset, 2, 1, lower)
            )

    async def run():
        for packets in zip(sequences[2], sequences[3], strict=True):
            for packet in packets:
                await node.handle_pdu(0, packet)

    asyncio.run(run())
    assert node.access_received.call_count == 2
    node.access_received.assert_any_call(2, 0x8204, b"a" * 23)
    node.access_received.assert_any_call(2, 0x8204, b"b" * 23)
    assert len(acks) == 2
    assert all(mesh.network_decrypt(node.net_keys, 2, ack).ctl for ack in acks)
