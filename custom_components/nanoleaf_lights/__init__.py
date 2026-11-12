"""Enhanced Nanoleaf Light integration setup and teardown."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_IP_ADDRESS,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    CONF_TOKEN,
    DOMAIN,
)
from .coordinator import NanoleafLtpduCoordinator
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

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
    """Set up a config entry once the device has answered."""
    await _async_migrate_unique_ids(hass, entry)
    coordinator = NanoleafLtpduCoordinator(hass, entry)

    # Unreachable: ConfigEntryNotReady (setup retries). Token rejected: reauth.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_migrate_unique_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Replace colliding "None-" entity unique IDs with the config entry ID."""
    if entry.unique_id is not None:
        return
    legacy_prefix = "None-"

    @callback
    def _migrate(entity_entry: er.RegistryEntry) -> dict[str, Any] | None:
        if not entity_entry.unique_id.startswith(legacy_prefix):
            return None
        suffix = entity_entry.unique_id[len(legacy_prefix) :]
        return {"new_unique_id": f"{entry.entry_id}-{suffix}"}

    await er.async_migrate_entries(hass, entry.entry_id, _migrate)


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
