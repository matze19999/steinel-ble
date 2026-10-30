"""Reachability entities must resolve the device owned by their config entry."""

import asyncio
import importlib
import sys
import types
from unittest.mock import Mock

import pytest
from test_import_setup import integration as integration


@pytest.mark.parametrize("registered", [False, True])
def test_tracker_uses_config_entry_scoped_device_lookup(
    integration, monkeypatch, registered
):
    package = sys.modules[integration.coordinator.__package__]
    package.SteinelConfigEntry = types.SimpleNamespace
    tracker = types.ModuleType("homeassistant.components.device_tracker")
    tracker.BaseScannerEntity = object
    tracker.SourceType = types.SimpleNamespace(BLUETOOTH="bluetooth")
    monkeypatch.setitem(sys.modules, tracker.__name__, tracker)
    er = types.ModuleType("homeassistant.helpers.entity_registry")
    er.async_get = Mock(return_value=types.SimpleNamespace(async_get_or_create=Mock()))
    monkeypatch.setitem(sys.modules, er.__name__, er)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "entity_registry", er, raising=False
    )
    platform = types.ModuleType("homeassistant.helpers.entity_platform")
    platform.AddConfigEntryEntitiesCallback = object
    monkeypatch.setitem(sys.modules, platform.__name__, platform)
    registry = types.SimpleNamespace(
        async_get_device_by_identifier=Mock(
            return_value=types.SimpleNamespace(id="device") if registered else None
        )
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.device_registry"],
        "async_get",
        lambda hass: registry,
        raising=False,
    )
    module = importlib.import_module(f"{package.__name__}.device_tracker")
    coordinator = types.SimpleNamespace(address="AA:BB:CC:DD:EE:FF", device_info={})
    entry = types.SimpleNamespace(
        entry_id="my-entry", title="Lamp", runtime_data=coordinator
    )
    entities = []
    asyncio.run(module.async_setup_entry(object(), entry, entities.extend))
    registry.async_get_device_by_identifier.assert_called_once_with(
        ("steinel_ble", coordinator.address), "my-entry"
    )
    assert er.async_get.return_value.async_get_or_create.call_args.kwargs[
        "device_id"
    ] == ("device" if registered else None)
    assert len(entities) == 1
