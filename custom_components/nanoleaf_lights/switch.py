"""Device settings switches for Nanoleaf lights."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .entity import NanoleafLtpduEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import NanoleafLtpduCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Expose the two device settings without changing their stored values."""
    coordinator = entry.runtime_data
    async_add_entities(
        [NanoleafCircadianSwitch(coordinator), NanoleafPowerLossRecoverySwitch(coordinator)]
    )


class NanoleafCircadianSwitch(NanoleafLtpduEntity, SwitchEntity):
    """Enable the bulb's stored circadian white-point schedule."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "circadian"
    _attr_icon = "mdi:sun-clock"

    def __init__(self, coordinator: NanoleafLtpduCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}-circadian"

    @property
    def is_on(self) -> bool | None:
        return ((self.coordinator.data or {}).get("circadian") or {}).get("enabled")

    @property
    def available(self) -> bool:
        capabilities = (self.coordinator.data or {}).get("capabilities") or {}
        return super().available and bool(capabilities.get("circadian")) and self.is_on is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        config = (data.get("circadian") or {}).get("config")
        values = (
            ("configuration", config),
            ("configuration_error", (data.get("circadian") or {}).get("config_error")),
            ("clock", data.get("clock")),
            ("location", data.get("location")),
        )
        return {key: value for key, value in values if value is not None}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_circadian_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_circadian_enabled(False)


class NanoleafPowerLossRecoverySwitch(NanoleafLtpduEntity, SwitchEntity):
    """Select whether the bulb restores its state after losing power."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "power_loss_recovery"
    _attr_icon = "mdi:power-plug"

    def __init__(self, coordinator: NanoleafLtpduCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}-power-loss-recovery"

    @property
    def is_on(self) -> bool | None:
        return (self.coordinator.data or {}).get("power_loss_recovery")

    @property
    def available(self) -> bool:
        capabilities = (self.coordinator.data or {}).get("capabilities") or {}
        return (
            super().available
            and bool(capabilities.get("power_loss_recovery"))
            and self.is_on is not None
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power_loss_recovery(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power_loss_recovery(False)
