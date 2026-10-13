"""Enhanced Nanoleaf Light integration setup and teardown."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_COMPANION_DEVICE_ID,
    CONF_COMPANION_SERIAL_NUMBER,
    CONF_DEVICE_LINK_MODE,
    CONF_IP_ADDRESS,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    CONF_TOKEN,
    DEVICE_LINK_MODE_AUTO,
    DEVICE_LINK_MODE_COMPANION,
    DEVICE_LINK_MODE_STANDALONE,
    DOMAIN,
    NATIVE_COMPANION_DOMAINS,
)
from .coordinator import NanoleafLtpduCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


def _device_label(device: dr.DeviceEntry, domain: str | None) -> str:
    """Return a useful device label for logging."""
    name = device.name_by_user or device.name or device.model or device.id
    if domain is None:
        return name
    return f"{name} ({domain})"


def _config_entry_domain(hass: HomeAssistant, device: dr.DeviceEntry) -> str | None:
    """Return the owning config entry domain for a device."""
    target_entry = hass.config_entries.async_get_entry(device.config_entry_id)
    return target_entry.domain if target_entry is not None else None


def _enhanced_serial(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: NanoleafLtpduCoordinator,
) -> str | None:
    """Return the latest known Enhanced Nanoleaf serial number."""
    device = (coordinator.data or {}).get("device") or {}
    serial = device.get("serial_number")
    if isinstance(serial, str) and serial:
        return serial

    serial = entry.options.get(CONF_COMPANION_SERIAL_NUMBER)
    if isinstance(serial, str) and serial:
        return serial

    own_device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.unique_id or entry.entry_id),
        entry.entry_id,
    )
    serial = own_device.serial_number if own_device is not None else None
    return serial if isinstance(serial, str) and serial else None


def _native_nanoleaf_candidates(
    hass: HomeAssistant,
    entry: ConfigEntry,
    serial: str,
) -> list[dr.DeviceEntry]:
    """Return Matter/HomeKit Nanoleaf devices matching the Enhanced device serial."""
    device_registry = dr.async_get(hass)
    candidates: list[dr.DeviceEntry] = []
    for device in device_registry.devices.values():
        if device.config_entry_id == entry.entry_id:
            continue
        if (device.manufacturer or "").casefold() != "nanoleaf":
            continue
        if device.serial_number != serial:
            continue
        if _config_entry_domain(hass, device) not in NATIVE_COMPANION_DOMAINS:
            continue
        candidates.append(device)
    return candidates


def _valid_companion_device(
    hass: HomeAssistant,
    device: dr.DeviceEntry,
    expected_serial: str | None,
) -> bool:
    """Return whether a device is a safe Matter/HomeKit companion target."""
    target_domain = _config_entry_domain(hass, device)
    if target_domain not in NATIVE_COMPANION_DOMAINS:
        _LOGGER.warning(
            "Companion target %s is owned by %s, expected Matter or HomeKit",
            _device_label(device, target_domain),
            target_domain,
        )
        return False

    if (device.manufacturer or "").casefold() != "nanoleaf":
        _LOGGER.warning(
            "Companion target %s is manufactured by %s, expected Nanoleaf",
            _device_label(device, target_domain),
            device.manufacturer,
        )
        return False

    if expected_serial and device.serial_number != expected_serial:
        _LOGGER.warning(
            "Companion target %s serial changed from %s to %s; keeping Enhanced entities "
            "on their standalone device",
            _device_label(device, target_domain),
            expected_serial,
            device.serial_number,
        )
        return False

    return True


def _resolve_companion_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: NanoleafLtpduCoordinator,
) -> dr.DeviceEntry | None:
    """Resolve and validate the native Matter/HomeKit device selected in options."""
    mode = entry.options.get(CONF_DEVICE_LINK_MODE, DEVICE_LINK_MODE_AUTO)
    if mode == DEVICE_LINK_MODE_STANDALONE:
        return None

    serial = _enhanced_serial(hass, entry, coordinator)
    if mode == DEVICE_LINK_MODE_AUTO:
        if serial is None:
            _LOGGER.debug("No serial number available for automatic companion matching")
            return None

        candidates = _native_nanoleaf_candidates(hass, entry, serial)
        if len(candidates) == 1:
            device = candidates[0]
            _LOGGER.debug(
                "Automatically matched Enhanced Nanoleaf serial %s to %s",
                serial,
                _device_label(device, _config_entry_domain(hass, device)),
            )
            return device
        if candidates:
            _LOGGER.warning(
                "Found %d Matter/HomeKit Nanoleaf devices with serial %s; keeping "
                "Enhanced entities on their standalone device",
                len(candidates),
                serial,
            )
        return None

    if mode != DEVICE_LINK_MODE_COMPANION:
        _LOGGER.warning("Unknown device link mode %s", mode)
        return None

    target_device_id = entry.options.get(CONF_COMPANION_DEVICE_ID)
    if not isinstance(target_device_id, str) or not target_device_id:
        _LOGGER.warning("Companion mode is enabled but no target device is configured")
        return None

    device_registry = dr.async_get(hass)
    device = device_registry.async_get(target_device_id)
    if device is None:
        _LOGGER.warning("Companion target device %s no longer exists", target_device_id)
        return None

    expected_serial = entry.options.get(CONF_COMPANION_SERIAL_NUMBER)
    if not isinstance(expected_serial, str) or not expected_serial:
        expected_serial = serial

    if not _valid_companion_device(hass, device, expected_serial):
        return None

    return device


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry: create coordinator, do a non-blocking first refresh."""
    coordinator = NanoleafLtpduCoordinator(hass, entry)

    # Do not use async_config_entry_first_refresh(): it raises
    # ConfigEntryNotReady on failure, which puts the entry into "Needs
    # attention" whenever the device is merely powered off. async_refresh()
    # never raises; the coordinator keeps polling every UPDATE_INTERVAL and
    # entities recover automatically once the device comes back. If the
    # stored token is rejected, the coordinator raises ConfigEntryAuthFailed
    # internally and the DataUpdateCoordinator machinery starts the reauth
    # flow on its own.
    await coordinator.async_refresh()

    coordinator.companion_device_entry = _resolve_companion_device(
        hass,
        entry,
        coordinator,
    )
    if coordinator.companion_device_entry is not None:
        _LOGGER.debug(
            "Attaching Enhanced Nanoleaf entities for %s to device %s",
            entry.title,
            coordinator.companion_device_entry.id,
        )

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry: stop coordinator and close session."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: NanoleafLtpduCoordinator = entry.runtime_data
        await coordinator.async_shutdown()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Revoke the device pairing token when the entry is deleted."""
    from .session import connect_session

    data = entry.data
    ip = data[CONF_IP_ADDRESS]
    port = data[CONF_PORT]
    model = data.get(CONF_MODEL)
    token = bytes.fromhex(data[CONF_TOKEN])

    try:
        session = await connect_session(
            ip, port, model=model, name=data.get(CONF_NAME), token=token, timeout=10.0
        )
        try:
            await session.unpair()
            _LOGGER.debug("remove_entry: unpaired %s:%d", ip, port)
        finally:
            await session.close()
    except Exception as exc:
        _LOGGER.warning("remove_entry: could not unpair %s:%d — %s", ip, port, exc)
