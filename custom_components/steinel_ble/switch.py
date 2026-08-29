"""STEINEL Light LC Extension boolean properties (e.g. ECO_MODE_ENABLE).

Only created for nodes where the vendor Light LC Extension model (0x1001)
bound successfully. See STEINEL_BLE_KOMMUNIKATION.md section 4.1 and
protocol.LIGHT_PROPERTY_KINDS for the (best-effort, not hardware-confirmed
for every property) value-type classification.
"""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CAPABILITY_LIGHT_LC_EXTENSION, DOMAIN
from .coordinator import SteinelMeshHub
from .entity_mixins import LightPropertyEntityMixin
from .protocol import (
    LIGHT_PROPERTIES,
    LIGHT_PROPERTIES_DEFAULT_ENABLED,
    LIGHT_PROPERTY_KINDS,
    humanize_property_name,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub: SteinelMeshHub = hass.data[DOMAIN][entry.entry_id]
    entities: list[SteinelLightPropertySwitch] = []
    for unicast_key, node in hub.network.nodes.items():
        capabilities: dict[str, bool] = node.get("capabilities") or {}
        if not capabilities.get(CAPABILITY_LIGHT_LC_EXTENSION):
            continue
        unicast = int(unicast_key, 16)
        for name, property_id in LIGHT_PROPERTIES.items():
            if LIGHT_PROPERTY_KINDS.get(name) != "bool":
                continue
            entities.append(
                SteinelLightPropertySwitch(
                    hub, unicast, node, name, property_id, name in LIGHT_PROPERTIES_DEFAULT_ENABLED
                )
            )
    async_add_entities(entities)


class SteinelLightPropertySwitch(LightPropertyEntityMixin, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, hub: SteinelMeshHub, unicast: int, node: dict, name: str, property_id: int, enabled_default: bool
    ) -> None:
        self._init_light_property(hub, unicast, node, property_id, f"lc_{name.lower()}")
        self._attr_translation_key = "light_property"
        self._attr_translation_placeholders = {"property": humanize_property_name(name)}
        self._attr_entity_registry_enabled_default = enabled_default
        self._is_on: bool | None = None

    def _on_raw_value(self, raw: bytes) -> None:
        self._is_on = bool(raw[0]) if raw else None

    @property
    def is_on(self) -> bool | None:
        return self._is_on

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_write(bytes([1]))
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_write(bytes([0]))
        self._is_on = False
        self.async_write_ha_state()
