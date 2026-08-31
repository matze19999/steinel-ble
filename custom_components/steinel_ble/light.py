"""Light entities for Steinel Connect Mesh devices."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SteinelConfigEntry
from .const import (
    CTL_TEMP_MAX_KELVIN,
    CTL_TEMP_MIN_KELVIN,
    MODEL_GENERIC_ONOFF_SERVER,
    MODEL_LIGHT_CTL_SERVER,
    MODEL_LIGHT_HSL_SERVER,
    MODEL_LIGHT_LC_SERVER,
    MODEL_LIGHT_LIGHTNESS_SERVER,
)
from .coordinator import SteinelCoordinator
from .mesh import ElementComposition


async def async_setup_entry(
    hass,
    entry: SteinelConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one entity for each element exposing a light server."""
    coordinator = entry.runtime_data
    entities = [
        SteinelMeshLight(coordinator, element)
        for element in coordinator.elements
        if (
            element.address == coordinator.entry.data["unicast_address"]
            and MODEL_GENERIC_ONOFF_SERVER in element.sig_models
        )
        or element.sig_models
        & {
            MODEL_LIGHT_LIGHTNESS_SERVER,
            MODEL_LIGHT_CTL_SERVER,
            MODEL_LIGHT_HSL_SERVER,
        }
    ]
    async_add_entities(entities)


class SteinelMeshLight(LightEntity):
    """A light element controlled through the Mesh Proxy service."""

    _attr_has_entity_name = True
    _attr_translation_key = "light"

    def __init__(
        self, coordinator: SteinelCoordinator, element: ElementComposition
    ) -> None:
        self.coordinator = coordinator
        self.element = element
        self._attr_unique_id = f"{coordinator.address}_{element.address:04x}_light"
        self._attr_device_info = coordinator.device_info
        if MODEL_LIGHT_HSL_SERVER in element.sig_models:
            self._attr_color_mode = ColorMode.HS
            self._attr_supported_color_modes = {ColorMode.HS}
        elif MODEL_LIGHT_CTL_SERVER in element.sig_models:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_min_color_temp_kelvin = CTL_TEMP_MIN_KELVIN
            self._attr_max_color_temp_kelvin = CTL_TEMP_MAX_KELVIN
        elif MODEL_LIGHT_LIGHTNESS_SERVER in element.sig_models:
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        else:
            self._attr_color_mode = ColorMode.ONOFF
            self._attr_supported_color_modes = {ColorMode.ONOFF}

    @property
    def available(self) -> bool:
        """Return whether the node has a usable proxy connection."""
        return self.coordinator.available

    @property
    def is_on(self) -> bool | None:
        """Return the last acknowledged on/off state."""
        return self.coordinator.is_on.get(self.element.address)

    @property
    def brightness(self) -> int | None:
        """Return the last acknowledged lightness."""
        return self.coordinator.brightness.get(self.element.address)

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the last acknowledged CTL temperature."""
        return self.coordinator.color_temperature.get(self.element.address)

    @property
    def hs_color(self) -> tuple[float, float] | None:
        """Return the last acknowledged HSL hue and saturation."""
        return self.coordinator.hs_color.get(self.element.address)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on this element and optionally set brightness."""
        brightness = kwargs.get(
            ATTR_BRIGHTNESS,
            self.coordinator.brightness.get(self.element.address, 255),
        )
        if ATTR_HS_COLOR in kwargs:
            hue, saturation = kwargs[ATTR_HS_COLOR]
            await self.coordinator.async_set_hsl(
                self.element.address, brightness, hue, saturation
            )
            return
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = max(
                CTL_TEMP_MIN_KELVIN,
                min(CTL_TEMP_MAX_KELVIN, kwargs[ATTR_COLOR_TEMP_KELVIN]),
            )
            await self.coordinator.async_set_ctl(
                self.element.address, brightness, kelvin
            )
            return
        await self.coordinator.async_set_light(
            self.element.address,
            on=True,
            brightness=kwargs.get(ATTR_BRIGHTNESS),
            use_lc=MODEL_LIGHT_LC_SERVER in self.element.sig_models,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off this element."""
        await self.coordinator.async_set_light(
            self.element.address,
            on=False,
            use_lc=MODEL_LIGHT_LC_SERVER in self.element.sig_models,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to acknowledged Mesh state updates."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
