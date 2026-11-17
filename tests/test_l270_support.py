"""L 270 SC discovery, native publications and configuration acknowledgements."""

from __future__ import annotations

import asyncio
import importlib
import types
from unittest.mock import AsyncMock, Mock

import pytest
from test_import_setup import integration as integration


@pytest.fixture
def lamp(integration):
    module = integration.coordinator
    mesh = importlib.import_module(f"{module.__package__}.mesh")
    entry = types.SimpleNamespace(
        entry_id="test",
        title="L 270 SC",
        options={},
        data={
            "address": "AA:BB:CC:DD:EE:FF",
            "product_id": 7643,
            "unicast_address": 256,
            "source_address": 0x7FFE,
            "sensor_properties": {"258": ["motion"]},
        },
    )
    hass = types.SimpleNamespace(data={})
    coordinator = module.SteinelCoordinator(hass, entry)
    coordinator.elements = [
        mesh.ElementComposition(256, {0x1000, 0x1300}, set()),
        mesh.ElementComposition(257, {0x130F, 0x1310}, {(0x0563, 0x1001)}),
        mesh.ElementComposition(258, set(), {(0x0563, 0x1003)}),
        mesh.ElementComposition(259, set(), set()),
    ]
    coordinator._update_entry = Mock(
        side_effect=lambda values: entry.data.update(values)
    )
    coordinator._schedule_state_persist = Mock()
    coordinator._arm_idle_disconnect = Mock()
    publications = {}

    def unconfigured(address, model):
        return (
            address.to_bytes(2, "little")
            + b"\x00" * 4
            + b"\xff\x00\x00"
            + model.to_bytes(2, "little")
        )

    async def reply(dst, opcode, parameters, expected, **kwargs):
        assert dst in (256, 259)
        if opcode == 0x803D:
            return dst, b"\x00" + parameters
        if opcode == 0x8231:
            assert parameters == b""  # Lux requires an unfiltered Sensor Get.
            return dst, bytes.fromhex("c409b04302")
        address = int.from_bytes(parameters[:2], "little")
        model = int.from_bytes(parameters[-2:], "little")
        key = (address, model)
        if opcode == 0x03:
            publications[key] = parameters
        else:
            assert opcode == 0x8018
        return dst, b"\x00" + publications.get(key, unconfigured(address, model))

    node = types.SimpleNamespace(
        request=AsyncMock(side_effect=reply),
        request_vendor=AsyncMock(return_value=(258, 0xD16305, b"\x40\x08\xc8")),
    )
    coordinator.node = node
    coordinator.async_ensure_connected = AsyncMock(return_value=node)
    return types.SimpleNamespace(
        coordinator=coordinator,
        entry=entry,
        node=node,
        publications=publications,
        mesh=mesh,
        unconfigured=unconfigured,
    )


def test_l270_discovers_lux_and_configures_native_routing(lamp):
    asyncio.run(lamp.coordinator._async_setup_l810_sensors())
    assert lamp.entry.data["sensor_properties"] == {
        "258": ["motion"],
        "259": ["illuminance"],
    }
    assert lamp.coordinator.sensor_values[(259, "illuminance")].value == 1484
    assert lamp.publications == {
        (258, 0x1100): bytes.fromhex("020101010000ff44000011"),
        (259, 0x1100): bytes.fromhex("030101010000ff4a000011"),
        (256, 0x1300): bytes.fromhex("0001fe7f0000ff00000013"),
    }
    assert all(
        record["verified"] for record in lamp.coordinator.model_publications.values()
    )
    assert all(
        0x1100 in element.sig_models for element in lamp.coordinator.elements[2:]
    )
    assert not any("publication" in key for key in lamp.entry.data)


def test_matching_publications_are_read_without_rewriting(lamp):
    asyncio.run(lamp.coordinator._async_setup_l270_publications())
    lamp.node.request.reset_mock()
    asyncio.run(lamp.coordinator._async_setup_l270_publications())
    assert [call.args[1] for call in lamp.node.request.call_args_list] == [0x8018] * 3
    assert all(
        not record["updated"] for record in lamp.coordinator.model_publications.values()
    )


def test_concurrent_publication_checks_are_serialized(lamp):
    reply = lamp.node.request.side_effect
    active = 0
    maximum = 0

    async def yielding_reply(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0)
            return await reply(*args, **kwargs)
        finally:
            active -= 1

    lamp.node.request.side_effect = yielding_reply

    async def run():
        await asyncio.gather(
            lamp.coordinator._async_setup_l270_publications(),
            lamp.coordinator._async_setup_l270_publications(),
        )

    asyncio.run(run())
    assert maximum == 1
    assert sum(call.args[1] == 0x03 for call in lamp.node.request.call_args_list) == 3


