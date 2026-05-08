"""Diagnostic sensor entities for Enhanced Nanoleaf Light integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
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


# ---------------------------------------------------------------------------
# Device sensors (populated from coordinator.data["device"])
# ---------------------------------------------------------------------------

_DEVICE_SENSORS: tuple[NanoleafSensorDescription, ...] = ()

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
        name="Thread Network Name",
        icon="mdi:thread",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _get_thread(data).get("network_name"),
    ),
    NanoleafSensorDescription(
        key="thread_channel",
        name="Thread Channel",
        icon="mdi:radio-tower",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _get_thread(data).get("channel"),
    ),
    NanoleafSensorDescription(
        key="thread_pan_id",
        name="Thread PAN ID",
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _get_thread(data).get("pan_id"),
    ),
    NanoleafSensorDescription(
        key="thread_extended_pan_id",
        name="Thread Extended PAN ID",
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _get_thread(data).get("extended_pan_id"),
    ),
    NanoleafSensorDescription(
        key="thread_mesh_prefix",
        name="Thread Mesh Prefix",
        icon="mdi:network",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _get_thread(data).get("mesh_local_prefix"),
    ),
    NanoleafSensorDescription(
        key="thread_role",
        name="Thread Role",
        icon="mdi:thread",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_thread_role_string,
    ),
    NanoleafSensorDescription(
        key="thread_capabilities",
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
    entities: list[NanoleafDiagnosticSensor] = []

    for description in (*_DEVICE_SENSORS, *_THREAD_SENSORS):
        entities.append(NanoleafDiagnosticSensor(coordinator, description))

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
