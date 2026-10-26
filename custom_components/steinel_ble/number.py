"""Light LC configuration controls."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SteinelConfigEntry
from .const import MODEL_LIGHT_LC_SETUP_SERVER


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
    supported = {
        element.address
        for element in coordinator.elements
        if MODEL_LIGHT_LC_SETUP_SERVER in element.sig_models
        and coordinator.supports_sensor_lighting(element.address)
    }
    for element in coordinator.elements:
        if element.address in supported:
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
        if element.address in supported
        for description in DESCRIPTIONS
    )


class SteinelLCNumber(NumberEntity):
    """A configurable Light LC property."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG
    _attr_available = True

    def __init__(
        self, coordinator, address: int, description: LCNumberDescription
    ) -> None:
        self.coordinator = coordinator
        self.address = address
        self.description = description
        self._attr_translation_key = description.key
        self._attr_unique_id = f"{coordinator.address}_{address:04x}_{description.key}"
        self._attr_device_info = coordinator.device_info
        self._attr_native_min_value = description.minimum
        self._attr_native_max_value = description.maximum
        self._attr_native_step = description.step
        self._attr_native_unit_of_measurement = description.unit

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        try:
            raw = await self.coordinator.async_get_lc_property(
                self.address, self.description.property_id
            )
            self._apply_value(raw)
        except (HomeAssistantError, ValueError):
            self._attr_available = False

    @property
    def available(self) -> bool:
        return self.coordinator.available and self._attr_available

    @callback
    def _handle_coordinator_update(self) -> None:
        raw = self.coordinator.lc_properties.get(
            (self.address, self.description.property_id)
        )
        if raw is not None:
            self._apply_value(raw)
        self.async_write_ha_state()

    def _apply_value(self, raw: bytes) -> bool:
        try:
            self._attr_native_value = self._decode(raw)
        except ValueError:
            self._attr_available = False
            self._attr_native_value = None
        else:
            self._attr_available = True
        return self._attr_available

    def _decode(self, raw: bytes) -> float | None:
        expected = 2 if self.description.kind == "lightness" else 3
        if len(raw) != expected:
            raise ValueError("Invalid Light LC property length")
        value = int.from_bytes(raw, "little")
        if self.description.kind == "lightness":
            return round(value * 100 / 65535)
        if value == 0xFFFFFF:
            return None
        if self.description.kind == "lux":
            return value * 0.01
        return value / 1000

    def _encode(self, value: float) -> bytes:
        if not self.description.minimum <= value <= self.description.maximum:
            raise ValueError("Light LC value outside the supported range")
        if self.description.kind == "lightness":
            return round(value * 65535 / 100).to_bytes(2, "little")
        if self.description.kind == "lux":
            return round(value * 100).to_bytes(3, "little")
        return round(value * 1000).to_bytes(3, "little")

    async def async_set_native_value(self, value: float) -> None:
        raw = await self.coordinator.async_set_lc_property(
            self.address, self.description.property_id, self._encode(value)
        )
        if not self._apply_value(raw):
            raise HomeAssistantError("Invalid Light LC property value response")
        self.async_write_ha_state()
