"""Configuration and lifecycle regression tests for existing Mesh imports."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def integration(monkeypatch):
    """Supply the small Home Assistant API surface used by the tested methods."""
    names = (
        "homeassistant",
        "homeassistant.components",
        "homeassistant.components.bluetooth",
        "homeassistant.config_entries",
        "homeassistant.const",
        "homeassistant.core",
        "homeassistant.exceptions",
        "homeassistant.helpers",
        "homeassistant.helpers.device_registry",
        "homeassistant.helpers.issue_registry",
        "homeassistant.helpers.selector",
    )
    modules = {name: types.ModuleType(name) for name in names}
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
        if "." in name:
            parent, attr = name.rsplit(".", 1)
            setattr(modules[parent], attr, module)

    class Flow:
        def __init_subclass__(cls, **kwargs):
            pass

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def async_create_entry(self, **kwargs):
            return {"type": "create_entry", **kwargs}

        def async_abort(self, **kwargs):
            return {"type": "abort", **kwargs}

        async def async_set_unique_id(self, _unique_id):
            pass

        def _abort_if_unique_id_configured(self):
            pass

        def _async_current_entries(self):
            return []

    entries = modules["homeassistant.config_entries"]
    entries.ConfigFlow = Flow
    entries.OptionsFlow = Flow
    entries.ConfigFlowResult = dict
    entries.ConfigEntry = types.SimpleNamespace
    modules["homeassistant.const"].CONF_ADDRESS = "address"
    modules["homeassistant.core"].HomeAssistant = object
    modules["homeassistant.core"].callback = lambda fn: fn
    modules["homeassistant.helpers.device_registry"].DeviceInfo = dict

    class HAError(Exception):
        pass

    for name in ("ConfigEntryError", "ConfigEntryNotReady", "HomeAssistantError"):
        setattr(modules["homeassistant.exceptions"], name, HAError)

    class Selector:
        def __init__(self, config=None):
            self.config = config

        def __call__(self, value):
            return value

    selectors = modules["homeassistant.helpers.selector"]
    for prefix in ("Boolean", "Number", "Select", "Text"):
        setattr(selectors, f"{prefix}Selector", Selector)
        setattr(selectors, f"{prefix}SelectorConfig", dict)
    selectors.NumberSelectorMode = types.SimpleNamespace(BOX="box")

    name = "steinel_test_import_setup_package"
    package = types.ModuleType(name)
    package.__path__ = [
        str(Path(__file__).parents[1] / "custom_components/steinel_ble")
    ]
    monkeypatch.setitem(sys.modules, name, package)
    # Fresh modules for each fixture; no Home Assistant stubs leak to other tests.
    for existing in list(sys.modules):
        if existing.startswith(f"{name}."):
            monkeypatch.delitem(sys.modules, existing)
    return types.SimpleNamespace(
        flow=importlib.import_module(f"{name}.config_flow"),
        coordinator=importlib.import_module(f"{name}.coordinator"),
        bluetooth=modules["homeassistant.components.bluetooth"],
    )


def _entry_data():
    elements = [{"address": 2, "sig_models": [0x1000], "vendor_models": []}]
    return {
        "address": "AA:BB:CC:DD:EE:FF",
        "existing_mesh": True,
        "net_key": "11" * 16,
        "app_key": "22" * 16,
        "device_key": "33" * 16,
        "unicast_address": 2,
        "source_address": 0x7FFE,
        "iv_index": 0,
        "sequence": 0,
        "imported_elements": elements,
    }


def _coordinator(integration):
    cls = integration.coordinator.SteinelCoordinator
    coordinator = object.__new__(cls)
    coordinator.entry = types.SimpleNamespace(data=_entry_data())
    coordinator.node = types.SimpleNamespace(configure=AsyncMock())
    coordinator._update_entry = Mock(
        side_effect=lambda updates: coordinator.entry.data.update(updates)
    )
    for method in (
        "_async_provision",
        "_async_connect_proxy",
        "_async_detect_sensor_properties",
        "_async_refresh_light_states",
        "async_shutdown",
    ):
        setattr(coordinator, method, AsyncMock())
    for method in (
        "_start_advertisement_monitor",
        "_start_reconnect_monitor",
        "_start_sensor_polling",
        "_arm_idle_disconnect",
    ):
        setattr(coordinator, method, Mock())
    return coordinator


def test_import_setup_skips_provision_and_config_writes(integration):
    coordinator = _coordinator(integration)
    asyncio.run(coordinator.async_setup())
    coordinator._async_provision.assert_not_awaited()
    coordinator.node.configure.assert_not_awaited()
    coordinator._async_connect_proxy.assert_awaited_once()
    assert coordinator.available
    assert coordinator.elements[0].address == 2
    assert coordinator.entry.data["device_key"] == "33" * 16


def test_rescan_restores_bindings_without_adding_or_binding_keys(integration):
    coordinator = _coordinator(integration)
    coordinator.entry.data.pop("elements", None)
    asyncio.run(coordinator._async_configure())
    coordinator.node.configure.assert_not_awaited()
    assert (
        coordinator.entry.data["elements"]
        == coordinator.entry.data["imported_elements"]
    )


def test_owned_mesh_still_configures_models(integration):
    coordinator = _coordinator(integration)
    coordinator.entry.data["existing_mesh"] = False
    coordinator.node.configure.return_value = []
    asyncio.run(coordinator._async_configure())
    coordinator.node.configure.assert_awaited_once_with(1)


def test_import_connection_failure_never_falls_back_to_provisioning(integration):
    coordinator = _coordinator(integration)
    coordinator._async_connect_proxy.side_effect = ConnectionError("No proxy")
    with pytest.raises(integration.coordinator.ConfigEntryNotReady):
        asyncio.run(coordinator.async_setup())
    coordinator._async_provision.assert_not_awaited()
    coordinator.node.configure.assert_not_awaited()
    coordinator.async_shutdown.assert_awaited_once()


def test_incomplete_import_does_not_generate_keys_or_reset(integration):
    coordinator = _coordinator(integration)
    del coordinator.entry.data["net_key"]
    with pytest.raises(integration.coordinator.ConfigEntryError):
        asyncio.run(coordinator.async_setup())
    coordinator._update_entry.assert_not_called()
    coordinator._async_provision.assert_not_awaited()
    coordinator._start_advertisement_monitor.assert_not_called()


def test_manual_setup_routes_to_import(integration):
    flow = integration.flow.SteinelConfigFlow()
    flow.hass = object()
    integration.bluetooth.async_last_service_info = Mock(return_value=None)
    result = asyncio.run(
        flow.async_step_user({"address": "aa:bb:cc:dd:ee:ff", "existing_mesh": True})
    )
    assert result["step_id"] == "mesh_import"
    assert flow._address == "AA:BB:CC:DD:EE:FF"


def test_discovered_proxy_can_choose_import(integration):
    flow = integration.flow.SteinelConfigFlow()
    flow.context = {}
    info = types.SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="Test light",
        connectable=True,
        service_uuids=[integration.flow.MESH_PROXY_SERVICE],
        manufacturer_data={},
    )
    assert asyncio.run(flow.async_step_bluetooth(info))["step_id"] == "reset_help"
    result = asyncio.run(flow.async_step_reset_help({"existing_mesh": True}))
    assert result["step_id"] == "mesh_import"


def test_invalid_json_stays_in_import_form(integration):
    flow = integration.flow.SteinelConfigFlow()
    result = asyncio.run(flow.async_step_mesh_import({"mesh_json": "invalid"}))
    assert result["errors"] == {"base": "invalid_mesh_export"}
    assert flow._mesh_export is None


@pytest.fixture
def imported_flow(integration):
    flow = integration.flow.SteinelConfigFlow()
    flow._address = "AA:BB:CC:DD:EE:FF"
    export = {
        "netKeys": [{"index": 4, "key": "11" * 16}],
        "appKeys": [{"index": 9, "boundNetKey": 4, "key": "22" * 16}],
        "nodes": [
            {
                "name": "Test lamp",
                "unicastAddress": "000A",
                "deviceKey": "33" * 16,
                "netKeys": [{"index": 4}],
                "appKeys": [{"index": 9}],
                "elements": [
                    {"index": 0, "models": [{"modelId": "1000", "bind": [9]}]}
                ],
            }
        ],
    }
    assert (
        asyncio.run(flow.async_step_mesh_import({"mesh_json": json.dumps(export)}))[
            "step_id"
        ]
        == "mesh_node"
    )
    assert (
        asyncio.run(flow.async_step_mesh_node({"node": "000A"}))["step_id"]
        == "mesh_credentials"
    )
    return flow


def _credentials():
    return {
        "app_key_index": "9",
        "source_address": "7FFE",
        "iv_index": 0,
        "sequence": 0,
    }


def test_full_import_flow_creates_entry_and_discards_export(imported_flow):
    result = asyncio.run(imported_flow.async_step_mesh_credentials(_credentials()))
    assert result["type"] == "create_entry"
    assert result["title"] == "Test lamp"
    assert result["data"]["address"] == "AA:BB:CC:DD:EE:FF"
    assert result["data"]["source_address"] == 0x7FFE
    assert result["data"]["unicast_address"] == 10
    assert result["data"]["existing_mesh"]
    assert "mesh_json" not in result["data"]
    assert imported_flow._mesh_export is None


def test_duplicate_sender_is_rejected(imported_flow):
    imported_flow._async_current_entries = lambda: [
        types.SimpleNamespace(data={"net_key": "11" * 16, "source_address": 0x7FFE})
    ]
    result = asyncio.run(imported_flow.async_step_mesh_credentials(_credentials()))
    assert result["errors"] == {"base": "invalid_mesh_credentials"}


def test_same_mesh_node_cannot_be_imported_twice(imported_flow):
    imported_flow._async_current_entries = lambda: [
        types.SimpleNamespace(
            data={
                "net_key": "11" * 16,
                "source_address": 0x7FFD,
                "unicast_address": 10,
            }
        )
    ]
    result = asyncio.run(imported_flow.async_step_mesh_credentials(_credentials()))
    assert result == {"type": "abort", "reason": "already_configured"}


def test_proxy_uses_imported_source_before_filter_configuration(
    integration, monkeypatch
):
    coordinator = _coordinator(integration)
    coordinator.entry.options = {}
    coordinator.entry.title = "Test lamp"
    coordinator._async_ble_device = AsyncMock(return_value=object())
    transport = types.SimpleNamespace(
        connect=AsyncMock(),
        subscribe=AsyncMock(),
        has_service=lambda _: True,
        disconnect=AsyncMock(),
    )
    coordinator.address = coordinator.entry.data["address"]
    node = types.SimpleNamespace(
        src=1,
        handle_pdu=AsyncMock(),
        iv_index=0,
        synchronize_iv_index=AsyncMock(return_value=False),
        rx_stats={"network_iv_mismatch": 0},
    )

    async def initialize_filter():
        assert node.src == 0x7FFE

    node.initialize_proxy_filter = AsyncMock(side_effect=initialize_filter)
    monkeypatch.setattr(
        integration.coordinator, "MeshGattTransport", Mock(return_value=transport)
    )
    monkeypatch.setattr(integration.coordinator, "MeshNode", Mock(return_value=node))
    asyncio.run(coordinator._async_connect_proxy_attempt())
    assert coordinator.node is node
    node.initialize_proxy_filter.assert_awaited_once()


def _command_coordinator(integration):
    coordinator = _coordinator(integration)
    coordinator.address = coordinator.entry.data["address"]
    coordinator.entry.options = {}
    coordinator.available = True
    coordinator.last_error = None
    coordinator._light_commands = 0
    coordinator._shutting_down = False
    coordinator._suppress_disconnect = False
    coordinator._connection_failed = False
    coordinator._disconnect_event = asyncio.Event()
    coordinator.is_on = {}
    coordinator.brightness = {}
    coordinator.elements = []
    coordinator.sensor_values = {(2, "motion"): True}
    coordinator.transport = types.SimpleNamespace(
        client=types.SimpleNamespace(is_connected=True), disconnect=AsyncMock()
    )
    coordinator.node = types.SimpleNamespace(
        request=AsyncMock(), next_tid=Mock(return_value=0)
    )
    coordinator.async_ensure_connected = AsyncMock(return_value=coordinator.node)
    coordinator._schedule_state_persist = Mock()
    coordinator._notify = Mock()
    return coordinator


@pytest.mark.parametrize("command", ["light", "ctl", "hsl"])
def test_missing_model_reply_does_not_disable_all_sensors(integration, command):
    coordinator = _command_coordinator(integration)
    coordinator.node.request.side_effect = TimeoutError()
    if command == "light":
        call = coordinator.async_set_light(2, on=True)
    elif command == "ctl":
        call = coordinator.async_set_ctl(2, 100, 4000)
    else:
        call = coordinator.async_set_hsl(2, 100, 20, 50)
    with pytest.raises(
        integration.coordinator.HomeAssistantError, match="status response"
    ):
        asyncio.run(call)
    assert coordinator.available
    assert not coordinator._disconnect_event.is_set()
    assert coordinator.sensor_values == {(2, "motion"): True}
    assert coordinator.is_on == {}
    assert coordinator._light_commands == 0
    coordinator._notify.assert_called_once()


@pytest.mark.parametrize("connected", [False, True])
def test_transport_failure_wakes_reconnect_monitor(integration, connected):
    coordinator = _command_coordinator(integration)
    coordinator.transport.client.is_connected = connected
    coordinator.node.request.side_effect = (
        integration.coordinator.BleakError("Link failed")
        if connected
        else TimeoutError()
    )
    with pytest.raises(integration.coordinator.HomeAssistantError):
        asyncio.run(coordinator.async_set_light(2, on=True))
    assert not coordinator.available
    assert coordinator._connection_failed
    assert coordinator._disconnect_event.is_set()


def test_lc_only_element_does_not_restore_unsupported_lightness(integration):
    coordinator = _command_coordinator(integration)
    coordinator.brightness[2] = 100
    coordinator.elements = [types.SimpleNamespace(address=2, sig_models={0x130F})]
    asyncio.run(coordinator.async_set_light(2, on=True, use_lc=True))
    coordinator.node.request.assert_awaited_once_with(2, 0x829A, b"\x01\x00", 0x829C)


def test_failed_connected_node_is_replaced_before_next_command(integration):
    coordinator = _command_coordinator(integration)
    coordinator.available = False
    coordinator._connection_failed = True
    coordinator._connect_lock = asyncio.Lock()
    coordinator._cancel_idle_disconnect = Mock()
    replacement = object()

    async def reconnect():
        coordinator.node = replacement

    coordinator._async_connect_proxy.side_effect = reconnect
    result = asyncio.run(
        integration.coordinator.SteinelCoordinator.async_ensure_connected(coordinator)
    )
    assert result is replacement
    assert coordinator.available
    coordinator.transport.disconnect.assert_awaited_once()


def test_initial_sensor_queries_reuse_connected_node_before_available(integration):
    coordinator = _command_coordinator(integration)
    coordinator.available = False
    coordinator._connect_lock = asyncio.Lock()
    coordinator._cancel_idle_disconnect = Mock()
    result = asyncio.run(
        integration.coordinator.SteinelCoordinator.async_ensure_connected(coordinator)
    )
    assert result is coordinator.node
    coordinator._async_connect_proxy.assert_not_awaited()


def _beacon_connection(integration, monkeypatch, *, beacon=True, flags=0):
    coordinator = _coordinator(integration)
    coordinator.entry.options = {}
    coordinator.entry.title = "Test lamp"
    coordinator.entry.data["sequence"] = 78
    coordinator.address = coordinator.entry.data["address"]
    coordinator._async_ble_device = AsyncMock(return_value=object())
    crypto = importlib.import_module(f"{integration.coordinator.__package__}.crypto")
    key = bytes.fromhex(coordinator.entry.data["net_key"])
    decoder = crypto.MeshBeaconDecoder(key)
    transport = types.SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        send=AsyncMock(),
        has_service=lambda _: True,
    )

    async def subscribe():
        # A proxy can deliver its beacon during start_notify itself.
        assert transport.handler is not None
        if beacon:
            body = bytes([flags]) + decoder.network_id + (3).to_bytes(4, "big")
            payload = b"\x01" + body + crypto.aes_cmac(decoder.beacon_key, body)[:8]
            await transport.handler(1, payload)
        else:
            packet = crypto.network_encrypt(
                crypto.NetworkKeys.derive(key), 3, 5, 1, 2, 1, b"test"
            )
            await transport.handler(0, packet)

    transport.subscribe = AsyncMock(side_effect=subscribe)
    monkeypatch.setattr(
        integration.coordinator, "MeshGattTransport", Mock(return_value=transport)
    )
    return coordinator, transport, crypto


@pytest.mark.parametrize("flags", [0, 2])
def test_connection_authenticates_immediate_beacon_before_proxy_filter(
    integration, monkeypatch, flags
):
    coordinator, transport, crypto = _beacon_connection(
        integration, monkeypatch, flags=flags
    )
    asyncio.run(coordinator._async_connect_proxy_attempt())
    assert coordinator.entry.data["iv_index"] == 3
    assert coordinator.entry.data["iv_update"] is bool(flags & 2)
    assert coordinator.entry.data["sequence"] == 79
    expected = crypto.network_encrypt(
        coordinator.node.net_keys,
        2 if flags & 2 else 3,
        0x80,
        78,
        0x7FFE,
        0,
        b"\x00\x01",
        nonce_type=3,
    )
    transport.send.assert_awaited_once_with(2, expected)
    coordinator._async_provision.assert_not_awaited()


def test_connection_fails_clearly_on_iv_mismatch_without_valid_beacon(
    integration, monkeypatch
):
    coordinator, transport, _crypto = _beacon_connection(
        integration, monkeypatch, beacon=False
    )
    node_class = integration.coordinator.MeshNode
    synchronize = node_class.synchronize_iv_index

    async def short_wait(node):
        return await synchronize(node, timeout=0.001)

    monkeypatch.setattr(node_class, "synchronize_iv_index", short_wait)
    with pytest.raises(
        integration.coordinator.SteinelConnectionError, match="IV Index"
    ):
        asyncio.run(coordinator._async_connect_proxy_attempt())
    transport.disconnect.assert_awaited_once()
    transport.send.assert_not_awaited()
    assert coordinator.node is None
    assert coordinator.transport is None
    assert coordinator.entry.data["iv_index"] == 0
    assert coordinator.entry.data["sequence"] == 78


def test_reconnect_monitor_recovers_without_entry_reload(integration, monkeypatch):
    coordinator = _command_coordinator(integration)
    coordinator._connect_lock = asyncio.Lock()
    coordinator.available = False
    coordinator._disconnect_event.set()
    coordinator.reconnect_count = 0

    async def reconnect():
        coordinator._shutting_down = True

    coordinator._async_connect_proxy.side_effect = reconnect
    monkeypatch.setattr(integration.coordinator.asyncio, "sleep", AsyncMock())
    asyncio.run(coordinator._async_reconnect_monitor())
    assert coordinator.available
    assert coordinator.reconnect_count == 1
    assert coordinator.last_error is None
