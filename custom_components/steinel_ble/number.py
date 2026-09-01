"""Light LC configuration controls."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SteinelConfigEntry
from .const import MODEL_LIGHT_LC_SERVER


@dataclass(frozen=True)
class LCNumberDescription:
    key: str
    name: str
    property_id: int
    minimum: float
    maximum: float
    step: float
    unit: str
    kind: str


DESCRIPTIONS = (
    LCNumberDescription(
        "main_light", "Main light", 0x002E, 1, 100, 1, PERCENTAGE, "lightness"
    ),
    LCNumberDescription(
        "basic_light", "Basic light", 0x002F, 0, 100, 1, PERCENTAGE, "lightness"
    ),
    LCNumberDescription(
        "main_light_time",
        "Main light time",
        0x003C,
        1,
        3600,
        1,
        UnitOfTime.SECONDS,
        "time",
    ),
    LCNumberDescription(
        "basic_light_time",
        "Basic light time",
        0x003B,
        0,
        3600,
        1,
        UnitOfTime.SECONDS,
        "time",
    ),
    LCNumberDescription(
        "light_threshold", "Light threshold", 0x002B, 0, 10000, 1, "lx", "lux"
    ),
)


async def async_setup_entry(
    hass, entry: SteinelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    registry = er.async_get(hass)
    for element in coordinator.elements:
        if coordinator.supports_sensor_lighting(element.address):
            continue
        for description in DESCRIPTIONS:
            unique_id = f"{coordinator.address}_{element.address:04x}_{description.key}"
            if entity_id := registry.async_get_entity_id(
                "number", "steinel_ble", unique_id
            ):
                registry.async_remove(entity_id)
    async_add_entities(
        SteinelLCNumber(coordinator, element.address, description)
        for element in coordinator.elements
        if MODEL_LIGHT_LC_SERVER in element.sig_models
        and coordinator.supports_sensor_lighting(element.address)
        for description in DESCRIPTIONS
    )


class SteinelLCNumber(NumberEntity):
    """A configurable Light LC property."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator, address: int, description: LCNumberDescription
    ) -> None:
        self.coordinator = coordinator
        self.address = address
        self.description = description
        self._attr_name = description.name
        self._attr_unique_id = f"{coordinator.address}_{address:04x}_{description.key}"
        self._attr_device_info = coordinator.device_info
        self._attr_native_min_value = description.minimum
        self._attr_native_max_value = description.maximum
        self._attr_native_step = description.step
        self._attr_native_unit_of_measurement = description.unit

    async def async_added_to_hass(self) -> None:
        try:
            raw = await self.coordinator.async_get_lc_property(
                self.address, self.description.property_id
            )
            self._attr_native_value = self._decode(raw)
        except Exception:
            self._attr_available = False

    def _decode(self, raw: bytes) -> float | None:
        if not raw or all(byte == 0xFF for byte in raw):
            return None
        value = int.from_bytes(raw, "little")
        if self.description.kind == "lightness":
            return round(value * 100 / 65535)
        if self.description.kind == "lux":
            return value * 0.01
        return value / 1000

    def _encode(self, value: float) -> bytes:
        if self.description.kind == "lightness":
            return round(value * 65535 / 100).to_bytes(2, "little")
        if self.description.kind == "lux":
            return round(value * 100).to_bytes(3, "little")
        return round(value * 1000).to_bytes(3, "little")

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_lc_property(
            self.address, self.description.property_id, self._encode(value)
        )
        self._attr_native_value = value
