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
from homeassistant.helpers.restore_state import RestoreEntity

from . import SteinelConfigEntry
from .const import CONF_PRODUCT_ID, CONF_UNICAST_ADDRESS, MODEL_LIGHT_LC_SETUP_SERVER


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


L810_EXTRA_DESCRIPTIONS = (
    LCNumberDescription(
        "fade_on_time",
        "Main light fade-on time",
        0x0037,
        0,
        3600,
        0.1,
        UnitOfTime.SECONDS,
        "time",
    ),
    LCNumberDescription(
        "occupancy_delay",
        "Motion response delay",
        0x003A,
        0,
        3600,
        0.1,
        UnitOfTime.SECONDS,
        "time",
    ),
)


@dataclass(frozen=True)
class SensorSettingDescription:
    key: str
    name: str
    setting_id: int
    minimum: float
    maximum: float
    step: float
    unit: str | None
    range_index: int | None = None


SENSOR_SETTING_DESCRIPTIONS = (
    SensorSettingDescription(
        "motion_range_1", "Motion range zone 1", 0xFFF5, 0, 100, 1, PERCENTAGE, 0
    ),
    SensorSettingDescription(
        "motion_range_2", "Motion range zone 2", 0xFFF5, 0, 100, 1, PERCENTAGE, 1
    ),
    SensorSettingDescription(
        "motion_sensitivity", "Motion sensitivity (0–4)", 0xFFF6, 0, 4, 1, None
    ),
)


async def async_setup_entry(
    hass, entry: SteinelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    descriptions = (
        DESCRIPTIONS + L810_EXTRA_DESCRIPTIONS
        if str(entry.data.get(CONF_PRODUCT_ID)) == "7801"
        else DESCRIPTIONS
    )
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
        for description in descriptions:
            unique_id = f"{coordinator.address}_{element.address:04x}_{description.key}"
            if entity_id := registry.async_get_entity_id(
                "number", "steinel_ble", unique_id
            ):
                registry.async_remove(entity_id)
    entities = list(
        SteinelLCNumber(coordinator, element.address, description)
        for element in coordinator.elements
        if element.address in supported
        for description in descriptions
    )

    if str(entry.data.get(CONF_PRODUCT_ID)) == "7801":
        sensor_address = entry.data[CONF_UNICAST_ADDRESS] + 2
        entities.extend(
            SteinelSensorSettingNumber(coordinator, sensor_address, description)
            for description in SENSOR_SETTING_DESCRIPTIONS
        )
    async_add_entities(entities)


class SteinelLCNumber(NumberEntity, RestoreEntity):
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
        await super().async_added_to_hass()
        previous = await self.async_get_last_state()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        try:
            raw = await self.coordinator.async_get_lc_property(
                self.address, self.description.property_id
            )
            if (
                str(self.coordinator.entry.data.get(CONF_PRODUCT_ID)) == "7801"
                and self.description.property_id == 0x002B
                and self._decode(raw) == 65535
                and previous is not None
            ):
                try:
                    restored = float(previous.state)
                except ValueError:
                    pass
                else:
                    if self.description.minimum <= restored <= self.description.maximum:
                        raw = await self.coordinator.async_set_lc_property(
                            self.address,
                            self.description.property_id,
                            self._encode(restored),
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


class SteinelSensorSettingNumber(NumberEntity):
    """A configurable L 810 motion-sensor setting."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self, coordinator, address: int, description: SensorSettingDescription
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
            raw = await self.coordinator.async_get_sensor_setting(
                self.address, self.description.setting_id
            )
            if self.description.range_index is None:
                if len(raw) != 1 or raw[0] > 4:
                    raise ValueError("Malformed STEINEL sensitivity setting")
                self._attr_native_value = raw[0]
            else:
                if len(raw) != 4:
                    raise ValueError("Malformed STEINEL range setting")
                offset = 0 if self.description.range_index == 0 else 3
                self._attr_native_value = raw[offset] / 2
        except Exception:
            self._attr_available = False

    async def async_set_native_value(self, value: float) -> None:
        if self.description.range_index is None:
            await self.coordinator.async_set_sensor_setting(
                self.address,
                self.description.setting_id,
                bytes((round(value),)),
            )
        else:
            await self.coordinator.async_set_sensor_range(
                self.address, self.description.range_index, value
            )
        self._attr_native_value = value
