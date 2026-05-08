"""Light entity for Enhanced Nanoleaf Light integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    MAX_COLOR_TEMP_KELVIN,
    MIN_COLOR_TEMP_KELVIN,
)
from .coordinator import NanoleafLtpduCoordinator
from .entity import NanoleafLtpduEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the light entity from a config entry."""
    coordinator: NanoleafLtpduCoordinator = entry.runtime_data
    async_add_entities([NanoleafLtpduLight(coordinator)])


class NanoleafLtpduLight(NanoleafLtpduEntity, LightEntity):
    """Represents a Nanoleaf light bulb as an HA light."""

    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_supported_color_modes = {ColorMode.HS, ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
    _attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN
    _attr_translation_key = "light"

    def __init__(self, coordinator: NanoleafLtpduCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id}-light"
        self._attr_name = None  # Use device name directly (has_entity_name + no suffix)
        # Track last-used color mode so HA reports the correct one.
        self._color_mode: ColorMode = ColorMode.COLOR_TEMP

    # ------------------------------------------------------------------
    # State properties
    # ------------------------------------------------------------------

    @property
    def _light(self) -> dict:
        return (self.coordinator.data or {}).get("light") or {}

    @property
    def is_on(self) -> bool | None:
        if "power" not in self._light:
            return None
        return self._light["power"]

    @property
    def brightness(self) -> int | None:
        """HA brightness 0-255; device is 0-100."""
        val = self._light.get("brightness")
        if val is None:
            return None
        return round(val * 255 / 100)

    @property
    def hs_color(self) -> tuple[float, float] | None:
        hue = self._light.get("hue")
        sat = self._light.get("saturation")
        if hue is None or sat is None:
            return None
        return (float(hue), float(sat))

    @property
    def color_temp_kelvin(self) -> int | None:
        return self._light.get("color_temp")

    @property
    def color_mode(self) -> ColorMode:
        return self._color_mode

    @property
    def effect(self) -> str | None:
        current = (self.coordinator.data or {}).get("current_scene", b"")
        if not current:
            return None
        b = current[0]
        return (self.coordinator.data or {}).get("scene_names", {}).get(b, f"Scene 0x{b:02x}")

    @property
    def effect_list(self) -> list[str] | None:
        scene_names: dict = (self.coordinator.data or {}).get("scene_names", {})
        if not scene_names:
            return None
        return list(scene_names.values())

    # ------------------------------------------------------------------
    # Service calls
    # ------------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on, optionally setting brightness, color, or scene."""
        effect: str | None = kwargs.get(ATTR_EFFECT)
        if effect is not None:
            await self._play_effect(effect)
            await self.coordinator.async_request_refresh()
            return

        set_kwargs: dict[str, Any] = {"on": True}

        brightness_ha = kwargs.get(ATTR_BRIGHTNESS)
        if brightness_ha is not None:
            set_kwargs["brightness"] = round(brightness_ha * 100 / 255)

        hs: tuple[float, float] | None = kwargs.get(ATTR_HS_COLOR)
        if hs is not None:
            set_kwargs["hue"] = round(hs[0])
            set_kwargs["saturation"] = round(hs[1])
            self._color_mode = ColorMode.HS

        ct_kelvin: int | None = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        if ct_kelvin is not None:
            ct_kelvin = max(MIN_COLOR_TEMP_KELVIN, min(MAX_COLOR_TEMP_KELVIN, ct_kelvin))
            set_kwargs["color_temp"] = ct_kelvin
            self._color_mode = ColorMode.COLOR_TEMP

        await self.coordinator.async_service_call(
            lambda s: s.set_light_state(**set_kwargs)
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        await self.coordinator.async_service_call(lambda s: s.set_power(False))
        await self.coordinator.async_request_refresh()

    async def _play_effect(self, effect_name: str) -> None:
        """Resolve an effect name to a scene handle and play it."""
        scene_names: dict[int, str] = (self.coordinator.data or {}).get("scene_names", {})
        for b, name in scene_names.items():
            if name == effect_name:
                await self.coordinator.async_service_call(
                    lambda s, handle=b: s.play_scene(bytes([handle]))
                )
                return
        _LOGGER.warning("Effect %r not found in scene list", effect_name)