def test_failed_motion_binding_does_not_prevent_other_publications(lamp):
    reply = lamp.node.request.side_effect

    async def reject_motion(dst, opcode, parameters, *args, **kwargs):
        if opcode == 0x803D and parameters[:2] == b"\x02\x01":
            return dst, b"\x02" + parameters
        return await reply(dst, opcode, parameters, *args, **kwargs)

    lamp.node.request.side_effect = reject_motion
    asyncio.run(lamp.coordinator._async_setup_l270_publications())
    assert not lamp.coordinator.model_publications["0102_1100"]["verified"]
    assert set(lamp.publications) == {(259, 0x1100), (256, 0x1300)}


@pytest.mark.parametrize(
    "status",
    [
        b"\x00",
        b"\x00" + b"\xff" * 11,
        b"\x02" + bytes.fromhex("000100000000ff00000013"),
    ],
)
def test_invalid_get_does_not_trigger_a_publication_write(lamp, status):
    lamp.node.request.side_effect = None
    lamp.node.request.return_value = (256, status)
    asyncio.run(lamp.coordinator._async_ensure_l270_publication(256, 0x1300, 0x7FFE, 0))
    assert lamp.node.request.await_count == 1
    assert not lamp.coordinator.model_publications["0100_1300"]["verified"]


def test_lost_publications_are_repaired_despite_old_once_flags(lamp):
    asyncio.run(lamp.coordinator._async_setup_l270_publications())
    lamp.entry.data.update(l270_lc_publication=1, l270_state_publication=1)
    lamp.publications.clear()
    lamp.node.request.reset_mock()
    asyncio.run(lamp.coordinator._async_setup_l270_publications())
    assert sum(call.args[1] == 0x03 for call in lamp.node.request.call_args_list) == 3
    assert len(lamp.publications) == 3


def test_imported_l270_reads_bound_lux_without_any_configuration_writes(lamp):
    lamp.entry.data["existing_mesh"] = True
    lamp.coordinator.elements[3].sig_models.add(0x1100)
    asyncio.run(lamp.coordinator._async_setup_l810_sensors())
    assert [call.args[1] for call in lamp.node.request.call_args_list] == [0x8231]
    assert lamp.coordinator.sensor_values[(259, "illuminance")].value == 1484
    assert not lamp.publications
    assert not lamp.coordinator.model_publications


def test_imported_l270_does_not_bind_unbound_sensor_models(lamp):
    lamp.entry.data["existing_mesh"] = True
    asyncio.run(lamp.coordinator._async_setup_l810_sensors())
    lamp.node.request.assert_not_awaited()
    lamp.node.request_vendor.assert_not_awaited()
    assert lamp.entry.data["sensor_properties"] == {"258": ["motion"]}


def test_l270_motion_stays_on_vendor_protocol(lamp):
    assert asyncio.run(lamp.coordinator.async_get_sensor(258, 0x0042)) == b"\xc8"
    lamp.node.request_vendor.assert_awaited_once()
    lamp.node.request.assert_not_awaited()


def test_other_products_do_not_receive_l270_publications(lamp):
    lamp.entry.data["product_id"] = 7801
    asyncio.run(lamp.coordinator._async_setup_l270_publications())
    lamp.node.request.assert_not_awaited()


def test_absent_elements_are_not_configured(lamp):
    lamp.coordinator.elements = lamp.coordinator.elements[:2]
    asyncio.run(lamp.coordinator._async_setup_l270_publications())
    assert set(lamp.publications) == {(256, 0x1300)}


@pytest.mark.parametrize(
    "reply", [b"\x00", b"\x00" + b"\xff" * 6, b"\x02\x02\x01\x00\x00\x00\x11"]
)
def test_invalid_bind_never_marks_sensor_as_bound(lamp, reply):
    lamp.node.request.return_value = (256, reply)
    lamp.node.request.side_effect = None
    with pytest.raises(ValueError, match="binding was not acknowledged"):
        asyncio.run(
            lamp.coordinator._async_bind_sensor_server(lamp.coordinator.elements[2])
        )
    assert 0x1100 not in lamp.coordinator.elements[2].sig_models


