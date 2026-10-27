"""Sensor response correlation, polling, and motion-entity regressions."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, Mock

import pytest
from test_import_setup import integration as integration


@pytest.fixture
def sensors(integration, monkeypatch):
    """Use real Mesh decryption, coordinator parsing, and a minimal HA entity."""

    class Entity:
        def async_on_remove(self, callback):
            self.remove_callback = callback

        def async_write_ha_state(self):
            pass

    module = types.ModuleType("homeassistant.components.binary_sensor")
    module.BinarySensorEntity = Entity
    module.BinarySensorDeviceClass = types.SimpleNamespace(
        MOTION="motion", OCCUPANCY="occupancy"
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    platform = types.ModuleType("homeassistant.helpers.entity_platform")
    platform.AddConfigEntryEntitiesCallback = object
    monkeypatch.setitem(sys.modules, platform.__name__, platform)
    package = sys.modules[integration.coordinator.__package__]
    package.SteinelConfigEntry = types.SimpleNamespace
    binary = importlib.import_module(f"{package.__name__}.binary_sensor")
    mesh = importlib.import_module(f"{package.__name__}.mesh")
    coordinator = integration.coordinator.SteinelCoordinator(
        types.SimpleNamespace(data={}),
        types.SimpleNamespace(
            title="Test lamp",
            options={},
            data={
                "address": "AA:BB:CC:DD:EE:FF",
                "sensor_properties": {"12": ["motion"]},
            },
        ),
    )
    coordinator.available = True
    coordinator.transport = types.SimpleNamespace(
        client=types.SimpleNamespace(is_connected=True)
    )
    coordinator._schedule_state_persist = Mock()
    coordinator._arm_idle_disconnect = Mock()
    node = mesh.MeshNode(
        None,
        bytes(16),
        bytes([1]) * 16,
        bytes([2]) * 16,
        0,
        0,
        10,
        Mock(),
        coordinator._access_received,
        request_timeout=0.1,
    )
    node.send_access = AsyncMock()
    coordinator.node = node
    coordinator.async_ensure_connected = AsyncMock(return_value=node)
    element = types.SimpleNamespace(address=12)
    entity = binary.SteinelMotionSensor(coordinator, element, "motion")
    sequence = 0

    async def deliver(parameters, opcode=0xD16305, src=12):
        nonlocal sequence
        sequence += 1
        upper = mesh.upper_transport_encrypt(
            node.app_key,
            1,
            sequence,
            src,
            node.src,
            0,
            mesh.encode_opcode(opcode) + parameters,
        )
        await node._handle_upper(
            src, node.src, sequence, 1, mesh.k4(node.app_key), 0, upper
        )

    return types.SimpleNamespace(
        coordinator=coordinator, node=node, entity=entity, deliver=deliver
    )


def test_motion_clears_from_authenticated_format_a_report(sensors):
    """0x40 is a header byte, not the motion value in 40 08 00."""

    async def run():
        await sensors.entity.async_added_to_hass()
        await sensors.deliver(b"\x40\x08\xc8")
        assert sensors.entity._attr_is_on is True
        assert sensors.entity._attr_available
        await sensors.deliver(b"\x40\x08\x00")
        assert sensors.entity._attr_is_on is False
        assert sensors.entity._attr_available
        assert sensors.entity._attr_extra_state_attributes == {"raw": "00"}
        await sensors.deliver(b"\x40\x08\xff")
        assert sensors.entity._attr_is_on is None
        assert not sensors.entity._attr_available
        await sensors.deliver(b"\x40\x08\x00")
        assert sensors.entity._attr_is_on is False
        assert sensors.entity._attr_available

    asyncio.run(run())


def test_motion_clears_from_polled_format_a_reply(sensors):
    """The old parser read header 0x40 as true even when the value was zero."""

    async def reply(_address, _opcode, _parameters):
        await sensors.deliver(b"\x40\x08\x00")

    async def run():
        await sensors.entity.async_added_to_hass()
        sensors.node.send_access.side_effect = reply
        await sensors.coordinator._async_poll_sensors()
        assert sensors.entity._attr_is_on is False
        assert sensors.entity._attr_available
        assert sensors.entity._attr_extra_state_attributes == {"raw": "00"}

    asyncio.run(run())


def test_polled_motion_clears_and_rejects_unrelated_vendor_replies(sensors):
    async def reply(_address, opcode, parameters):
        assert opcode == 0xD06305
        assert parameters == b"\x42\x00"
        for payload, status, source in (
            (b"\x40\x08\xc8", 0xDC6305, 12),  # Wrong response opcode.
            (b"\xa0\x09\x01", 0xD16305, 12),  # Different property.
            (b"\x40\x08", 0xD16305, 12),  # Truncated report.
            (b"\x40\x08\xc8", 0xD16305, 13),  # Different element.
        ):
            await sensors.deliver(payload, status, source)
            assert not sensors.node._vendor_requests[0][-1].done()
        await sensors.deliver(b"\x40\x08\x00")

    async def run():
        await sensors.entity.async_added_to_hass()
        await sensors.deliver(b"\x40\x08\xc8")
        sensors.node.send_access.side_effect = reply
        await sensors.coordinator._async_poll_sensors()
        assert sensors.entity._attr_is_on is False
        assert sensors.entity._attr_available
        assert sensors.coordinator.sensor_values[(12, "motion")].raw == b"\x00"
        assert not sensors.node._vendor_requests

    asyncio.run(run())


def test_concurrent_property_reads_accept_only_their_own_record(sensors):
    async def run():
        reads = [
            asyncio.create_task(sensors.coordinator.async_get_sensor(12, prop))
            for prop in (0x0042, 0x004D)
        ]
        await asyncio.sleep(0)
        await sensors.deliver(b"\xa0\x09\x00")
        assert not sensors.node._vendor_requests[0][-1].done()
        assert sensors.node._vendor_requests[1][-1].done()
        await sensors.deliver(b"\x40\x08\xc8")
        assert await asyncio.gather(*reads) == [b"\xc8", b"\x00"]
        assert not sensors.node._vendor_requests

    asyncio.run(run())


@pytest.mark.parametrize("opcode", [0xD16305, 0x52])
def test_multi_property_format_b_and_sig_publications(sensors, opcode):
    async def run():
        await sensors.entity.async_added_to_hass()
        await sensors.deliver(b"\x01\x42\x00\x00\x01\x4d\x00\x01", opcode)
        assert sensors.entity._attr_is_on is False
        assert sensors.coordinator.sensor_values[(12, "presence")].value is True

    asyncio.run(run())


def test_malformed_trailing_record_does_not_partially_change_state(sensors):
    async def run():
        await sensors.entity.async_added_to_hass()
        await sensors.deliver(b"\x40\x08\xc8")
        await sensors.deliver(b"\x40\x08\x00\x01")
        assert sensors.entity._attr_is_on is True

    asyncio.run(run())


def test_unrelated_vendor_opcode_does_not_update_motion(sensors):
    async def run():
        await sensors.entity.async_added_to_hass()
        await sensors.deliver(b"\x40\x08\x00")
        for opcode in (0xD06305, 0xDC6305, 0xD56305):
            await sensors.deliver(b"\x42\x00\x01", opcode)
        assert sensors.entity._attr_is_on is False

    asyncio.run(run())


def test_sensor_read_timeout_cleans_up_request(sensors):
    async def run():
        with pytest.raises(TimeoutError):
            await sensors.coordinator.async_get_sensor(12, 0x0042, timeout=0.001)
        assert not sensors.node._vendor_requests

    asyncio.run(run())


def test_sensor_discovery_uses_decoded_values(sensors):
    coordinator = sensors.coordinator
    coordinator.elements = [
        types.SimpleNamespace(address=12, vendor_models={(0x0563, 0x1003)})
    ]
    coordinator._update_entry = Mock()

    async def reply(address, opcode, parameters):
        prop = int.from_bytes(parameters, "little")
        # Format B length=1 only for motion; length=0 means unsupported.
        payload = (
            b"\x01" + parameters + b"\x00" if prop == 0x0042 else b"\xff" + parameters
        )
        await sensors.deliver(payload)

    sensors.node.send_access.side_effect = reply
    asyncio.run(coordinator._async_detect_sensor_properties())
    coordinator._update_entry.assert_called_once_with(
        {"sensor_properties": {"12": ["motion"]}, "sensor_schema_version": 3}
    )
