"""STEINEL Light LC Extension properties with an unknown wire shape (group
addresses, address lists) - exposed as raw hex, matching how
steinel_ble.py's mesh-light-set CLI command already handles these ("Werte
werden absichtlich als Hexbytes übergeben, weil die APK je nach Property
unterschiedliche Datentypen und Breiten serialisiert").

Only created for nodes where the vendor Light LC Extension model (0x1001)
bound successfully.
"""

from __future__ import annotations

import logging

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CAPABILITY_LIGHT_LC_EXTENSION, DOMAIN
from .coordinator import SteinelMeshHub
from .entity_mixins import LightPropertyEntityMixin
from .protocol import LIGHT_PROPERTIES, LIGHT_PROPERTY_KINDS, humanize_property_name

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub: SteinelMeshHub = hass.data[DOMAIN][entry.entry_id]
    entities: list[SteinelLightPropertyText] = []
    for unicast_key, node in hub.network.nodes.items():
        capabilities: dict[str, bool] = node.get("capabilities") or {}
        if not capabilities.get(CAPABILITY_LIGHT_LC_EXTENSION):
            continue
        unicast = int(unicast_key, 16)
        for name, property_id in LIGHT_PROPERTIES.items():
            if LIGHT_PROPERTY_KINDS.get(name) != "raw":
                continue
            entities.append(SteinelLightPropertyText(hub, unicast, node, name, property_id))
    async_add_entities(entities)


class SteinelLightPropertyText(LightPropertyEntityMixin, TextEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_pattern = r"^([0-9A-Fa-f]{2} ?)*$"

    def __init__(self, hub: SteinelMeshHub, unicast: int, node: dict, name: str, property_id: int) -> None:
        self._init_light_property(hub, unicast, node, property_id, f"lc_{name.lower()}")
        self._attr_translation_key = "light_property_raw"
        self._attr_translation_placeholders = {"property": humanize_property_name(name)}
        self._value: str | None = None

    def _on_raw_value(self, raw: bytes) -> None:
        self._value = raw.hex(" ").upper()

    @property
    def native_value(self) -> str | None:
        return self._value

    async def async_set_value(self, value: str) -> None:
        try:
            raw = bytes.fromhex(value.replace(" ", ""))
        except ValueError as exc:
            raise HomeAssistantError(f"not a valid hex string: {value}") from exc
        await self._async_write(raw)
        self._value = raw.hex(" ").upper()
        self.async_write_ha_state()
