"""Operating-mode control for the STEINEL L 810 SC."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import SteinelConfigEntry
from .const import CONF_PRODUCT_ID

PROFILE_OPTIONS = {
    "Vollautomatik": 3,
    "Halbautomatik": 4,
    "Schaltermodus": 5,
    "Nightmatic": 6,
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
    async_add_entities([SteinelL810ProfileSelect(entry.runtime_data)])


class SteinelL810ProfileSelect(SelectEntity, RestoreEntity):
    """Select one of the L 810 built-in operating modes."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Betriebsart"
    _attr_options = list(PROFILE_OPTIONS)

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_operating_profile"
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        try:
            profile = await self.coordinator.async_get_l810_profile()
        except Exception:
            profile = 0
        option = PROFILE_NAMES.get(profile)
        if option is None:
            previous = await self.async_get_last_state()
            if previous is not None and previous.state in PROFILE_OPTIONS:
                option = previous.state
        self._attr_current_option = option

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_l810_profile(PROFILE_OPTIONS[option])
        self._attr_current_option = option
