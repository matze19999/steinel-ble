"""LC controls for nodes with separate controller and sensor elements."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, Mock

import pytest
from test_import_setup import integration as integration


@pytest.fixture
def controls(integration, monkeypatch):
    class Entity:
        _attr_native_value = None
        _attr_is_on = None

        async def async_added_to_hass(self):
            pass

        def async_on_remove(self, callback):
            self.remove_callback = callback

        def async_write_ha_state(self):
            pass

    modules = {}
    for name in (
        "homeassistant.components.number",
        "homeassistant.components.switch",
        "homeassistant.helpers.entity",
        "homeassistant.helpers.entity_registry",
        "homeassistant.helpers.entity_platform",
        "homeassistant.helpers.restore_state",
    ):
        module = modules[name] = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
        parent, attr = name.rsplit(".", 1)
        monkeypatch.setattr(sys.modules[parent], attr, module, raising=False)

    class RestoreEntity:
        async def async_get_last_state(self):
            return None

    modules["homeassistant.helpers.restore_state"].RestoreEntity = RestoreEntity
    modules["homeassistant.components.number"].NumberEntity = Entity
    modules["homeassistant.components.number"].NumberMode = types.SimpleNamespace(
        BOX="box", SLIDER="slider"
    )
    modules["homeassistant.components.switch"].SwitchEntity = Entity
    modules["homeassistant.helpers.entity"].EntityCategory = types.SimpleNamespace(
        CONFIG="config"
    )
    modules[
        "homeassistant.helpers.entity_platform"
    ].AddConfigEntryEntitiesCallback = object
    sys.modules["homeassistant.const"].PERCENTAGE = "%"
    sys.modules["homeassistant.const"].UnitOfTime = types.SimpleNamespace(SECONDS="s")
    registry = types.SimpleNamespace(
        async_get_entity_id=Mock(return_value=None), async_remove=Mock()
    )
    modules["homeassistant.helpers.entity_registry"].async_get = lambda _: registry
    package = sys.modules[integration.coordinator.__package__]
    package.SteinelConfigEntry = types.SimpleNamespace
    number = importlib.import_module(f"{package.__name__}.number")
    switch = importlib.import_module(f"{package.__name__}.switch")

    hass = types.SimpleNamespace(data={})
    entry = types.SimpleNamespace(
        title="Test lamp",
        options={},
        data={
            "address": "AA:BB:CC:DD:EE:FF",
            "unicast_address": 10,
            "sensor_properties": {"12": ["motion"], "13": ["illuminance"]},
        },
    )
    coordinator = integration.coordinator.SteinelCoordinator(hass, entry)
    coordinator.elements = [
        types.SimpleNamespace(address=10, sig_models={0x1000, 0x1300}),
        types.SimpleNamespace(address=11, sig_models={0x130F, 0x1310}),
        types.SimpleNamespace(address=12, sig_models={0x1100}),
        types.SimpleNamespace(address=13, sig_models={0x1100}),
    ]
    coordinator.available = True
    coordinator.transport = types.SimpleNamespace(
        client=types.SimpleNamespace(is_connected=True)
    )
    coordinator._schedule_state_persist = Mock()
    coordinator._arm_idle_disconnect = Mock()
    values = {
        0x002E: b"\xff\xff",
        0x002F: round(10 * 65535 / 100).to_bytes(2, "little"),
        0x003C: (60000).to_bytes(3, "little"),
        0x003B: (120000).to_bytes(3, "little"),
        0x002B: (2000).to_bytes(3, "little"),
    }
    counters = {"active": 0, "maximum": 0}

    async def reply(address, opcode, parameters, _expected):
        assert address == 11
        counters["active"] += 1
        counters["maximum"] = max(counters["maximum"], counters["active"])
        try:
            await asyncio.sleep(0)
            if opcode in (0x829D, 0x62):
                prop = int.from_bytes(parameters[:2], "little")
                if opcode == 0x62:
                    values[prop] = parameters[2:]
                return address, parameters[:2] + values[prop]
            if opcode in (0x8291, 0x8295):
                return address, b"\x01"
            return address, parameters
        finally:
            counters["active"] -= 1

    node = types.SimpleNamespace(request=AsyncMock(side_effect=reply))
    coordinator.node = node
    coordinator.async_ensure_connected = AsyncMock(return_value=node)
    entry.runtime_data = coordinator
    return types.SimpleNamespace(
        coordinator=coordinator,
        number=number,
        switch=switch,
        entry=entry,
        hass=hass,
        registry=registry,
        node=node,
        counters=counters,
        error=integration.coordinator.HomeAssistantError,
    )


def _entities(controls):
    numbers, switches = [], []
    asyncio.run(
        controls.number.async_setup_entry(controls.hass, controls.entry, numbers.extend)
    )
    asyncio.run(
        controls.switch.async_setup_entry(
            controls.hass, controls.entry, switches.extend
        )
    )
    return numbers, switches


def test_separate_sensor_elements_expose_five_numbers_and_two_switches(controls):
    numbers, switches = _entities(controls)
    assert len(numbers) == 5
    assert len(switches) == 2
    assert {entity.address for entity in numbers + switches} == {11}
    assert not controls.coordinator.supports_sensor_lighting(12)
    controls.node.request.assert_not_awaited()


def test_l810_adds_sensor_settings_without_adding_them_to_other_models(controls):
    controls.entry.data["product_id"] = 7801
    numbers, _ = _entities(controls)
    assert len(numbers) == 10
    assert (
        sum(isinstance(n, controls.number.SteinelSensorSettingNumber) for n in numbers)
        == 3
    )


def test_l810_restores_confirmed_threshold_on_firmware_placeholder(controls):
    controls.entry.data["product_id"] = 7801
    description = next(
        d for d in controls.number.DESCRIPTIONS if d.key == "light_threshold"
    )
    entity = controls.number.SteinelLCNumber(controls.coordinator, 11, description)
    entity.async_get_last_state = AsyncMock(
        return_value=types.SimpleNamespace(state="82")
    )
    controls.coordinator.async_get_lc_property = AsyncMock(
        return_value=(6553500).to_bytes(3, "little")
    )
    controls.coordinator.async_set_lc_property = AsyncMock(
        return_value=(8200).to_bytes(3, "little")
    )
    asyncio.run(entity.async_added_to_hass())
    controls.coordinator.async_set_lc_property.assert_awaited_once_with(
        11, 0x002B, (8200).to_bytes(3, "little")
    )
    assert entity._attr_native_value == 82


@pytest.mark.parametrize("sensors", [{"12": ["motion"]}, {"12": ["presence"]}])
def test_lux_publication_is_not_required_for_lc_settings(controls, sensors):
    controls.entry.data["sensor_properties"] = sensors
    assert controls.coordinator.supports_sensor_lighting(11)


@pytest.mark.parametrize("sensors", [{}, {"12": ["temperature"]}, {"99": ["motion"]}])
def test_nodes_without_own_occupancy_sensing_do_not_expose_controls(controls, sensors):
    controls.entry.data["sensor_properties"] = sensors
    assert _entities(controls) == ([], [])


def test_numbers_require_bound_setup_server_but_modes_do_not(controls):
    controls.coordinator.elements[1].sig_models = {0x130F}
    numbers, switches = _entities(controls)
    assert not numbers
    assert len(switches) == 2


def test_initial_queries_are_serialized_and_never_change_settings(controls):
    numbers, switches = _entities(controls)

    async def run():
        await asyncio.gather(
            *(entity.async_added_to_hass() for entity in numbers + switches)
        )

    asyncio.run(run())
    assert controls.counters["maximum"] == 1
    assert {
        entity.description.key: entity._attr_native_value for entity in numbers
    } == {
        "main_light": 100,
        "basic_light": 10,
        "main_light_time": 60,
        "basic_light_time": 120,
        "light_threshold": 20,
    }
    assert all(entity._attr_is_on for entity in switches)
    assert {call.args[1] for call in controls.node.request.await_args_list} == {
        0x829D,
        0x8291,
        0x8295,
    }


def test_full_lightness_is_a_valid_value_and_unknown_lux_is_none(controls):
    numbers, _switches = _entities(controls)
    main, _basic, _main_time, _basic_time, lux = numbers
    assert main._decode(b"\xff\xff") == 100
    assert main._encode(100) == b"\xff\xff"
    assert lux._decode(b"\xff\xff\xff") is None
    with pytest.raises(ValueError):
        main._decode(b"\xff")


def test_wrong_property_status_is_not_misinterpreted(controls):
    controls.node.request.side_effect = None
    controls.node.request.return_value = (11, b"\x2f\x00\x01\x00")
    with pytest.raises(controls.error, match="Unexpected Light LC property"):
        asyncio.run(controls.coordinator.async_get_lc_property(11, 0x002E))
    assert not controls.coordinator.lc_properties
    controls.coordinator._arm_idle_disconnect.assert_called_once()


def test_number_uses_acknowledged_value_instead_of_requested_value(controls):
    main = _entities(controls)[0][0]
    controls.node.request.side_effect = None
    acknowledged = round(30 * 65535 / 100).to_bytes(2, "little")
    controls.node.request.return_value = (11, b"\x2e\x00" + acknowledged)
    asyncio.run(main.async_set_native_value(35))
    assert main._attr_native_value == 30
    assert controls.node.request.await_args.args[1] == 0x62


def test_switch_uses_acknowledged_state(controls):
    automatic = _entities(controls)[1][0]
    controls.node.request.side_effect = None
    controls.node.request.return_value = (11, b"\x00")
    asyncio.run(automatic.async_turn_on())
    assert automatic._attr_is_on is False
    controls.node.request.assert_awaited_once_with(11, 0x8292, b"\x01", 0x8294)


def test_config_entities_follow_connection_state_and_live_status(controls):
    numbers, switches = _entities(controls)

    async def run():
        await asyncio.gather(
            *(entity.async_added_to_hass() for entity in numbers + switches)
        )

    asyncio.run(run())
    controls.coordinator.available = False
    controls.coordinator._notify()
    assert not any(entity.available for entity in numbers + switches)
    controls.coordinator.available = True
    controls.coordinator._access_received(11, 0x8294, b"\x00")
    controls.coordinator._access_received(11, 0x64, b"\x2e\x00\x00\x80")
    assert all(entity.available for entity in numbers + switches)
    assert switches[0]._attr_is_on is False
    assert numbers[0]._attr_native_value == 50


def test_timeout_does_not_optimistically_change_a_setting(controls):
    main = _entities(controls)[0][0]
    asyncio.run(main.async_added_to_hass())
    assert main._attr_native_value == 100
    controls.node.request.side_effect = TimeoutError()
    with pytest.raises(controls.error, match="status response"):
        asyncio.run(main.async_set_native_value(50))
    assert main._attr_native_value == 100
    assert controls.coordinator.available


def test_live_property_status_recovers_an_initially_unavailable_control(controls):
    main = _entities(controls)[0][0]
    controls.node.request.side_effect = TimeoutError()
    asyncio.run(main.async_added_to_hass())
    assert not main.available
    controls.coordinator._access_received(11, 0x64, b"\x2e\x00\xff\xff")
    assert main.available
    assert main._attr_native_value == 100


def test_invalid_mode_status_does_not_enable_automation(controls):
    controls.node.request.side_effect = None
    controls.node.request.return_value = (11, b"\x02")
    with pytest.raises(controls.error, match="Invalid Light LC mode"):
        asyncio.run(controls.coordinator.async_get_lc_mode(11, False))
    assert not controls.coordinator.lc_modes


@pytest.mark.parametrize("value", [-1, 0, 101, float("nan")])
def test_invalid_main_light_value_does_not_send_a_write(controls, value):
    main = _entities(controls)[0][0]
    with pytest.raises(ValueError):
        asyncio.run(main.async_set_native_value(value))
    controls.node.request.assert_not_awaited()


def test_unsupported_property_does_not_mark_whole_device_unavailable(controls):
    main = _entities(controls)[0][0]
    controls.node.request.side_effect = None
    controls.node.request.return_value = (11, b"\x2e\x00")
    asyncio.run(main.async_added_to_hass())
    assert not main.available
    assert controls.coordinator.available
