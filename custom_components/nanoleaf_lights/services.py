"""Targeted Home Assistant actions for local Nanoleaf settings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:
    import probatio as vol
except ImportError:
    import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_extract_config_entry_ids

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall

_TARGET_FIELDS = {
    vol.Optional("config_entry_id"): cv.string,
    vol.Optional("entity_id"): cv.entity_ids_or_uuids,
    **{
        vol.Optional(key): vol.Any(cv.string, [cv.string])
        for key in ("device_id", "area_id", "floor_id", "label_id")
    },
}
_CIRCADIAN_FIELDS = {
    vol.Required("mode"): vol.In(("explicit", "sunrise_sunset")),
    **{
        vol.Required(key): vol.All(vol.Coerce(int), vol.Range(min=1200, max=6535))
        for key in ("daylight_target_kelvin", "evening_target_kelvin", "morning_target_kelvin")
    },
    **{
        vol.Required(key): vol.All(vol.Coerce(int), vol.Range(min=0, max=65535))
        for key in ("rise_ramp_minutes", "fall_ramp_minutes", "morning_ramp_minutes")
    },
    **{
        vol.Optional(key): vol.All(vol.Coerce(int), vol.Range(min=0, max=1439))
        for key in ("rise_time_minutes", "fall_time_minutes")
    },
    **{
        vol.Optional(key): vol.All(vol.Coerce(int), vol.Range(min=-1439, max=1439))
        for key in ("sunrise_offset_minutes", "sunset_offset_minutes")
    },
}
_CIRCADIAN_KEYS = tuple(key.schema for key in _CIRCADIAN_FIELDS)


def _validate_circadian(data: dict[str, Any]) -> dict[str, Any]:
    """Require a complete config for the selected schedule mode."""
    explicit = ("rise_time_minutes", "fall_time_minutes")
    solar = ("sunrise_offset_minutes", "sunset_offset_minutes")
    required, excluded = (explicit, solar) if data["mode"] == "explicit" else (solar, explicit)
    if any(key not in data for key in required):
        raise vol.Invalid(f"{data['mode']} mode requires both {required[0]} and {required[1]}")
    if any(key in data for key in excluded):
        raise vol.Invalid(f"{data['mode']} mode does not accept {excluded[0]} or {excluded[1]}")
    return data


async def _target_entries(call: ServiceCall, capability: str) -> list[ConfigEntry]:
    """Resolve integration-owned targets and validate all before any device I/O."""
    hass = call.hass
    entry_ids = set(await async_extract_config_entry_ids(call))
    if entry_id := call.data.get("config_entry_id"):
        entry_ids.add(entry_id)
    entries = []
    for entry_id in sorted(entry_ids):
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        if entry.state is not ConfigEntryState.LOADED or not getattr(entry, "runtime_data", None):
            raise ServiceValidationError("The targeted Nanoleaf integration is not loaded")
        capabilities = (entry.runtime_data.data or {}).get("capabilities") or {}
        if not capabilities.get(capability):
            raise ServiceValidationError(
                f"The targeted Nanoleaf device does not support {capability}"
            )
        entries.append(entry)
    if not entries:
        raise ServiceValidationError("Select an Enhanced Nanoleaf entity or device")
    return entries


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register actions once; connections are opened only when an action is called."""

    async def set_circadian_config(call: ServiceCall) -> None:
        entries = await _target_entries(call, "circadian")
        config = {key: call.data[key] for key in _CIRCADIAN_KEYS if key in call.data}
        for entry in entries:
            await entry.runtime_data.async_set_circadian_config(config)

    async def set_device_time(call: ServiceCall) -> None:
        for entry in await _target_entries(call, "time"):
            await entry.runtime_data.async_set_device_time(
                epoch_seconds=call.data.get("epoch_seconds"),
                timezone_offset_seconds=call.data.get("timezone_offset_seconds"),
            )

    async def set_device_location(call: ServiceCall) -> None:
        for entry in await _target_entries(call, "location"):
            await entry.runtime_data.async_set_device_location(
                latitude=call.data["latitude"],
                longitude=call.data["longitude"],
            )

    async def get_motion_capabilities(call: ServiceCall) -> dict[str, Any]:
        devices = {}
        for entry in await _target_entries(call, "motions"):
            devices[entry.entry_id] = await entry.runtime_data.async_get_motion_capabilities()
        return {"devices": devices}

    registrations = (
        (
            "set_circadian_config",
            set_circadian_config,
            vol.All(
                vol.Schema({**_TARGET_FIELDS, **_CIRCADIAN_FIELDS}),
                _validate_circadian,
            ),
            SupportsResponse.NONE,
        ),
        (
            "set_device_time",
            set_device_time,
            vol.Schema(
                {
                    **_TARGET_FIELDS,
                    vol.Optional("epoch_seconds"): vol.All(vol.Coerce(int), vol.Range(min=0)),
                    vol.Optional("timezone_offset_seconds"): vol.All(
                        vol.Coerce(int), vol.Range(min=-86400, max=86400)
                    ),
                }
            ),
            SupportsResponse.NONE,
        ),
        (
            "set_device_location",
            set_device_location,
            vol.Schema(
                {
                    **_TARGET_FIELDS,
                    vol.Required("latitude"): vol.All(
                        vol.Coerce(float), vol.Range(min=-90, max=90)
                    ),
                    vol.Required("longitude"): vol.All(
                        vol.Coerce(float), vol.Range(min=-180, max=180)
                    ),
                }
            ),
            SupportsResponse.NONE,
        ),
        (
            "get_motion_capabilities",
            get_motion_capabilities,
            vol.Schema(_TARGET_FIELDS),
            SupportsResponse.ONLY,
        ),
    )
    for name, handler, schema, response in registrations:
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(
                DOMAIN,
                name,
                handler,
                schema=schema,
                supports_response=response,
            )
