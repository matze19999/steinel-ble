"""Buttons: Identify (blink) and Factory reset for a provisioned lamp.

Identify uses the proprietary GATT channel opcode 0x17 so the physical lamp
behind a device entry can be found; it is also used automatically during
config-flow device confirmation.

Factory reset sends the proprietary Global Reset opcode 0xE5 (see
STEINEL_BLE_KOMMUNIKATION.md section 6.6/7 and steinel_ble.py's
GlobalResetCommand) and then removes the lamp from Home Assistant's mesh
store and device/entity registries - the lamp itself erases its Bluetooth
Mesh keys and needs to be re-provisioned before it can be used again. It is
disabled by default (has to be enabled explicitly on the entity) because
there is no per-press confirmation dialog for a plain button entity in Home
Assistant.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER
from .coordinator import SteinelMeshHub
from .protocol import ProtocolError

_LOGGER = logging.getLogger(__name__)

IDENTIFY_DESCRIPTION = ButtonEntityDescription(
    key="identify", translation_key="identify", entity_category=EntityCategory.CONFIG
)
FACTORY_RESET_DESCRIPTION = ButtonEntityDescription(
    key="factory_reset",
    translation_key="factory_reset",
    entity_category=EntityCategory.CONFIG,
    icon="mdi:restart-alert",
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub: SteinelMeshHub = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = []
    for unicast_key, node in hub.network.nodes.items():
        unicast = int(unicast_key, 16)
        entities.append(SteinelIdentifyButton(hub, unicast, node))
        entities.append(SteinelFactoryResetButton(hass, hub, unicast, node))
    async_add_entities(entities)


def _device_info(unicast: int, node: dict) -> DeviceInfo:
    address = node["address"]
    return DeviceInfo(
        identifiers={(DOMAIN, address)},
        connections={(CONNECTION_BLUETOOTH, address)},
        name=node.get("name") or f"STEINEL {address}",
        manufacturer=MANUFACTURER,
        model=f"Mesh node 0x{unicast:04X}",
    )


class SteinelIdentifyButton(ButtonEntity):
    _attr_has_entity_name = True
    entity_description = IDENTIFY_DESCRIPTION

    def __init__(self, hub: SteinelMeshHub, unicast: int, node: dict) -> None:
        self._hub = hub
        self._address = node["address"]
        self._attr_unique_id = f"{DOMAIN}_{self._address}_identify"
        self._attr_device_info = _device_info(unicast, node)

    async def async_press(self) -> None:
        try:
            await self._hub.async_identify(self._address, True, duration=10)
        except ProtocolError as exc:
            raise HomeAssistantError(f"could not identify {self._address}: {exc}") from exc


class SteinelFactoryResetButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    entity_description = FACTORY_RESET_DESCRIPTION

    def __init__(self, hass: HomeAssistant, hub: SteinelMeshHub, unicast: int, node: dict) -> None:
        self.hass = hass
        self._hub = hub
        self._unicast = unicast
        self._address = node["address"]
        self._attr_unique_id = f"{DOMAIN}_{self._address}_factory_reset"
        self._attr_device_info = _device_info(unicast, node)

    async def async_press(self) -> None:
        try:
            await self._hub.async_factory_reset(self._unicast)
        except ProtocolError as exc:
            raise HomeAssistantError(f"factory reset of {self._address} failed: {exc}") from exc
        _LOGGER.info("STEINEL lamp %s (0x%04X) was factory-reset and removed from the mesh", self._address, self._unicast)
        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device(identifiers={(DOMAIN, self._address)})
        if device is not None:
            device_registry.async_remove_device(device.id)
