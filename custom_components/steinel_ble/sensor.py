"""STEINEL Sensor Extension - environmental sensors.

Only created for the properties actually discovered on a node during setup
(see coordinator.SteinelMeshHub.async_bind_capabilities). Properties with a
published Bluetooth SIG Device Property encoding (protocol.
STANDARD_SENSOR_PROPERTIES) get a typed numeric sensor; anything else is a
STEINEL-vendor property with no published encoding and is only exposed as a
diagnostic raw-hex sensor, disabled by default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CAPABILITY_SENSOR_EXTENSION, DOMAIN, MANUFACTURER
from .coordinator import SteinelMeshHub, SteinelSensorCoordinator
from .protocol import STANDARD_SENSOR_PROPERTIES, humanize_property_name

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Kind:
    translation_key: str
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT


_TYPED_SENSORS: dict[str, _Kind] = {
    "MOTION_SENSED": _Kind("motion_sensed", unit=PERCENTAGE),
    "PEOPLE_COUNT": _Kind("people_count"),
    "TIME_SINCE_MOTION_SENSED": _Kind("time_since_motion_sensed", unit=UnitOfTime.SECONDS),
    "TIME_SINCE_PRESENCE_DETECTED": _Kind("time_since_presence_detected", unit=UnitOfTime.SECONDS),
    "PRESENT_AMBIENT_TEMPERATURE": _Kind(
        "ambient_temperature", device_class=SensorDeviceClass.TEMPERATURE, unit=UnitOfTemperature.CELSIUS
    ),
    "PRECISE_PRESENT_AMBIENT_TEMPERATURE": _Kind(
        "precise_ambient_temperature", device_class=SensorDeviceClass.TEMPERATURE, unit=UnitOfTemperature.CELSIUS
    ),
    "PRESENT_AMBIENT_RELATIVE_HUMIDITY": _Kind(
        "ambient_humidity", device_class=SensorDeviceClass.HUMIDITY, unit=PERCENTAGE
    ),
    "CO2": _Kind("co2", device_class=SensorDeviceClass.CO2, unit=CONCENTRATION_PARTS_PER_MILLION),
    "VOC": _Kind("voc", unit=CONCENTRATION_PARTS_PER_MILLION),
    "NOISE": _Kind("noise", unit="dB"),
    "AIR_PRESSURE": _Kind("air_pressure", device_class=SensorDeviceClass.PRESSURE, unit=UnitOfPressure.PA),
    "DEW_POINT": _Kind("dew_point", device_class=SensorDeviceClass.TEMPERATURE, unit=UnitOfTemperature.CELSIUS),
}


def _device_info(unicast: int, node: dict) -> DeviceInfo:
    address = node["address"]
    return DeviceInfo(
        identifiers={(DOMAIN, address)},
        connections={(CONNECTION_BLUETOOTH, address)},
        name=node.get("name") or f"STEINEL {address}",
        manufacturer=MANUFACTURER,
        model=f"Mesh node 0x{unicast:04X}",
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub: SteinelMeshHub = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    started: set[int] = set()
    for unicast_key, node in hub.network.nodes.items():
        capabilities: dict[str, bool] = node.get("capabilities") or {}
        if not capabilities.get(CAPABILITY_SENSOR_EXTENSION):
            continue
        unicast = int(unicast_key, 16)
        discovered: list[str] = node.get("sensor_properties") or []
        if not discovered:
            continue
        coordinator = hub.sensor_coordinator(unicast, node["address"], node.get("name") or node["address"])
        for name in discovered:
            if name in _TYPED_SENSORS:
                entities.append(SteinelEnvironmentSensor(coordinator, unicast, node, name, _TYPED_SENSORS[name]))
            elif name not in STANDARD_SENSOR_PROPERTIES:
                entities.append(SteinelRawPropertySensor(coordinator, unicast, node, name))
        if unicast not in started:
            started.add(unicast)
            hass.async_create_task(coordinator.async_refresh())
    async_add_entities(entities)


class SteinelEnvironmentSensor(CoordinatorEntity[SteinelSensorCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: SteinelSensorCoordinator, unicast: int, node: dict, name: str, kind: _Kind) -> None:
        super().__init__(coordinator)
        self._property_name = name
        self.entity_description = SensorEntityDescription(
            key=name.lower(),
            translation_key=kind.translation_key,
            device_class=kind.device_class,
            native_unit_of_measurement=kind.unit,
            state_class=kind.state_class,
        )
        self._attr_unique_id = f"{DOMAIN}_{node['address']}_{name.lower()}"
        self._attr_device_info = _device_info(unicast, node)

    @property
    def available(self) -> bool:
        return super().available and bool(self.coordinator.data) and self._property_name in self.coordinator.data

    @property
    def native_value(self) -> Any:
        if not self.coordinator.data:
            return None
        value = self.coordinator.data.get(self._property_name)
        return None if value is None else value.value


class SteinelRawPropertySensor(CoordinatorEntity[SteinelSensorCoordinator], SensorEntity):
    """Diagnostic fallback for STEINEL-vendor sensor properties (0xFFxx)
    with no published value encoding - shows the raw hex payload as-is."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SteinelSensorCoordinator, unicast: int, node: dict, name: str) -> None:
        super().__init__(coordinator)
        self._property_name = name
        self._attr_translation_key = "raw_property"
        self._attr_translation_placeholders = {"property": humanize_property_name(name)}
        self._attr_unique_id = f"{DOMAIN}_{node['address']}_{name.lower()}_raw"
        self._attr_device_info = _device_info(unicast, node)

    @property
    def available(self) -> bool:
        return super().available and bool(self.coordinator.data) and self._property_name in self.coordinator.data

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        value = self.coordinator.data.get(self._property_name)
        return None if value is None else value.raw.hex(" ").upper()
