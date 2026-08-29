"""STEINEL Mesh light entity: on/off, brightness, and (if supported by the
lamp) colour temperature or HS colour, auto-detected during setup by which
Mesh models successfully bound (see coordinator.SteinelMeshHub.async_bind_capabilities)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CAPABILITY_CTL,
    CAPABILITY_HSL,
    CAPABILITY_LIGHTNESS,
    CAPABILITY_ONOFF,
    CTL_TEMP_MAX_KELVIN,
    CTL_TEMP_MIN_KELVIN,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import SteinelLightCoordinator, SteinelMeshHub
from .protocol import ProtocolError

_LOGGER = logging.getLogger(__name__)


def _to_lightness(brightness_255: int) -> int:
    if brightness_255 >= 255:
        return 65535
    return max(0, min(65535, round(brightness_255 * 65535 / 255)))


def _to_brightness(lightness: int) -> int:
    if lightness >= 65535:
        return 255
    return max(0, min(255, round(lightness * 255 / 65535)))


def _to_hs_raw(hue: float, saturation: float) -> tuple[int, int]:
    return (
        max(0, min(65535, round(hue / 360 * 65535))),
        max(0, min(65535, round(saturation / 100 * 65535))),
    )


def _from_hs_raw(hue_raw: int, saturation_raw: int) -> tuple[float, float]:
    return hue_raw / 65535 * 360, saturation_raw / 65535 * 100


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub: SteinelMeshHub = hass.data[DOMAIN][entry.entry_id]
    entities: list[SteinelLight] = []

    for unicast_key, node in hub.network.nodes.items():
        capabilities: dict[str, bool] = node.get("capabilities") or {}
        if not capabilities.get(CAPABILITY_ONOFF):
            continue
        unicast = int(unicast_key, 16)
        coordinator = SteinelLightCoordinator(
            hass, hub, unicast, node["address"], node.get("name") or node["address"], capabilities
        )
        entities.append(SteinelLight(coordinator, node, capabilities))
        hass.async_create_task(coordinator.async_refresh())

    async_add_entities(entities)


class SteinelLight(CoordinatorEntity[SteinelLightCoordinator], LightEntity):
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: SteinelLightCoordinator, node: dict[str, Any], capabilities: dict[str, bool]) -> None:
        super().__init__(coordinator)
        self._unicast = coordinator.unicast
        self._address = coordinator.address
        self._attr_unique_id = f"{DOMAIN}_{self._address}_light"
        self._capable_lightness = bool(capabilities.get(CAPABILITY_LIGHTNESS))
        self._capable_ctl = bool(capabilities.get(CAPABILITY_CTL))
        self._capable_hsl = bool(capabilities.get(CAPABILITY_HSL))

        modes: set[ColorMode] = set()
        if self._capable_hsl:
            modes.add(ColorMode.HS)
        if self._capable_ctl:
            modes.add(ColorMode.COLOR_TEMP)
        if not modes and self._capable_lightness:
            modes.add(ColorMode.BRIGHTNESS)
        if not modes:
            modes.add(ColorMode.ONOFF)
        self._attr_supported_color_modes = modes
        self._current_color_mode = next(iter(modes))
        if self._capable_ctl:
            self._attr_min_color_temp_kelvin = CTL_TEMP_MIN_KELVIN
            self._attr_max_color_temp_kelvin = CTL_TEMP_MAX_KELVIN

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            connections={(CONNECTION_BLUETOOTH, self._address)},
            name=node.get("name") or f"STEINEL {self._address}",
            manufacturer=MANUFACTURER,
            model=f"Mesh node 0x{self._unicast:04X}",
        )

    @property
    def color_mode(self) -> ColorMode:
        return self._current_color_mode

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.onoff if self.coordinator.data else None

    @property
    def brightness(self) -> int | None:
        data = self.coordinator.data
        if data is None:
            return None
        raw = data.lightness if data.lightness is not None else data.ctl_lightness if data.ctl_lightness is not None else data.hsl_lightness
        return None if raw is None else _to_brightness(raw)

    @property
    def color_temp_kelvin(self) -> int | None:
        data = self.coordinator.data
        return None if data is None or data.ctl_temperature is None else data.ctl_temperature

    @property
    def hs_color(self) -> tuple[float, float] | None:
        data = self.coordinator.data
        if data is None or data.hue is None or data.saturation is None:
            return None
        return _from_hs_raw(data.hue, data.saturation)

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            if ATTR_HS_COLOR in kwargs and self._capable_hsl:
                hue_raw, sat_raw = _to_hs_raw(*kwargs[ATTR_HS_COLOR])
                lightness = _to_lightness(kwargs.get(ATTR_BRIGHTNESS, self.brightness or 255))
                state = await self.coordinator.hub.async_set_hsl(self._unicast, self._address, lightness, hue_raw, sat_raw)
                self._current_color_mode = ColorMode.HS
            elif ATTR_COLOR_TEMP_KELVIN in kwargs and self._capable_ctl:
                kelvin = max(CTL_TEMP_MIN_KELVIN, min(CTL_TEMP_MAX_KELVIN, kwargs[ATTR_COLOR_TEMP_KELVIN]))
                lightness = _to_lightness(kwargs.get(ATTR_BRIGHTNESS, self.brightness or 255))
                state = await self.coordinator.hub.async_set_ctl(self._unicast, self._address, lightness, kelvin)
                self._current_color_mode = ColorMode.COLOR_TEMP
            elif ATTR_BRIGHTNESS in kwargs and self._capable_lightness:
                state = await self.coordinator.hub.async_set_lightness(
                    self._unicast, self._address, _to_lightness(kwargs[ATTR_BRIGHTNESS])
                )
            else:
                state = await self.coordinator.hub.async_set_onoff(self._unicast, self._address, True)
        except ProtocolError as exc:
            raise HomeAssistantError(f"could not switch on {self.entity_id}: {exc}") from exc
        await self.coordinator.async_apply(state)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            state = await self.coordinator.hub.async_set_onoff(self._unicast, self._address, False)
        except ProtocolError as exc:
            raise HomeAssistantError(f"could not switch off {self.entity_id}: {exc}") from exc
        await self.coordinator.async_apply(state)
