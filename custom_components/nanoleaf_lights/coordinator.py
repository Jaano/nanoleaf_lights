"""DataUpdateCoordinator for the Enhanced Nanoleaf Light integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_IP_ADDRESS,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    CONF_TOKEN,
    DOMAIN,
    SCENES_FILENAME,
    STORAGE_DIR,
    UPDATE_INTERVAL,
)
from .nl_ltpdu import AuthRejectedError, SceneLookup, SessionExpiredError
from .nl_ltpdu_v3 import device_capabilities
from .session import connect_session, scene_fallback_name, scene_handle_bytes

if TYPE_CHECKING:
    from .session import Session

_LOGGER = logging.getLogger(__name__)
_SETTINGS_INTERVAL = 60.0
_THREAD_INTERVAL = 60.0
_THREAD_QUERIES = {
    "capabilities": "query_thread_capabilities",
    "role": "query_thread_role",
    "network": "query_thread_network_info",
}
_CIRCADIAN_FIELDS = {
    "daylight_target_kelvin": "daylightTargetK",
    "evening_target_kelvin": "eveningTargetK",
    "morning_target_kelvin": "morningTargetK",
    "rise_ramp_minutes": "riseRampMinutes",
    "fall_ramp_minutes": "fallRampMinutes",
    "morning_ramp_minutes": "morningRampMinutes",
    "rise_time_minutes": "riseTimeOfDayMinutes",
    "fall_time_minutes": "fallTimeOfDayMinutes",
    "sunrise_offset_minutes": "sunriseOffsetMinutes",
    "sunset_offset_minutes": "sunsetOffsetMinutes",
}


def _circadian_config(config: dict[str, Any], *, to_device: bool) -> dict[str, Any]:
    """Translate the action schema and protocol representation without defaults."""
    if not to_device:
        config = {key: value for key, value in config.items() if key != "version"}
    fields = _CIRCADIAN_FIELDS if to_device else {v: k for k, v in _CIRCADIAN_FIELDS.items()}
    modes = {"explicit": "explicit", "sunrise_sunset": "sunriseSunset"}
    if not to_device:
        modes = {v: k for k, v in modes.items()}
    mode = config.get("mode")
    if mode not in modes or set(config) - (set(fields) | {"mode"}):
        raise HomeAssistantError("Invalid circadian configuration fields or mode")
    if to_device:
        common = set(_CIRCADIAN_FIELDS) - {
            "rise_time_minutes",
            "fall_time_minutes",
            "sunrise_offset_minutes",
            "sunset_offset_minutes",
        }
        timing = (
            {"rise_time_minutes", "fall_time_minutes"}
            if mode == "explicit"
            else {"sunrise_offset_minutes", "sunset_offset_minutes"}
        )
        if set(config) != common | timing | {"mode"}:
            raise HomeAssistantError(
                "Supply all white points and ramps, and only the timing fields "
                "for the selected mode"
            )
    return {
        "mode": modes[mode],
        **{fields[key]: value for key, value in config.items() if key != "mode"},
    }


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
        self._session: Session | None = None
        self.connected: bool = False
        self._consecutive_auth_failures: int = 0
        self._AUTH_FAILURE_THRESHOLD = 3
        self.scenes_path: Path = (
            Path(hass.config.config_dir) / ".storage" / STORAGE_DIR / SCENES_FILENAME
        )
        self._scene_lookup: SceneLookup | None = None
        self._settings_updated_at = 0.0
        self._thread_updated_at = 0.0
        self._settings_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    async def _open_session(self) -> Session:
        """Authenticate with LTPDUv3, falling back to legacy CoAP."""
        data = self.config_entry.data
        ip = data[CONF_IP_ADDRESS]
        port = data[CONF_PORT]
        model = data.get(CONF_MODEL)
        token = bytes.fromhex(data[CONF_TOKEN])

        try:
            session = await connect_session(
                ip, port, model=model, name=data.get(CONF_NAME), token=token, timeout=10.0
            )
        except AuthRejectedError:
            self.connected = True
            raise
        except Exception:
            self.connected = False
            raise
        self.connected = True
        return session

    async def _safe_close(self) -> None:
        """Close the session without raising."""
        self.connected = False
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def _refresh_device_info(self) -> None:
        """Refresh metadata and the optional feature profile."""
        assert self._session is not None
        try:
            device = await self._session.query_device_info(timeout=10.0)
        except Exception as exc:
            _LOGGER.warning("Could not fetch device info: %s", exc)
            device = {}
        self.async_set_updated_data(
            {
                **(self.data or {}),
                "device": device,
                "capabilities": device_capabilities(
                    device.get("model_number") or self.config_entry.data.get(CONF_MODEL),
                    device.get("firmware_version"),
                ),
            }
        )

    async def _refresh_static_data(self) -> None:
        """Query slow-changing device and Thread info once after a fresh session."""
        assert self._session is not None
        await self._refresh_device_info()

        await self._refresh_thread_data(force=True)

        scene_handles: list[bytes] = []
        try:
            scene_handles = [
                scene_handle_bytes(handle)
                for handle in await self._session.list_scenes(timeout=10.0)
            ]
        except Exception as exc:
            _LOGGER.warning("Could not fetch scene list: %s", exc)

        scene_names: dict[bytes, str] = {}
        for b in scene_handles:
            try:
                detail = await self._session.get_scene(b, timeout=10.0)
                palette = (detail or {}).get("palette", "")
                match = self._scene_lookup.resolve(palette) if palette else None
                reported_name = (detail or {}).get("name")
                scene_names[b] = reported_name or (match[0] if match else scene_fallback_name(b))
            except Exception:
                scene_names[b] = scene_fallback_name(b)

        # Merge into current data (may still be None on first run).
        existing = dict(self.data or {})
        existing.pop("motion_capabilities", None)
        self.async_set_updated_data(
            {
                **existing,
                "scene_handles": scene_handles,
                "scene_names": scene_names,
            }
        )

    async def _refresh_thread_data(self, *, force: bool = False) -> None:
        """Read Thread groups independently and retry only missing readbacks."""
        assert self._session is not None
        readbacks = (
            dict.fromkeys(_THREAD_QUERIES)
            if force else dict((self.data or {}).get("thread_readbacks") or {})
        )
        try:
            for group, method in _THREAD_QUERIES.items():
                if readbacks.get(group) is not None:
                    continue
                readbacks[group] = None
                try:
                    value = await getattr(self._session, method)(timeout=10.0)
                    if not value or value.get("status", 0) != 0:
                        raise RuntimeError(f"Thread {group} returned no valid readback")
                    readbacks[group] = value
                except (SessionExpiredError, TimeoutError, OSError):
                    raise
                except Exception as exc:
                    if getattr(self._session, "_cipher_corrupted", False) or getattr(
                        self._session, "_broken", False
                    ):
                        raise ConnectionError("Thread exchange invalidated the session") from exc
                    _LOGGER.debug("Could not fetch Thread %s: %s", group, exc)
        finally:
            self._thread_updated_at = monotonic()
            thread = {
                key: value
                for group in _THREAD_QUERIES
                for key, value in (readbacks.get(group) or {}).items()
            }
            self.async_set_updated_data(
                {**(self.data or {}), "thread": thread or None, "thread_readbacks": readbacks}
            )

    async def _refresh_settings(self, groups: set[str] | None = None) -> None:
        """Serialize settings reads with settings actions."""
        async with self._settings_lock:
            device = (self.data or {}).get("device") or {}
            if self._settings_updated_at and (
                not device.get("firmware_version")
                or not (device.get("model_number") or self.config_entry.data.get(CONF_MODEL))
            ):
                await self._refresh_device_info()
            await self._read_settings(groups)

    async def _read_settings(self, groups: set[str] | None = None) -> None:
        """Read supported settings without making unavailable options look disabled."""
        assert self._session is not None
        values: dict[str, Any] = {}
        capabilities = (self.data or {}).get("capabilities", {})
        queries = {
            "circadian": ("circadian", "query_circadian_settings"),
            "clock": ("time", "query_device_time"),
            "location": ("location", "query_device_location"),
            "power_loss_recovery": ("power_loss_recovery", "query_power_loss_recovery"),
            "motion_capabilities": ("motions", "query_motion_capabilities"),
        }
        for key, (feature, method) in queries.items():
            if groups is not None and key not in groups:
                continue
            values[key] = None
            if not capabilities.get(feature):
                continue
            try:
                value = await getattr(self._session, method)(timeout=10.0)
                if key == "circadian" and value.get("config") is not None:
                    value = {**value, "config": _circadian_config(value["config"], to_device=False)}
                if key == "circadian" and value.get("config_error"):
                    _LOGGER.debug("Circadian configuration read failed: %s", value["config_error"])
                values[key] = value
            except (SessionExpiredError, TimeoutError, OSError):
                # Stop after transport failure; a fresh session will retry these reads.
                self.async_set_updated_data({**(self.data or {}), **values})
                raise
            except Exception as exc:
                if getattr(self._session, "_cipher_corrupted", False) or getattr(
                    self._session, "_broken", False
                ):
                    self.async_set_updated_data({**(self.data or {}), **values})
                    raise ConnectionError("Settings exchange invalidated the session") from exc
                _LOGGER.debug("Could not read %s: %s", key, exc)
        self.async_set_updated_data({**(self.data or {}), **values})

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
            except AuthRejectedError as exc:
                # Device reachable but token invalid — re-pairing/onboarding needed.
                self._consecutive_auth_failures += 1
                if self._consecutive_auth_failures >= self._AUTH_FAILURE_THRESHOLD:
                    raise ConfigEntryAuthFailed(
                        f"Token rejected by device after {self._consecutive_auth_failures} attempts"
                    ) from exc
                raise UpdateFailed(
                    f"Token rejected (attempt {self._consecutive_auth_failures})"
                ) from exc
            except Exception as exc:
                raise UpdateFailed(f"Cannot open session: {exc}") from exc
            self._consecutive_auth_failures = 0
            # Populate static data right after a fresh connection.
            try:
                await self._refresh_static_data()
            except (SessionExpiredError, TimeoutError, OSError) as exc:
                await self._safe_close()
                raise UpdateFailed(
                    f"Connection lost while reading device diagnostics: {exc}"
                ) from exc

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

        if monotonic() - self._thread_updated_at >= _THREAD_INTERVAL and any(
            ((self.data or {}).get("thread_readbacks") or {}).get(group) is None
            for group in _THREAD_QUERIES
        ):
            try:
                await self._refresh_thread_data()
            except (SessionExpiredError, TimeoutError, OSError) as exc:
                _LOGGER.debug("Thread connection failed: %s", exc)
                await self._safe_close()

        if (
            self._session is not None
            and monotonic() - self._settings_updated_at >= _SETTINGS_INTERVAL
        ):
            groups = {"circadian", "clock", "location", "power_loss_recovery"}
            if (self.data or {}).get("motion_capabilities") is None:
                groups.add("motion_capabilities")
            try:
                await self._refresh_settings(groups)
            except (SessionExpiredError, TimeoutError, OSError) as exc:
                _LOGGER.debug("Settings connection failed: %s", exc)
                await self._safe_close()
                # Keep the successful light state and reconnect on the next poll.
                # Back off optional reads so a silent endpoint cannot starve controls.
            self._settings_updated_at = monotonic()
        existing = self.data or {}
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
        self, action: Callable[[Session], Coroutine[Any, Any, None]]
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

    def _require_feature(self, feature: str) -> None:
        if not (self.data or {}).get("capabilities", {}).get(feature):
            raise HomeAssistantError(f"This device does not support {feature}")

    async def _settings_call(
        self, action: Callable[[Session], Coroutine[Any, Any, None]], groups: set[str]
    ) -> None:
        """Write settings, then read the device's accepted values back."""

        async def update(session: Session) -> None:
            await action(session)
            await self._read_settings(groups)

        async with self._settings_lock:
            await self.async_service_call(update)
        await self.async_request_refresh()

    def _clock_values(
        self, epoch_seconds: int | None, timezone_offset_seconds: int | None
    ) -> tuple[int, int]:
        epoch = int(datetime.now(UTC).timestamp()) if epoch_seconds is None else epoch_seconds
        if isinstance(epoch, bool) or not isinstance(epoch, int) or not 0 <= epoch <= (1 << 64) - 1:
            raise HomeAssistantError("epoch_seconds must be a nonnegative integer")
        if timezone_offset_seconds is None:
            try:
                moment = datetime.fromtimestamp(epoch, ZoneInfo(self.hass.config.time_zone))
                offset = moment.utcoffset()
            except (ValueError, OverflowError, OSError) as exc:
                raise HomeAssistantError(
                    "Cannot determine timezone offset for this timestamp"
                ) from exc
            assert offset is not None
            timezone_offset_seconds = int(offset.total_seconds())
        if (
            isinstance(timezone_offset_seconds, bool)
            or not isinstance(timezone_offset_seconds, int)
            or not -86400 <= timezone_offset_seconds <= 86400
        ):
            raise HomeAssistantError("timezone_offset_seconds must be an integer within one day")
        return epoch, timezone_offset_seconds

    async def async_set_circadian_enabled(self, enabled: bool) -> None:
        self._require_feature("circadian")
        groups = {"circadian"}
        if enabled:
            epoch, offset = self._clock_values(None, None)
            self._require_feature("time")
            self._require_feature("location")
            groups |= {"clock", "location"}

        async def update(session: Session) -> None:
            if enabled:
                await session.set_device_time(epoch, offset)
                await session.set_device_location(
                    self.hass.config.latitude, self.hass.config.longitude
                )
            await session.set_circadian_enabled(enabled)

        await self._settings_call(update, groups)

    async def async_set_circadian_config(self, config: dict[str, Any]) -> None:
        self._require_feature("circadian")
        wire_config = _circadian_config(config, to_device=True)
        groups = {"circadian"}
        solar = config["mode"] == "sunrise_sunset"
        if solar:
            self._require_feature("time")
            self._require_feature("location")
            epoch, offset = self._clock_values(None, None)
            groups |= {"clock", "location"}

        async def update(session: Session) -> None:
            if solar:
                await session.set_device_time(epoch, offset)
                await session.set_device_location(
                    self.hass.config.latitude, self.hass.config.longitude
                )
            await session.set_circadian_config(wire_config)

        await self._settings_call(update, groups)

    async def async_set_device_time(
        self, epoch_seconds: int | None = None, timezone_offset_seconds: int | None = None
    ) -> None:
        self._require_feature("time")
        epoch, offset = self._clock_values(epoch_seconds, timezone_offset_seconds)
        await self._settings_call(lambda session: session.set_device_time(epoch, offset), {"clock"})

    async def async_set_device_location(self, latitude: float, longitude: float) -> None:
        self._require_feature("location")
        await self._settings_call(
            lambda session: session.set_device_location(latitude, longitude), {"location"}
        )

    async def async_sync_clock_and_location(self) -> None:
        self._require_feature("circadian")
        if ((self.data or {}).get("circadian") or {}).get("enabled") is None:
            raise HomeAssistantError("Auto Circadian Lighting is unavailable")
        self._require_feature("time")
        self._require_feature("location")
        epoch, offset = self._clock_values(None, None)

        async def update(session: Session) -> None:
            await session.set_device_time(epoch, offset)
            await session.set_device_location(self.hass.config.latitude, self.hass.config.longitude)

        await self._settings_call(update, {"clock", "location"})

    async def async_set_power_loss_recovery(self, enabled: bool) -> None:
        self._require_feature("power_loss_recovery")
        await self._settings_call(
            lambda session: session.set_power_loss_recovery(enabled), {"power_loss_recovery"}
        )

    async def async_get_motion_capabilities(self) -> dict[str, Any]:
        self._require_feature("motions")
        result: dict[str, Any] = {}

        async def query(session: Session) -> None:
            nonlocal result
            result = await session.query_motion_capabilities()

        async with self._settings_lock:
            await self.async_service_call(query)
            self.async_set_updated_data({**(self.data or {}), "motion_capabilities": result})
        return result

    async def async_reload_scenes(self) -> None:
        """Reload the scene lookup after scenes.json has been updated."""
        self._scene_lookup = await SceneLookup.from_path_async(self.scenes_path)
        existing = self.data or {}
        scene_handles: list[bytes] = existing.get("scene_handles", [])
        scene_names: dict[bytes, str] = {}
        if self._session is not None:
            for b in scene_handles:
                try:
                    detail = await self._session.get_scene(b, timeout=10.0)
                    palette = (detail or {}).get("palette", "")
                    match = self._scene_lookup.resolve(palette) if palette else None
                    reported_name = (detail or {}).get("name")
                    scene_names[b] = reported_name or (
                        match[0] if match else scene_fallback_name(b)
                    )
                except Exception:
                    scene_names[b] = scene_fallback_name(b)
        self.async_set_updated_data({**existing, "scene_names": scene_names})

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def async_shutdown(self) -> None:
        """Close the LTPDU session on coordinator teardown."""
        await self._safe_close()
        await super().async_shutdown()
