"""Shared behaviour for STEINEL Light LC Extension property entities
(switch.py / number.py / text.py): read once when the entity is added to
Home Assistant, then update optimistically after a user-initiated write.

There is deliberately no periodic coordinator/poll here: a lamp can expose
up to ~50 of these (see protocol.LIGHT_PROPERTIES), they are installer-time
configuration rather than fast-changing state, and polling all of them on
every lamp on a timer would spend most of a Bluetooth proxy's time budget
on values nobody is watching change.
"""

from __future__ import annotations

import logging

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo

from .const import DOMAIN, MANUFACTURER
from .coordinator import SteinelMeshHub
from .protocol import ProtocolError

_LOGGER = logging.getLogger(__name__)


class LightPropertyEntityMixin:
    """Mix into a SwitchEntity/NumberEntity/TextEntity subclass."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def _init_light_property(
        self, hub: SteinelMeshHub, unicast: int, node: dict, property_id: int, unique_suffix: str
    ) -> None:
        self._hub = hub
        self._unicast = unicast
        self._address = node["address"]
        self._property_id = property_id
        self._attr_unique_id = f"{DOMAIN}_{self._address}_{unique_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            connections={(CONNECTION_BLUETOOTH, self._address)},
            name=node.get("name") or f"STEINEL {self._address}",
            manufacturer=MANUFACTURER,
            model=f"Mesh node 0x{unicast:04X}",
        )
        self._have_value = False

    @property
    def available(self) -> bool:
        return self._have_value

    async def async_added_to_hass(self) -> None:
        try:
            raw = await self._hub.async_get_light_property(self._unicast, self._address, self._property_id)
        except ProtocolError:
            _LOGGER.debug(
                "Could not read property 0x%02X on %s (may not be supported on this product)",
                self._property_id,
                self._address,
            )
            return
        self._on_raw_value(raw)
        self._have_value = True

    async def _async_write(self, value: bytes) -> None:
        try:
            await self._hub.async_set_light_property(self._unicast, self._address, self._property_id, value)
        except ProtocolError as exc:
            raise HomeAssistantError(f"could not set property on {self._address}: {exc}") from exc
        self._have_value = True

    def _on_raw_value(self, raw: bytes) -> None:
        raise NotImplementedError
