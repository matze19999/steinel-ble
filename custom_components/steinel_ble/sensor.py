"""Environmental sensors exposed by the Steinel Sensor Extension model."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfPressure,
    UnitOfRatio,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SteinelConfigEntry
from .const import MODEL_STEINEL_SENSOR_EXTENSION, STEINEL_COMPANY_ID
from .coordinator import SteinelCoordinator
from .mesh import ElementComposition
from .sensor_protocol import SENSOR_PROPERTIES, decode_sensor_value

SCAN_INTERVAL = timedelta(seconds=90)

_META: dict[str, tuple[str | None, str | None]] = {
    "motion": (None, PERCENTAGE),
    "people_count": (None, None),
    "temperature": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    "precise_temperature": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    "dew_point": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    "humidity": (SensorDeviceClass.HUMIDITY, PERCENTAGE),
    "co2": (SensorDeviceClass.CO2, UnitOfRatio.PARTS_PER_MILLION),
    "voc": (None, UnitOfRatio.PARTS_PER_MILLION),
    "noise": (None, "dB"),
    "air_pressure": (SensorDeviceClass.PRESSURE, UnitOfPressure.PA),
    "time_since_motion": (SensorDeviceClass.DURATION, UnitOfTime.SECONDS),
    "time_since_presence": (SensorDeviceClass.DURATION, UnitOfTime.SECONDS),
}


async def async_setup_entry(
    hass,
    entry: SteinelConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create typed sensors only for elements with Sensor Extension."""
    coordinator = entry.runtime_data
    async_add_entities(
        SteinelPropertySensor(coordinator, element, name, property_id)
        for element in coordinator.elements
        if (STEINEL_COMPANY_ID, MODEL_STEINEL_SENSOR_EXTENSION) in element.vendor_models
        for name, property_id in SENSOR_PROPERTIES.items()
        if name != "presence"
    )


class SteinelPropertySensor(SensorEntity):
    """A polled Bluetooth SIG property carried by a Steinel vendor model."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: SteinelCoordinator,
        element: ElementComposition,
        name: str,
        property_id: int,
    ) -> None:
        self.coordinator = coordinator
        self.element = element
        self.name_key = name
        self.property_id = property_id
        self._attr_name = name.replace("_", " ").title()
        self._attr_unique_id = f"{coordinator.address}_{element.address:04x}_{name}"
        self._attr_device_info = coordinator.device_info
        device_class, unit = _META.get(name, (None, None))
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_native_value: Any = None
        self._attr_available = False

    async def async_update(self) -> None:
        """Read and decode the property."""
        try:
            raw = await self.coordinator.async_get_sensor(
                self.element.address, self.property_id
            )
        except Exception:
            self._attr_available = False
            return
        value = decode_sensor_value(self.name_key, raw)
        self._attr_native_value = value.value
        self._attr_available = value.value is not None
        self._attr_extra_state_attributes = {"raw": value.raw.hex(" ").upper()}
