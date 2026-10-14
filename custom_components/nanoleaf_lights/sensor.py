"""Diagnostic sensor entities for Enhanced Nanoleaf Light integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import NanoleafLtpduCoordinator
from .entity import NanoleafLtpduEntity


@dataclass(frozen=True)
class NanoleafSensorDescription(SensorEntityDescription):
    """Extends SensorEntityDescription with a data accessor."""

    value_fn: Callable[[dict[str, Any]], Any] = lambda _: None
    capability: str | None = None
    readback_key: str | None = None
    thread_group: str | None = None


# ---------------------------------------------------------------------------
# Device sensors (populated from coordinator.data["device"])
# ---------------------------------------------------------------------------

_DEVICE_SENSORS: tuple[NanoleafSensorDescription, ...] = ()


def _circadian_clock(data: dict[str, Any]) -> datetime | None:
    epoch = (data.get("clock") or {}).get("epoch_seconds")
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(epoch, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _circadian_timezone(data: dict[str, Any]) -> str | None:
    offset = (data.get("clock") or {}).get("timezone_offset_seconds")
    if offset is None:
        return None
    hours, remainder = divmod(abs(offset), 3600)
    minutes, seconds = divmod(remainder, 60)
    value = f"UTC{'+' if offset >= 0 else '-'}{hours:02d}:{minutes:02d}"
    return f"{value}:{seconds:02d}" if seconds else value


_CIRCADIAN_SENSORS: tuple[NanoleafSensorDescription, ...] = (
    NanoleafSensorDescription(
        key="circadian_clock",
        translation_key="circadian_clock",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        capability="time",
        readback_key="clock",
        value_fn=_circadian_clock,
    ),
    NanoleafSensorDescription(
        key="circadian_timezone_offset",
        translation_key="circadian_timezone_offset",
        icon="mdi:map-clock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        capability="time",
        readback_key="clock",
        value_fn=_circadian_timezone,
    ),
    NanoleafSensorDescription(
        key="circadian_latitude",
        translation_key="circadian_latitude",
        icon="mdi:latitude",
        native_unit_of_measurement="°",
        entity_category=EntityCategory.DIAGNOSTIC,
        capability="location",
        readback_key="location",
        value_fn=lambda data: (data.get("location") or {}).get("latitude"),
    ),
    NanoleafSensorDescription(
        key="circadian_longitude",
        translation_key="circadian_longitude",
        icon="mdi:longitude",
        native_unit_of_measurement="°",
        entity_category=EntityCategory.DIAGNOSTIC,
        capability="location",
        readback_key="location",
        value_fn=lambda data: (data.get("location") or {}).get("longitude"),
    ),
)

# ---------------------------------------------------------------------------
# Thread helper functions (must be defined before _THREAD_SENSORS)
# ---------------------------------------------------------------------------


def _thread_role_string(data: dict[str, Any]) -> str | None:
    thread: dict[str, Any] = data.get("thread") or {}
    if not thread:
        return None
    role_flags = {
        "disabled": thread.get("disabled"),
        "detached": thread.get("detached"),
        "joining": thread.get("joining"),
        "child": thread.get("child"),
        "router": thread.get("router"),
        "leader": thread.get("leader"),
        "border_router": thread.get("border_router"),
    }
    active = [k for k, v in role_flags.items() if v]
    return ", ".join(active) if active else None


def _thread_caps_string(data: dict[str, Any]) -> str | None:
    thread: dict[str, Any] = data.get("thread") or {}
    if not thread:
        return None
    caps_flags = {
        "minimal": thread.get("minimal"),
        "sleepy": thread.get("sleepy"),
        "full": thread.get("full"),
        "router_eligible": thread.get("router_eligible"),
        "border_router_capable": thread.get("border_router_capable"),
    }
    active = [k for k, v in caps_flags.items() if v]
    return ", ".join(active) if active else None


# ---------------------------------------------------------------------------
# Thread sensors (populated from coordinator.data["thread"])
# ---------------------------------------------------------------------------


def _get_thread(data: dict[str, Any]) -> dict[str, Any]:
    val: Any = data.get("thread")
    return val if val else {}


_THREAD_SENSORS: tuple[NanoleafSensorDescription, ...] = (
    NanoleafSensorDescription(
        key="thread_network_name",
        thread_group="network",
        name="Thread Network Name",
        icon="mdi:thread",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _get_thread(data).get("network_name"),
    ),
    NanoleafSensorDescription(
        key="thread_channel",
        thread_group="network",
        name="Thread Channel",
        icon="mdi:radio-tower",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _get_thread(data).get("channel"),
    ),
    NanoleafSensorDescription(
        key="thread_pan_id",
        thread_group="network",
        name="Thread PAN ID",
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _get_thread(data).get("pan_id"),
    ),
    NanoleafSensorDescription(
        key="thread_extended_pan_id",
        thread_group="network",
        name="Thread Extended PAN ID",
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _get_thread(data).get("extended_pan_id"),
    ),
    NanoleafSensorDescription(
        key="thread_mesh_prefix",
        thread_group="network",
        name="Thread Mesh Prefix",
        icon="mdi:network",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _get_thread(data).get("mesh_local_prefix"),
    ),
    NanoleafSensorDescription(
        key="thread_role",
        thread_group="role",
        name="Thread Role",
        icon="mdi:thread",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_thread_role_string,
    ),
    NanoleafSensorDescription(
        key="thread_capabilities",
        thread_group="capabilities",
        name="Thread Capabilities",
        icon="mdi:thread",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_thread_caps_string,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up diagnostic sensors from a config entry."""
    coordinator: NanoleafLtpduCoordinator = entry.runtime_data
    entities: list[SensorEntity] = []

    for description in (*_DEVICE_SENSORS, *_THREAD_SENSORS, *_CIRCADIAN_SENSORS):
        entities.append(NanoleafDiagnosticSensor(coordinator, description))

    entities.append(NanoleafMotionCapabilitiesSensor(coordinator))

    async_add_entities(entities)


