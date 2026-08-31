"""Tests for Mesh composition and GATT segmentation."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path


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
