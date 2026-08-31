"""Reachability tracker for a Steinel Bluetooth Mesh device."""

from __future__ import annotations

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SteinelConfigEntry
from .coordinator import SteinelCoordinator


async def async_setup_entry(
    hass,
    entry: SteinelConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one reachability tracker for the configured node."""
    async_add_entities([SteinelReachabilityTracker(entry.runtime_data)])


class SteinelReachabilityTracker(ScannerEntity):
    """Represent whether the node has a usable Mesh Proxy connection."""

    _attr_has_entity_name = True
    _attr_translation_key = "reachability"
    _attr_source_type = SourceType.BLUETOOTH

    def __init__(self, coordinator: SteinelCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_reachability"
        self._attr_device_info = coordinator.device_info

    @property
    def is_connected(self) -> bool:
        """Return whether the node currently has a usable proxy connection."""
        return self.coordinator.available

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator availability changes."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
