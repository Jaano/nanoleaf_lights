"""Base entity class for Nanoleaf light entities."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NanoleafLtpduCoordinator


class NanoleafLtpduEntity(CoordinatorEntity[NanoleafLtpduCoordinator]):
    """Base class for all Nanoleaf light entities.

    Provides shared device_info and enforces has_entity_name so that HA
    constructs entity names as "<device name> <entity name>".
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: NanoleafLtpduCoordinator) -> None:
        super().__init__(coordinator)

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
                connections.add(("eui64", ":".join(raw[i:i+2] for i in range(0, 16, 2))))
        return DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name=entry.data.get("name", "Nanoleaf light"),
            model=entry.data.get("model"),
            manufacturer="Nanoleaf",
            sw_version=device.get("firmware_version"),
            hw_version=device.get("hardware_version"),
            serial_number=device.get("serial_number"),
            connections=connections,
        )