@pytest.mark.parametrize(
    "reply",
    [
        b"\x00",  # Truncated.
        b"\x00" + b"\xff" * 11,  # Different element/model.
        b"\x02" + bytes.fromhex("0001fe7f0000ff00000013"),  # Rejected.
        b"\x00" + bytes.fromhex("0001fd7f0000ff00000013"),  # Wrong target.
        b"\x00" + bytes.fromhex("0001fe7f0100ff00000013"),  # Wrong AppKey.
        b"\x00" + bytes.fromhex("0001fe7f0000ff01000013"),  # Wrong period.
    ],
)
def test_invalid_set_reply_is_not_cached_as_success(lamp, reply):
    lamp.node.request.side_effect = [
        (256, b"\x00" + lamp.unconfigured(256, 0x1300)),
        (256, reply),
    ]
    asyncio.run(lamp.coordinator._async_ensure_l270_publication(256, 0x1300, 0x7FFE, 0))
    record = lamp.coordinator.model_publications["0100_1300"]
    assert not record["verified"]
    assert "error" in record


def test_wrong_source_is_not_accepted_even_with_matching_status(lamp):
    lamp.node.request.side_effect = [(257, b"\x00" + lamp.unconfigured(256, 0x1300))]
    asyncio.run(lamp.coordinator._async_ensure_l270_publication(256, 0x1300, 0x7FFE, 0))
    assert lamp.node.request.await_count == 1
    assert not lamp.coordinator.model_publications["0100_1300"]["verified"]


def test_publication_timeout_is_recorded_and_retried_next_connection(lamp):
    reply = lamp.node.request.side_effect
    lamp.node.request.side_effect = TimeoutError()
    asyncio.run(lamp.coordinator._async_ensure_l270_publication(256, 0x1300, 0x7FFE, 0))
    assert lamp.coordinator.model_publications["0100_1300"]["error"] == "TimeoutError"
    lamp.node.request.side_effect = reply
    asyncio.run(lamp.coordinator._async_ensure_l270_publication(256, 0x1300, 0x7FFE, 0))
    assert lamp.coordinator.model_publications["0100_1300"]["verified"]
    assert "error" not in lamp.coordinator.model_publications["0100_1300"]


def test_authenticated_late_or_unrelated_publication_replies_are_ignored(lamp):
    """Exercise the real Mesh response matcher with encrypted Device Key replies."""
    mesh = lamp.mesh
    node = mesh.MeshNode(None, bytes(16), b"\x01" * 16, b"\x02" * 16, 0, 0, 256, Mock())
    node.src = 0x7FFE
    lamp.coordinator.node = node
    sequence = 0

    async def deliver(parameters):
        nonlocal sequence
        sequence += 1
        upper = mesh.upper_transport_encrypt(
            node.device_key, 2, sequence, 256, node.src, 0, b"\x80\x19" + parameters
        )
        await node._handle_upper(256, node.src, sequence, 0, 0, 0, upper)

    async def send(dst, opcode, parameters, **kwargs):
        assert dst == 256 and kwargs["device_key"]
        for bad in (b"\x00", b"\x00" + b"\xff" * 11):
            await deliver(bad)
            assert not node._requests[0][-1].done()
        if opcode == 0x8018:
            await deliver(b"\x00" + lamp.unconfigured(256, 0x1300))
        else:
            assert opcode == 0x03
            wrong_target = bytearray(parameters)
            wrong_target[2:4] = b"\xfd\x7f"
            await deliver(b"\x00" + wrong_target)
            assert not node._requests[0][-1].done()
            await deliver(b"\x00" + parameters)

    node.send_access = AsyncMock(side_effect=send)
    asyncio.run(lamp.coordinator._async_ensure_l270_publication(256, 0x1300, 0x7FFE, 0))
    assert lamp.coordinator.model_publications["0100_1300"]["verified"]
    assert not node._requests


@pytest.mark.parametrize(
    "level,on,brightness", [(0, False, 0), (32768, True, 128), (65535, True, 255)]
)
def test_native_lightness_status_updates_ha_state(lamp, level, on, brightness):
    lamp.coordinator._access_received(256, 0x824E, level.to_bytes(2, "little"))
    assert lamp.coordinator.is_on[256] is on
    assert lamp.coordinator.brightness[256] == brightness


def test_reconnect_verifies_l270_publications_before_marking_available(
    lamp, monkeypatch
):
    coordinator = lamp.coordinator
    coordinator._disconnect_event.set()
    coordinator.available = False
    coordinator._async_connect_proxy = AsyncMock()
    coordinator.hass.config_entries = types.SimpleNamespace(
        async_schedule_reload=Mock()
    )
    coordinator._async_setup_l270_publications = AsyncMock()

    async def verify():
        assert not coordinator.available
        coordinator._shutting_down = True

    coordinator._async_setup_l270_publications.side_effect = verify
    monkeypatch.setattr(lamp.mesh.asyncio, "sleep", AsyncMock())
    asyncio.run(coordinator._async_reconnect_monitor())
    coordinator._async_setup_l270_publications.assert_awaited_once()
    assert coordinator.available
