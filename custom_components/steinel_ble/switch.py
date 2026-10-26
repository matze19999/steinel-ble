"""Light LC automation switches."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SteinelConfigEntry


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
        if coordinator.supports_sensor_lighting(element.address)
        for occupancy in (False, True)
    )


class SteinelLCSwitch(SwitchEntity):
    """Enable the LC controller or its occupancy input."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_available = True

    def __init__(self, coordinator, address: int, occupancy: bool) -> None:
        self.coordinator = coordinator
        self.address = address
        self.occupancy = occupancy
        key = "occupancy_automation" if occupancy else "automatic_light_control"
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.address}_{address:04x}_{key}"
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        try:
            self._attr_is_on = await self.coordinator.async_get_lc_mode(
                self.address, self.occupancy
            )
            self._attr_available = True
        except HomeAssistantError:
            self._attr_available = False

    @property
    def available(self) -> bool:
        return self.coordinator.available and self._attr_available

    @callback
    def _handle_coordinator_update(self) -> None:
        value = self.coordinator.lc_modes.get((self.address, self.occupancy))
        if value is not None:
            self._attr_is_on = value
            self._attr_available = True
        self.async_write_ha_state()

    async def _async_set(self, state: bool) -> None:
        self._attr_is_on = await self.coordinator.async_set_lc_mode(
            self.address, self.occupancy, state
        )
        self._attr_available = True
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)