class NanoleafDiagnosticSensor(NanoleafLtpduEntity, SensorEntity):  # type: ignore[misc]
    """A single diagnostic sensor for a Nanoleaf light device."""

    entity_description: NanoleafSensorDescription  # type: ignore[override]
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: NanoleafLtpduCoordinator,
        description: NanoleafSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description  # type: ignore[assignment]
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id}-{description.key}"

    @property
    def native_value(self) -> Any:  # type: ignore[override]
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def available(self) -> bool:
        data = self.coordinator.data or {}
        description = self.entity_description
        if description.thread_group is not None:
            return (
                super().available
                and (data.get("thread_readbacks") or {}).get(description.thread_group) is not None
                and self.native_value is not None
            )
        if description.capability is None:
            return super().available
        return (
            super().available
            and bool((data.get("capabilities") or {}).get(description.capability))
            and data.get(description.readback_key) is not None
        )


class NanoleafMotionCapabilitiesSensor(NanoleafLtpduEntity, SensorEntity):
    """Report the motions/options supported by this device."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "motion_capabilities"
    _attr_icon = "mdi:animation-play"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: NanoleafLtpduCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}-motion-capabilities"

    @property
    def native_value(self) -> str | None:
        capabilities = (self.coordinator.data or {}).get("motion_capabilities")
        if capabilities is None:
            return None
        motions = capabilities.get("motions", [])
        if not motions:
            return "No supported animations"
        names = [motion.get("name") or "Unknown animation" for motion in motions]
        description = ", ".join(name[:1].upper() + name[1:] for name in names)
        if len(description) > 255:
            return "Supports many animations; see attributes for the full list"
        return description

    @property
    def available(self) -> bool:
        capabilities = (self.coordinator.data or {}).get("capabilities") or {}
        return (
            super().available and bool(capabilities.get("motions"))
            and self.native_value is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("motion_capabilities") or {}
