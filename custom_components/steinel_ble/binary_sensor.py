"""STEINEL Sensor Extension - presence/occupancy binary sensor.

Only created for nodes where the vendor Sensor Extension model (0x1003)
bound successfully - i.e. lamps/sensors that actually offer it, such as
STEINEL products with a built-in motion detector. See
STEINEL_BLE_KOMMUNIKATION.md section 4.2 and protocol.parse_sensor_value.
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CAPABILITY_SENSOR_EXTENSION, DOMAIN, MANUFACTURER
from .coordinator import SteinelMeshHub, SteinelSensorCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub: SteinelMeshHub = hass.data[DOMAIN][entry.entry_id]
    entities: list[SteinelPresenceBinarySensor] = []
    started: set[int] = set()
    for unicast_key, node in hub.network.nodes.items():
        capabilities: dict[str, bool] = node.get("capabilities") or {}
        if not capabilities.get(CAPABILITY_SENSOR_EXTENSION):
            continue
        unicast = int(unicast_key, 16)
        coordinator = hub.sensor_coordinator(unicast, node["address"], node.get("name") or node["address"])
        entities.append(SteinelPresenceBinarySensor(coordinator, unicast, node))
        if unicast not in started:
            started.add(unicast)
            hass.async_create_task(coordinator.async_refresh())
    async_add_entities(entities)


class SteinelPresenceBinarySensor(CoordinatorEntity[SteinelSensorCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "presence"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, coordinator: SteinelSensorCoordinator, unicast: int, node: dict) -> None:
        super().__init__(coordinator)
        address = node["address"]
        self._attr_unique_id = f"{DOMAIN}_{address}_presence"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            name=node.get("name") or f"STEINEL {address}",
            manufacturer=MANUFACTURER,
            model=f"Mesh node 0x{unicast:04X}",
        )

    @property
    def available(self) -> bool:
        return super().available and bool(self.coordinator.data) and "PRESENCE_DETECTED" in self.coordinator.data

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        value = self.coordinator.data.get("PRESENCE_DETECTED")
        return None if value is None else value.value
