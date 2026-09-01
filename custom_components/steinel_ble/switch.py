"""Light LC automation switches."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SteinelConfigEntry
from .const import (
    MODEL_LIGHT_LC_SERVER,
    OP_LIGHT_LC_MODE_GET,
    OP_LIGHT_LC_MODE_SET,
    OP_LIGHT_LC_MODE_STATUS,
    OP_LIGHT_LC_OCCUPANCY_GET,
    OP_LIGHT_LC_OCCUPANCY_SET,
    OP_LIGHT_LC_OCCUPANCY_STATUS,
)


async def async_setup_entry(
    hass,
    entry: SteinelConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    registry = er.async_get(hass)
    for element in coordinator.elements:
        if coordinator.supports_sensor_lighting(element.address):
            continue
        for key in ("automatic_light_control", "occupancy_automation"):
            unique_id = f"{coordinator.address}_{element.address:04x}_{key}"
            if entity_id := registry.async_get_entity_id(
                "switch", "steinel_ble", unique_id
            ):
                registry.async_remove(entity_id)
    async_add_entities(
        SteinelLCSwitch(coordinator, element.address, occupancy)
        for element in coordinator.elements
        if MODEL_LIGHT_LC_SERVER in element.sig_models
        and coordinator.supports_sensor_lighting(element.address)
        for occupancy in (False, True)
    )


class SteinelLCSwitch(SwitchEntity):
    """Enable the LC controller or its occupancy input."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator, address: int, occupancy: bool) -> None:
        self.coordinator = coordinator
        self.address = address
        self.occupancy = occupancy
        key = "occupancy_automation" if occupancy else "automatic_light_control"
        self._attr_name = (
            "Motion automation" if occupancy else "Automatic light control"
        )
        self._attr_unique_id = f"{coordinator.address}_{address:04x}_{key}"
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        try:
            node = await self.coordinator.async_ensure_connected()
            _src, value = await node.request(
                self.address,
                OP_LIGHT_LC_OCCUPANCY_GET if self.occupancy else OP_LIGHT_LC_MODE_GET,
                b"",
                OP_LIGHT_LC_OCCUPANCY_STATUS
                if self.occupancy
                else OP_LIGHT_LC_MODE_STATUS,
            )
            self._attr_is_on = bool(value[0])
        except Exception:
            self._attr_available = False

    async def _async_set(self, state: bool) -> None:
        node = await self.coordinator.async_ensure_connected()
        await node.request(
            self.address,
            OP_LIGHT_LC_OCCUPANCY_SET if self.occupancy else OP_LIGHT_LC_MODE_SET,
            bytes((state,)),
            OP_LIGHT_LC_OCCUPANCY_STATUS if self.occupancy else OP_LIGHT_LC_MODE_STATUS,
        )
        self._attr_is_on = state

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)
