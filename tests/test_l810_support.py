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
    result._lc_lock = asyncio.Lock()
    result._notify = Mock()
    result.sensor_settings = {}
    result.sensor_values = {}
    result.lc_modes = {}
    result._light_commands = 0
    result._shutting_down = False
    result._l810_forwarded_motion = None
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


def test_profile_read_uses_existing_lc_lock(integration):
    coordinator = device(integration)
    coordinator.node.request_vendor.return_value = (257, 0xC76305, b"\x1f\x00\x04\x00")
    assert asyncio.run(coordinator.async_get_l810_profile()) == 4
    assert coordinator.l810_profile == 4


def test_profile_changes_require_scene_and_profile_acknowledgements(integration):
    coordinator = device(integration)
    coordinator.node.next_tid = Mock(return_value=7)
    coordinator.node.request.side_effect = [
        (256, b"\x00\x03\x00"),
        (257, b"\x01"),
        (257, b"\x01"),
    ]
    coordinator.node.request_vendor.return_value = (257, 0xC76305, b"\x1f\x00\x03\x00")
    asyncio.run(coordinator.async_set_l810_profile(3))
    assert coordinator.node.request.call_args_list[0].args == (
        256,
        0x8242,
        b"\x03\x00\x07",
        0x5E,
    )
    assert coordinator.lc_modes == {(257, False): True, (257, True): True}
    assert coordinator.l810_profile == 3


def test_profile_rejection_does_not_report_success(integration):
    coordinator = device(integration)
    coordinator.node.next_tid = Mock(return_value=0)
    coordinator.node.request.return_value = (256, b"\x01\x00\x00")
    with pytest.raises(ValueError, match="scene was not acknowledged"):
        asyncio.run(coordinator.async_set_l810_profile(4))
    coordinator.node.request_vendor.assert_not_awaited()


def test_imported_l810_discovery_does_not_bind_unavailable_models(integration):
    coordinator = device(integration)
    coordinator.entry.data["existing_mesh"] = True
    coordinator.elements = [types.SimpleNamespace(address=258, sig_models=set())]
    coordinator._update_entry = Mock()
    asyncio.run(coordinator._async_setup_l810_sensors())
    coordinator.node.request.assert_not_awaited()
    coordinator.node.request_vendor.assert_not_awaited()
    coordinator._update_entry.assert_not_called()


def test_imported_l810_discovery_only_reads_bound_sensor_elements(integration):
    coordinator = device(integration)
    coordinator.entry.data["existing_mesh"] = True
    coordinator.elements = [
        types.SimpleNamespace(address=258, sig_models={0x1100}),
        types.SimpleNamespace(address=259, sig_models={0x1100}),
    ]
    coordinator._update_entry = Mock()
    coordinator.node.request.side_effect = [
        (258, b"\x40\x08\x00"),
        (259, bytes.fromhex("c40990dc01")),
    ]
    asyncio.run(coordinator._async_setup_l810_sensors())
    assert coordinator.node.request.await_count == 2
    assert all(
        call.args[1] == 0x8231 for call in coordinator.node.request.call_args_list
    )
    coordinator._update_entry.assert_called_once_with(
        {"sensor_properties": {"258": ["motion"], "259": ["illuminance"]}}
    )


def test_motion_forwarding_retries_after_failed_send(integration, monkeypatch):
    coordinator = device(integration)
    coordinator.async_get_sensor = AsyncMock(return_value=b"\xc8")
    coordinator._async_forward_l810_motion = AsyncMock(
        side_effect=[ConnectionError(), None]
    )
    cycles = 0

    async def sleep(delay):
        nonlocal cycles
        if delay == 0.25:
            cycles += 1
            if cycles == 2:
                coordinator._shutting_down = True

    monkeypatch.setattr(integration.coordinator.asyncio, "sleep", sleep)
    asyncio.run(coordinator._async_l810_motion_poll_loop())
    assert coordinator._async_forward_l810_motion.await_count == 2
    assert coordinator._l810_forwarded_motion == b"\xc8"


def test_shared_polling_leaves_l810_motion_to_dedicated_loop(integration):
    coordinator = device(integration)
    coordinator.entry.data["sensor_properties"] = {
        "258": ["motion"],
        "259": ["illuminance"],
    }
    coordinator.async_get_sensor = AsyncMock(return_value=b"\x00\x00\x00")
    coordinator._async_forward_l810_illuminance = AsyncMock()
    asyncio.run(coordinator._async_poll_sensors())
    coordinator.async_get_sensor.assert_awaited_once_with(259, 0x004E)
    assert (258, "motion") not in coordinator.sensor_values


@pytest.mark.parametrize("value", [-1, 101, float("nan"), float("inf")])
def test_invalid_range_never_reaches_the_lamp(integration, value):
    coordinator = device(integration)
    with pytest.raises(ValueError):
        asyncio.run(coordinator.async_set_sensor_range(258, 0, value))
    coordinator.async_ensure_connected.assert_not_awaited()


def test_sequence_allocations_are_unique_across_concurrent_requests_and_restart(
    integration, tmp_path
):
    hass = types.SimpleNamespace(
        config=types.SimpleNamespace(
            path=lambda *parts: str(tmp_path.joinpath(*parts))
        ),
        async_add_executor_job=lambda job: asyncio.to_thread(job),
    )
    first = integration.coordinator.SequenceStore(hass, "test", 100)

    async def run():
        values = await asyncio.gather(*(first.async_next() for _ in range(50)))
        assert len(set(values)) == 50
        assert values == list(range(65636, 65686))
        second = integration.coordinator.SequenceStore(hass, "test", 100)
        assert await second.async_next() == 65636 + 4096

    asyncio.run(run())


def test_sequence_storage_failure_stays_failed_until_reload(integration, tmp_path):
    hass = types.SimpleNamespace(
        config=types.SimpleNamespace(
            path=lambda *parts: str(tmp_path.joinpath(*parts))
        ),
        async_add_executor_job=lambda job: asyncio.to_thread(job),
    )
    store = integration.coordinator.SequenceStore(hass, "test", 0)
    store._reserve = Mock(side_effect=OSError("Disk full"))

    async def run():
        with pytest.raises(ConnectionError, match="Cannot safely reserve"):
            await store.async_next()
        with pytest.raises(ConnectionError, match="reload integration"):
            await store.async_next()
        store._reserve.assert_called_once()

    asyncio.run(run())
