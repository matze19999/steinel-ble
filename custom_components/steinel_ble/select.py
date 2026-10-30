"""Operating-mode control for the STEINEL L 810 SC."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SteinelConfigEntry
from .const import (
    CONF_PRODUCT_ID,
    CONF_UNICAST_ADDRESS,
    MODEL_STEINEL_LIGHT_LC_EXTENSION,
    STEINEL_COMPANY_ID,
)

PROFILE_OPTIONS = {
    "automatic": 3,
    "semi_automatic": 4,
    "switch": 5,
    "nightmatic": 6,
}
PROFILE_NAMES = {value: name for name, value in PROFILE_OPTIONS.items()}


async def async_setup_entry(
    hass,
    entry: SteinelConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the L 810 operating-mode selector."""
    if str(entry.data.get(CONF_PRODUCT_ID)) != "7801":
        return
    if not any(
        element.address == entry.data[CONF_UNICAST_ADDRESS] + 1
        and (STEINEL_COMPANY_ID, MODEL_STEINEL_LIGHT_LC_EXTENSION)
        in element.vendor_models
        for element in entry.runtime_data.elements
    ):
        return
    async_add_entities([SteinelL810ProfileSelect(entry.runtime_data)])


class SteinelL810ProfileSelect(SelectEntity):
    """Select one of the L 810 built-in operating modes."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "operating_profile"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_available = False
    _attr_options = list(PROFILE_OPTIONS)

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_operating_profile"
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        try:
            profile = await self.coordinator.async_get_l810_profile()
        except (HomeAssistantError, ValueError, TimeoutError, ConnectionError):
            self._attr_available = False
        else:
            self._apply_profile(profile)

    @property
    def available(self) -> bool:
        return self.coordinator.available and self._attr_available

    def _apply_profile(self, profile: int) -> None:
        self._attr_current_option = PROFILE_NAMES.get(profile)
        self._attr_available = self._attr_current_option is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.l810_profile is not None:
            self._apply_profile(self.coordinator.l810_profile)
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        if option not in PROFILE_OPTIONS:
            raise HomeAssistantError("Unsupported STEINEL operating mode")
        try:
            await self.coordinator.async_set_l810_profile(PROFILE_OPTIONS[option])
        except (ValueError, TimeoutError, ConnectionError) as err:
            raise HomeAssistantError(str(err) or "No operating mode response") from err
        self._apply_profile(PROFILE_OPTIONS[option])
        self.async_write_ha_state()
