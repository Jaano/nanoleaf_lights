"""Button entities for Enhanced Nanoleaf Light integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import NanoleafLtpduCoordinator
from .entity import NanoleafLtpduEntity
from .nl_api import NanoleafCloudApi


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button entities from a config entry."""
    coordinator: NanoleafLtpduCoordinator = entry.runtime_data
    async_add_entities([
        NanoleafIdentifyButton(coordinator),
        NanoleafRefreshScenesButton(coordinator),
        NanoleafSyncClockLocationButton(coordinator),
    ])


class NanoleafIdentifyButton(NanoleafLtpduEntity, ButtonEntity):  # type: ignore[misc]
    """Button that triggers the device's physical identification blink."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Identify"
    _attr_icon = "mdi:eye"

    def __init__(self, coordinator: NanoleafLtpduCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id}-identify"

    async def async_press(self) -> None:
        """Trigger the device to blink for identification."""
        await self.coordinator.async_service_call(lambda s: s.identify())


class NanoleafRefreshScenesButton(NanoleafLtpduEntity, ButtonEntity):  # type: ignore[misc]
    """Button that downloads the latest scene database from the Nanoleaf cloud."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Refresh Scene Database"
    _attr_icon = "mdi:cloud-download"

    def __init__(self, coordinator: NanoleafLtpduCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id}-refresh-scenes"

    async def async_press(self) -> None:
        """Download updated scene data from the Nanoleaf cloud and reload names."""
        scenes_path = self.coordinator.scenes_path
        api = NanoleafCloudApi()
        await self.hass.async_add_executor_job(api.build_scenes, scenes_path)
        await self.coordinator.async_reload_scenes()


class NanoleafSyncClockLocationButton(NanoleafLtpduEntity, ButtonEntity):
    """Copy the configured HA clock, timezone and location to the bulb."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "sync_clock_location"
    _attr_icon = "mdi:map-clock"

    def __init__(self, coordinator: NanoleafLtpduCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}-sync-clock-location"

    @property
    def available(self) -> bool:
        data = self.coordinator.data or {}
        capabilities = data.get("capabilities") or {}
        return (
            super().available
            and bool(capabilities.get("circadian"))
            and (data.get("circadian") or {}).get("enabled") is not None
            and bool(capabilities.get("time"))
            and bool(capabilities.get("location"))
        )

    async def async_press(self) -> None:
        await self.coordinator.async_sync_clock_and_location()
