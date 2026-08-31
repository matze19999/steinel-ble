"""Reachability tracker for a Steinel Bluetooth Mesh device."""

from __future__ import annotations

from homeassistant.components.device_tracker import BaseScannerEntity, SourceType
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SteinelConfigEntry
from .const import DOMAIN
from .coordinator import SteinelCoordinator


async def async_setup_entry(
    hass,
    entry: SteinelConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one reachability tracker for the configured node."""
    coordinator = entry.runtime_data
    unique_id = f"{coordinator.address}_reachability"
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, coordinator.address)}
    )
    er.async_get(hass).async_get_or_create(
        "device_tracker",
        DOMAIN,
        unique_id,
        config_entry=entry,
        device_id=device.id if device else None,
        original_name="Reachability",
        suggested_object_id=f"{entry.title}_reachability",
    )
    async_add_entities([SteinelReachabilityTracker(coordinator)])


class SteinelReachabilityTracker(BaseScannerEntity):
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
