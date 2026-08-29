"""STEINEL Light LC Extension numeric properties (u8/u16/lightness/scene).

Only created for nodes where the vendor Light LC Extension model (0x1001)
bound successfully. See STEINEL_BLE_KOMMUNIKATION.md section 4.1 and
protocol.LIGHT_PROPERTY_KINDS for the (best-effort, not hardware-confirmed
for every property) value-type/byte-width classification.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity
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

# Byte width on the wire for each numeric kind.
_WIDTH = {"u8": 1, "u16": 2, "lightness": 2, "scene": 2}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub: SteinelMeshHub = hass.data[DOMAIN][entry.entry_id]
    entities: list[SteinelLightPropertyNumber] = []
    for unicast_key, node in hub.network.nodes.items():
        capabilities: dict[str, bool] = node.get("capabilities") or {}
        if not capabilities.get(CAPABILITY_LIGHT_LC_EXTENSION):
            continue
        unicast = int(unicast_key, 16)
        for name, property_id in LIGHT_PROPERTIES.items():
            kind = LIGHT_PROPERTY_KINDS.get(name)
            if kind not in _WIDTH:
                continue
            entities.append(
                SteinelLightPropertyNumber(
                    hub, unicast, node, name, property_id, _WIDTH[kind], name in LIGHT_PROPERTIES_DEFAULT_ENABLED
                )
            )
    async_add_entities(entities)


class SteinelLightPropertyNumber(LightPropertyEntityMixin, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_step = 1

    def __init__(
        self,
        hub: SteinelMeshHub,
        unicast: int,
        node: dict,
        name: str,
        property_id: int,
        width: int,
        enabled_default: bool,
    ) -> None:
        self._init_light_property(hub, unicast, node, property_id, f"lc_{name.lower()}")
        self._width = width
        self._attr_translation_key = "light_property"
        self._attr_translation_placeholders = {"property": humanize_property_name(name)}
        self._attr_entity_registry_enabled_default = enabled_default
        self._attr_native_min_value = 0
        self._attr_native_max_value = 255 if width == 1 else 65535
        self._value: float | None = None

    def _on_raw_value(self, raw: bytes) -> None:
        self._value = int.from_bytes(raw[: self._width], "little") if len(raw) >= self._width else None

    @property
    def native_value(self) -> float | None:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        raw = int(value).to_bytes(self._width, "little")
        await self._async_write(raw)
        self._value = int(value)
        self.async_write_ha_state()
