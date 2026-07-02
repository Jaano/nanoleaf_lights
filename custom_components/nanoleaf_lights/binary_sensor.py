"""Binary sensor entities for Enhanced Nanoleaf Light integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import NanoleafLtpduCoordinator
from .entity import NanoleafLtpduEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors from a config entry."""
    coordinator: NanoleafLtpduCoordinator = entry.runtime_data
    async_add_entities([NanoleafConnectedSensor(coordinator)])


class NanoleafConnectedSensor(NanoleafLtpduEntity, BinarySensorEntity):  # type: ignore[misc]
    """Reports whether the device is reachable on the network."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Connected"

    def __init__(self, coordinator: NanoleafLtpduCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id}-connected"

    @property
    def available(self) -> bool:
        # This sensor reports unreachability as state False; it must never
        # go "unavailable" itself — that would defeat its purpose.
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.connected
