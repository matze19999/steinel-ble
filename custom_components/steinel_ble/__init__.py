"""The STEINEL Connect BLE / Bluetooth Mesh integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import SteinelMeshHub

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.BUTTON,
    Platform.UPDATE,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.TEXT,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = SteinelMeshHub(hass, entry)
    await hub.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hub: SteinelMeshHub = hass.data[DOMAIN].pop(entry.entry_id)
        await hub.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the persisted mesh network state (keys, sequence, node list)
    when the integration instance itself is removed.

    This does not factory-reset any lamp: a removed lamp keeps the NetKey it
    was provisioned with and will need a physical reset before it can join a
    different mesh network again.
    """
    from homeassistant.helpers.storage import Store

    from .mesh_store import STORAGE_KEY, STORAGE_VERSION

    await Store(hass, STORAGE_VERSION, STORAGE_KEY).async_remove()


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
