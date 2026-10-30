"""Regression coverage for the device behavior observed on L 810 SC 1.1.30."""

import asyncio
import types
from unittest.mock import AsyncMock, Mock

import pytest
from test_import_setup import integration as integration


def device(integration, product=7801):
    cls = integration.coordinator.SteinelCoordinator
    result = object.__new__(cls)
    result.entry = types.SimpleNamespace(
        data={"product_id": product, "unicast_address": 256}, options={}
    )
    result.node = types.SimpleNamespace(
        request=AsyncMock(), request_vendor=AsyncMock(), send_access=AsyncMock()
    )
    result.async_ensure_connected = AsyncMock(return_value=result.node)
    result._arm_idle_disconnect = Mock()
    result._sensor_request_lock = asyncio.Lock()
    return result


def test_illuminance_uses_unfiltered_standard_status(integration):
    coordinator = device(integration)
    coordinator.node.request.return_value = (259, bytes.fromhex("c40990dc01"))
    raw = asyncio.run(coordinator.async_get_sensor(259, 0x004E))
    assert raw == bytes.fromhex("90dc01")
    assert coordinator.node.request.call_args.args[:4] == (259, 0x8231, b"", 0x52)
    coordinator.node.request_vendor.assert_not_awaited()


def test_other_models_keep_vendor_sensor_requests(integration):
    coordinator = device(integration, product=1234)
    coordinator.node.request_vendor.return_value = (
        258,
        0xD16305,
        bytes.fromhex("4008c8"),
    )
    assert asyncio.run(coordinator.async_get_sensor(258, 0x0042)) == b"\xc8"
    coordinator.node.request.assert_not_awaited()


def test_motion_and_lux_are_routed_to_separate_lc_element(integration):
    coordinator = device(integration)

    async def run():
        await coordinator._async_forward_l810_motion(b"\xc8")
        await coordinator._async_forward_l810_illuminance(bytes.fromhex("90dc01"))
        await coordinator._async_forward_l810_illuminance(b"\xff\xff\xff")

    asyncio.run(run())
    assert coordinator.node.send_access.await_count == 2
    assert coordinator.node.send_access.call_args_list[0].args == (
        257,
        0x52,
        bytes.fromhex("4008c8"),
    )
    assert coordinator.node.send_access.call_args_list[1].args == (
        257,
        0x52,
        bytes.fromhex("c40990dc01"),
    )


def test_motion_range_edit_preserves_other_zone_and_reserved_bytes(integration):
    coordinator = device(integration)
    coordinator._async_sensor_setting_request = AsyncMock(
        side_effect=[bytes.fromhex("c800007a"), b"ok"]
    )
    asyncio.run(coordinator.async_set_sensor_range(258, 1, 80))
    assert coordinator._async_sensor_setting_request.call_args.args[
        -1
    ] == bytes.fromhex("c80000a0")


def test_sequence_journal_reservations_survive_restart(integration, tmp_path):
    store_type = integration.coordinator.SequenceStore
    hass = types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts)))
    )
    first = store_type(hass, "test", 100)
    start, end, migrated = first._reserve()
    assert migrated and start == 100 + 65536 and end == start + 4096
    second = store_type(hass, "test", 100)
    next_start, _, migrated = second._reserve()
    assert not migrated and next_start == end


def test_corrupted_sequence_journal_is_not_silently_recreated(integration, tmp_path):
    hass = types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts)))
    )
    store = integration.coordinator.SequenceStore(hass, "test", 100)
    store._path.parent.mkdir(parents=True)
    store._path.write_text('{"version":1,"reserved_until":false}')
    with pytest.raises(ValueError):
        store._reserve()
