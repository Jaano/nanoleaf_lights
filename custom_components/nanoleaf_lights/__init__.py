"""Enhanced Nanoleaf Light integration setup and teardown."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_IP_ADDRESS,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    CONF_TOKEN,
)
from .coordinator import NanoleafLtpduCoordinator
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register integration actions independently of loaded devices."""
    await async_setup_services(hass)
    return True


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
