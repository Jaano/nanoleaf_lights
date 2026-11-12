"""Base entity class for Nanoleaf light entities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NanoleafLtpduCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


def device_key(entry: ConfigEntry) -> str:
    """EUI-64 unique ID, or the entry ID for manual entries without one."""
    return entry.unique_id or entry.entry_id


def entity_unique_id(entry: ConfigEntry, suffix: str) -> str:
    """Build an entity unique ID that is unique across all config entries."""
    return f"{device_key(entry)}-{suffix}"


class NanoleafLtpduEntity(CoordinatorEntity[NanoleafLtpduCoordinator]):
    """Base class for all Nanoleaf light entities.

    Provides shared device_info and enforces has_entity_name so that HA
    constructs entity names as "<device name> <entity name>".
    """

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        device = (self.coordinator.data or {}).get("device") or {}
        entry = self.coordinator.config_entry
        eui64: str | None = device.get("eui64")
        connections: set[tuple[str, str]] = set()
        if eui64:
            # Format EUI-64 as colon-separated pairs for display.
            raw = eui64.replace(":", "").lower()
            if len(raw) == 16:
                connections.add(("eui64", ":".join(raw[i : i + 2] for i in range(0, 16, 2))))
        return DeviceInfo(
            identifiers={(DOMAIN, device_key(entry))},
            name=entry.data.get("name", "Nanoleaf light"),
            model=entry.data.get("model"),
            manufacturer="Nanoleaf",
            sw_version=device.get("firmware_version"),
            hw_version=device.get("hardware_version"),
            serial_number=device.get("serial_number"),
            connections=connections,
        )
