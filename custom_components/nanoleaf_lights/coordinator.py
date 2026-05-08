"""DataUpdateCoordinator for the Enhanced Nanoleaf Light integration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_IP_ADDRESS,
    CONF_MODEL,
    CONF_PORT,
    CONF_TOKEN,
    DOMAIN,
    SCENES_FILENAME,
    STORAGE_DIR,
    UPDATE_INTERVAL,
)
from .nl_ltpdu import LtpduSession, SceneLookup, SessionExpiredError

_LOGGER = logging.getLogger(__name__)


class NanoleafLtpduCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that holds one persistent LTPDU session per device.

    Lifecycle:
      - Session is opened (KEX + auth) on the first update.
      - SessionExpiredError → reauth() in-place without re-KEX.
      - Any other transport error → close session, retry on next tick.
      - Persistent auth failures → raise ConfigEntryAuthFailed to trigger reauth.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.unique_id}",
            update_interval=UPDATE_INTERVAL,
        )
        self.config_entry = entry
        self._session: LtpduSession | None = None
        self._consecutive_auth_failures: int = 0
        self._AUTH_FAILURE_THRESHOLD = 3
        self.scenes_path: Path = (
            Path(hass.config.config_dir) / ".storage" / STORAGE_DIR / SCENES_FILENAME
        )
        self._scene_lookup: SceneLookup | None = None

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    async def _open_session(self) -> LtpduSession:
        """Perform KEX + auth and return a ready session."""
        data = self.config_entry.data
        ip = data[CONF_IP_ADDRESS]
        port = data[CONF_PORT]
        model = data.get(CONF_MODEL)
        token = bytes.fromhex(data[CONF_TOKEN])

        session = await LtpduSession.connect(ip, port, model=model, timeout=10.0)
        try:
            await session.auth(token)
        except Exception:
            await session.close()
            raise
        return session

    async def _safe_close(self) -> None:
        """Close the session without raising."""
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def _refresh_static_data(self) -> None:
        """Query slow-changing device and Thread info once after a fresh session."""
        assert self._session is not None
        try:
            device = await self._session.query_device_info(timeout=10.0)
        except Exception as exc:
            _LOGGER.warning("Could not fetch device info: %s", exc)
            device = {}

        thread: dict[str, Any] | None = None
        try:
            caps = await self._session.query_thread_capabilities(timeout=10.0)
            role = await self._session.query_thread_role(timeout=10.0)
            net = await self._session.query_thread_network_info(timeout=10.0)
            thread = {**caps, **role, **net}
        except Exception as exc:
            _LOGGER.debug("Could not fetch Thread info: %s", exc)

        scene_handles: list[int] = []
        try:
            scene_handles = list(await self._session.list_scenes(timeout=10.0))
        except Exception as exc:
            _LOGGER.warning("Could not fetch scene list: %s", exc)

        scene_names: dict[int, str] = {}
        for b in scene_handles:
            try:
                detail = await self._session.get_scene(bytes([b]), timeout=10.0)
                palette = (detail or {}).get("palette", "")
                match = self._scene_lookup.resolve(palette) if palette else None
                scene_names[b] = match[0] if match else f"Scene 0x{b:02x}"
            except Exception:
                scene_names[b] = f"Scene 0x{b:02x}"

        # Merge into current data (may still be None on first run).
        existing = self.data or {}
        self.async_set_updated_data(
            {**existing, "device": device, "thread": thread,
             "scene_handles": scene_handles, "scene_names": scene_names}
        )

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch device state; re-open session when needed."""
        if self._scene_lookup is None:
            self._scene_lookup = await SceneLookup.from_path_async(self.scenes_path)

        if self._session is None:
            try:
                self._session = await self._open_session()
            except RuntimeError as exc:
                # Distinguish permanent auth rejection from transient errors.
                if "rejected" in str(exc).lower():
                    self._consecutive_auth_failures += 1
                    if self._consecutive_auth_failures >= self._AUTH_FAILURE_THRESHOLD:
                        raise ConfigEntryAuthFailed(
                            f"Token rejected by device after {self._consecutive_auth_failures} attempts"
                        ) from exc
                raise UpdateFailed(f"Cannot open session: {exc}") from exc
            self._consecutive_auth_failures = 0
            # Populate static data right after a fresh connection.
            await self._refresh_static_data()

        existing = self.data or {}

        async def _query() -> tuple[dict[str, Any], bytes]:
            assert self._session is not None
            light = await self._session.query_light_state(timeout=10.0)
            current_scene = await self._session.get_current_scene(timeout=10.0)
            return light, current_scene

        try:
            light, current_scene = await _query()
        except SessionExpiredError:
            _LOGGER.debug("Session expired, attempting reauth")
            try:
                await self._session.reauth()
                light, current_scene = await _query()
            except Exception as exc:
                _LOGGER.warning("Reauth failed: %s — will reconnect", exc)
                await self._safe_close()
                raise UpdateFailed("Reauth failed, will reconnect") from exc
        except Exception as exc:
            _LOGGER.debug("Query failed: %s — will reconnect", exc)
            await self._safe_close()
            raise UpdateFailed(f"Connection lost: {exc}") from exc

        return {
            **existing,
            "light": light,
            "current_scene": current_scene,
            "scene_handles": existing.get("scene_handles", []),
            "scene_names": existing.get("scene_names", {}),
        }

    # ------------------------------------------------------------------
    # Service call helper
    # ------------------------------------------------------------------

    async def async_service_call(
        self, action: Callable[[LtpduSession], Coroutine[Any, Any, None]]
    ) -> None:
        """Execute *action(session)*, opening or reauthenticating as needed.

        Raises HomeAssistantError on unrecoverable failure so the caller
        never needs to touch _session directly.
        """
        if self._session is None:
            try:
                self._session = await self._open_session()
            except Exception as exc:
                raise HomeAssistantError(f"Device unavailable: {exc}") from exc

        try:
            await action(self._session)
        except SessionExpiredError:
            try:
                await self._session.reauth()
                await action(self._session)
            except Exception as exc:
                await self._safe_close()
                raise HomeAssistantError(f"Session error: {exc}") from exc
        except Exception as exc:
            await self._safe_close()
            raise HomeAssistantError(f"Command failed: {exc}") from exc

    async def async_reload_scenes(self) -> None:
        """Reload the scene lookup after scenes.json has been updated."""
        self._scene_lookup = await SceneLookup.from_path_async(self.scenes_path)
        existing = self.data or {}
        scene_handles: list[int] = existing.get("scene_handles", [])
        scene_names: dict[int, str] = {}
        if self._session is not None:
            for b in scene_handles:
                try:
                    detail = await self._session.get_scene(bytes([b]), timeout=10.0)
                    palette = (detail or {}).get("palette", "")
                    match = self._scene_lookup.resolve(palette) if palette else None
                    scene_names[b] = match[0] if match else f"Scene 0x{b:02x}"
                except Exception:
                    scene_names[b] = f"Scene 0x{b:02x}"
        self.async_set_updated_data({**existing, "scene_names": scene_names})

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def async_shutdown(self) -> None:
        """Close the LTPDU session on coordinator teardown."""
        await self._safe_close()
        await super().async_shutdown()
