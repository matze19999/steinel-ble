"""Presence entity for the Steinel Sensor Extension model."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SteinelConfigEntry
from .const import (
    CONF_SENSOR_PROPERTIES,
    MODEL_STEINEL_SENSOR_EXTENSION,
    STEINEL_COMPANY_ID,
)
from .coordinator import SteinelCoordinator
from .mesh import ElementComposition


async def async_setup_entry(
    hass,
    entry: SteinelConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create presence sensors for Sensor Extension elements."""
    coordinator = entry.runtime_data
    async_add_entities(
        SteinelPresenceSensor(coordinator, element)
        for element in coordinator.elements
        if (STEINEL_COMPANY_ID, MODEL_STEINEL_SENSOR_EXTENSION) in element.vendor_models
        and "presence"
        in entry.data.get(CONF_SENSOR_PROPERTIES, {}).get(str(element.address), [])
    )


class SteinelPresenceSensor(BinarySensorEntity):
    """A polled presence state."""

    _attr_has_entity_name = True
    _attr_name = "Presence"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_should_poll = False

    def __init__(
        self, coordinator: SteinelCoordinator, element: ElementComposition
    ) -> None:
        self.coordinator = coordinator
        self.element = element
        self._attr_unique_id = f"{coordinator.address}_{element.address:04x}_presence"
        self._attr_device_info = coordinator.device_info
        self._attr_is_on = None
        self._attr_available = False

    async def async_added_to_hass(self) -> None:
        """Subscribe to the coordinator's shared polling result."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self._handle_coordinator_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        value = self.coordinator.sensor_values.get((self.element.address, "presence"))
        if value is None:
            return
        self._attr_is_on = value.value if isinstance(value.value, bool) else None
        self._attr_available = (
            self._attr_is_on is not None and self.coordinator.reachable
        )
        self._attr_extra_state_attributes = {"raw": value.raw.hex(" ").upper()}
        self.async_write_ha_state()
