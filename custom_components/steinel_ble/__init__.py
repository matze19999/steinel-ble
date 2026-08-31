"""Steinel Connect Bluetooth Mesh integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ADDRESS, PLATFORMS
from .coordinator import SteinelCoordinator

type SteinelConfigEntry = ConfigEntry[SteinelCoordinator]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: SteinelConfigEntry) -> bool:
    """Set up a Steinel Mesh config entry."""
    if CONF_ADDRESS not in entry.data:
        _LOGGER.error(
            "Config entry %s is incomplete and must be removed and added again",
            entry.title,
        )
        return False
    coordinator = SteinelCoordinator(hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: SteinelConfigEntry
) -> None:
    """Reload the entry after its options change."""
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data and runtime_data.loaded_options == dict(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SteinelConfigEntry) -> bool:
    """Unload a Steinel Mesh config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.async_shutdown()
    return True
