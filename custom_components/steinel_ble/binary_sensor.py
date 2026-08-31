"""Presence entity for the Steinel Sensor Extension model."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SteinelConfigEntry
from .const import MODEL_STEINEL_SENSOR_EXTENSION, STEINEL_COMPANY_ID
from .coordinator import SteinelCoordinator
from .mesh import ElementComposition
from .sensor_protocol import SENSOR_PROPERTIES, decode_sensor_value

SCAN_INTERVAL = timedelta(seconds=30)


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
    )


class SteinelPresenceSensor(BinarySensorEntity):
    """A polled presence state."""

    _attr_has_entity_name = True
    _attr_name = "Presence"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_should_poll = True

    def __init__(
        self, coordinator: SteinelCoordinator, element: ElementComposition
    ) -> None:
        self.coordinator = coordinator
        self.element = element
        self._attr_unique_id = f"{coordinator.address}_{element.address:04x}_presence"
        self._attr_device_info = coordinator.device_info
        self._attr_is_on = None
        self._attr_available = False

    async def async_update(self) -> None:
        """Read the Presence Detected property."""
        try:
            raw = await self.coordinator.async_get_sensor(
                self.element.address, SENSOR_PROPERTIES["presence"]
            )
        except Exception:
            self._attr_available = False
            return
        value = decode_sensor_value("presence", raw)
        self._attr_is_on = value.value if isinstance(value.value, bool) else None
        self._attr_available = self._attr_is_on is not None
        self._attr_extra_state_attributes = {"raw": value.raw.hex(" ").upper()}
