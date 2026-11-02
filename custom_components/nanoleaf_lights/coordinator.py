"""DataUpdateCoordinator for the Enhanced Nanoleaf Light integration."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, TypeVar
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
    PUBLIC_SCENES_DIR,
    SCENES_FILENAME,
    STORAGE_DIR,
    UPDATE_INTERVAL,
)
from .nl_ltpdu import (
    AuthRejectedError,
    LtpduSession,
    SceneLookup,
    SessionExpiredError,
    _parse_scene_data,
    build_simple_scene_tlv,
)
from .nl_ltpdu_v3 import (
    EFFECT_ID_MAX,
    LtpduV3Session,
    _parse_effect,
    build_effect_v2,
    device_capabilities,
)
from .session import connect_session, scene_fallback_name, scene_handle_bytes

if TYPE_CHECKING:
    from .session import Session

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")
_SETTINGS_INTERVAL = 60.0
_THREAD_INTERVAL = 60.0
_SCENE_LIMIT = 12
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


def _select_scenes_path(config_dir: Path) -> Path:
    public_path = config_dir / "www" / PUBLIC_SCENES_DIR / SCENES_FILENAME
    private_path = config_dir / ".storage" / STORAGE_DIR / SCENES_FILENAME
    if public_path.is_file() or not private_path.is_file():
        return public_path
    return private_path


def _load_scene_entry(path: Path, scene_uuid: str) -> tuple[str, str]:
    with path.open(encoding="utf-8") as file:
        database = json.load(file)
    if not isinstance(database, dict) or not isinstance(database.get("effects"), list):
        raise ValueError("Scene database must contain an effects array")
    matches = [
        entry for entry in database["effects"]
        if isinstance(entry, dict) and entry.get("uuid") == scene_uuid
    ]
    if len(matches) != 1:
        raise ValueError("Scene UUID is missing or ambiguous; refresh the scene database")
    entry = matches[0]
    name = entry.get("name")
    palette = entry.get("palette")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Scene has no valid name")
    if not isinstance(palette, str):
        raise ValueError("Scene has no RGB palette")
    return name, palette


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
            Path(hass.config.config_dir) / "www" / PUBLIC_SCENES_DIR / SCENES_FILENAME
        )
        self._scene_lookup: SceneLookup | None = None
        self._settings_updated_at = 0.0
        self._thread_updated_at = 0.0
        self._settings_lock = asyncio.Lock()
        self._scene_lock = asyncio.Lock()

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
            await self.async_reload_scenes()

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
        self, action: Callable[[Session], Coroutine[Any, Any, _T]]
    ) -> _T:
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
            return await action(self._session)
        except SessionExpiredError:
            try:
                await self._session.reauth()
                return await action(self._session)
            except Exception as exc:
                await self._safe_close()
                raise HomeAssistantError(f"Session error: {exc}") from exc
        except HomeAssistantError:
            raise
        except Exception as exc:
            await self._safe_close()
            raise HomeAssistantError(f"Command failed: {exc}") from exc

    def _require_feature(self, feature: str) -> None:
        if not (self.data or {}).get("capabilities", {}).get(feature):
            raise HomeAssistantError(f"This device does not support {feature}")

    async def _scene_call(
        self, action: Callable[[LtpduSession], Coroutine[Any, Any, _T]]
    ) -> _T:
        async def execute(session: Session) -> _T:
            if not isinstance(session, LtpduSession):
                raise HomeAssistantError("Scene upload requires the NL45 legacy protocol")
            return await action(session)

        return await self.async_service_call(execute)

    async def _native_scene_call(
        self, action: Callable[[LtpduV3Session], Coroutine[Any, Any, _T]]
    ) -> _T:
        async def execute(session: Session) -> _T:
            if not isinstance(session, LtpduV3Session):
                raise HomeAssistantError("NL67 scene upload requires native effectV2 over LTPDUv3")
            return await action(session)

        return await self.async_service_call(execute)

    async def async_scene_action(
        self,
        scene: str,
        effect: str,
        transition: int,
        *,
        upload: bool,
        activate: bool = False,
    ) -> None:
        """Preview or save a supported palette animation without replacing scenes."""
        self._require_feature("scene_upload")
        if effect not in {"fade", "random", "highlight"}:
            raise HomeAssistantError("Unsupported scene animation")
        if type(transition) is not int or not 0 <= transition <= 255:
            raise HomeAssistantError("Transition must be an integer from 0 to 255")
        if activate and not upload:
            raise HomeAssistantError("Activation is only available after upload")
        try:
            scenes_path = await self.async_select_scenes_path()
            source: tuple[str, str] = await self.hass.async_add_executor_job(
                _load_scene_entry, scenes_path, scene
            )
            name, palette = source
            native = (self.data or {}).get("capabilities", {}).get("scene_protocol") == "effectV2"
            payload = (
                build_effect_v2(palette, 1, name, effect=effect, transition=transition)
                if native else
                build_simple_scene_tlv(palette, 1, effect=effect, transition=transition)
            )
        except (OSError, ValueError) as exc:
            raise HomeAssistantError(f"Cannot prepare scene: {exc}") from exc

        async with self._scene_lock:
            if native:
                motions = await self._native_scene_call(
                    lambda session: session.query_motion_capabilities()
                )
                expected = _parse_effect(payload)
                if expected["motion_id"] not in {
                    motion["id"] for motion in motions["motions"]
                }:
                    raise HomeAssistantError("The bulb does not support this scene animation")
                if upload:
                    await self._upload_effect_v2(
                        name, palette, effect, transition, activate=activate
                    )
                else:
                    await self._native_scene_call(lambda session: session.preview_scene(payload))
            elif upload:
                await self._upload_scene(name, palette, effect, transition, activate=activate)
            else:
                await self._scene_call(lambda session: session.preview_scene(payload))
            await self.async_request_refresh()

    async def _allocate_effect_id(self, handles: list[bytes], name: str) -> bytes:
        if any(len(handle) != 4 for handle in handles) or len(set(handles)) != len(handles):
            raise HomeAssistantError("Invalid native scene ID list; upload was not attempted")
        if len(handles) >= _SCENE_LIMIT:
            raise HomeAssistantError("NL67 scene storage is full (12 listed scenes)")
        for handle in handles:
            detail = await self._native_scene_call(
                lambda session, identifier=handle: session.get_scene(identifier)
            )
            if not detail or not detail.get("name"):
                raise HomeAssistantError(
                    "Cannot check existing scene names; upload was not attempted"
                )
            if detail["name"] == name:
                raise HomeAssistantError(
                    "A scene with this name already exists; upload was not attempted"
                )
        identifier = secrets.randbelow(EFFECT_ID_MAX - 1) + 1
        while identifier.to_bytes(4, "big") in handles:
            identifier = identifier % (EFFECT_ID_MAX - 1) + 1
        return identifier.to_bytes(4, "big")

    async def _upload_effect_v2(
        self, name: str, palette: str, effect: str, transition: int, *, activate: bool
    ) -> None:
        handles = await self._native_scene_call(lambda session: session.list_scenes())
        assigned = await self._allocate_effect_id(handles, name)
        payload = build_effect_v2(
            palette, int.from_bytes(assigned, "big"), name, effect=effect, transition=transition
        )
        expected = _parse_effect(payload)
        try:
            returned = await self._native_scene_call(lambda session: session.add_scene(payload))
        except HomeAssistantError as exc:
            raise HomeAssistantError(
                f"Upload failed or its outcome is unknown: {exc}. "
                "Check the bulb's scene list before retrying"
            ) from exc
        if returned != assigned:
            raise HomeAssistantError(
                "Upload returned an unexpected scene ID; check the bulb before retrying"
            )
        try:
            detail = await self._native_scene_call(lambda session: session.get_scene(assigned))
        except HomeAssistantError as exc:
            raise HomeAssistantError(
                f"Scene was uploaded but readback failed: {exc}. Check the bulb before retrying"
            ) from exc
        if not detail or any(
            detail.get(key) != expected[key] for key in ("id", "name", "motion_id", "palette_data")
        ) or not isinstance(detail.get("motion_options"), dict) or any(
            detail.get("motion_options", {}).get(key) != value
            for key, value in expected["motion_options"].items()
        ):
            raise HomeAssistantError(
                "Scene was uploaded but readback verification failed; "
                "check the bulb before retrying"
            )
        try:
            refreshed = await self._native_scene_call(lambda session: session.list_scenes())
        except HomeAssistantError as exc:
            raise HomeAssistantError(
                f"Scene was uploaded and verified, but refreshing the scene list failed: {exc}"
            ) from exc
        if assigned not in refreshed or not set(handles).issubset(refreshed):
            raise HomeAssistantError(
                "Scene was uploaded but the stored scene list changed unexpectedly; "
                "check the bulb before retrying"
            )
        existing = self.data or {}
        names = {
            handle: existing.get("scene_names", {}).get(handle, scene_fallback_name(handle))
            for handle in refreshed
        }
        names[assigned] = name
        self.async_set_updated_data(
            {**existing, "scene_handles": refreshed, "scene_names": names}
        )
        if activate:
            try:
                await self._native_scene_call(lambda session: session.play_scene(assigned))
            except HomeAssistantError as exc:
                raise HomeAssistantError(
                    f"Scene was saved and verified, but activation failed: {exc}"
                ) from exc

    async def _upload_scene(
        self, name: str, palette: str, effect: str, transition: int, *, activate: bool
    ) -> None:
        handles = await self._scene_call(lambda session: session.list_scenes())
        occupied = set(handles)
        user_ids = occupied.intersection(range(1, 244))
        if len(user_ids) >= _SCENE_LIMIT:
            raise HomeAssistantError("NL45 scene storage is full (12 saved user scenes)")
        scene_id = next(identifier for identifier in range(1, 244) if identifier not in occupied)
        payload = build_simple_scene_tlv(palette, scene_id, effect=effect, transition=transition)
        # Each step has its own session recovery. Readback never retries the write.
        try:
            assigned = await self._scene_call(lambda session: session.add_scene(payload))
        except HomeAssistantError as exc:
            raise HomeAssistantError(
                f"Upload failed or its outcome is unknown: {exc}. "
                "Check the bulb's scene list before retrying"
            ) from exc
        if len(assigned) != 1 or not 1 <= assigned[0] <= 243 or assigned[0] in occupied:
            raise HomeAssistantError(
                "Upload returned an invalid or occupied scene ID; "
                "the scene may have been saved. Check the bulb before retrying"
            )
        expected = _parse_scene_data(payload[:2] + assigned + payload[3:])
        try:
            detail = await self._scene_call(lambda session: session.get_scene(assigned))
        except HomeAssistantError as exc:
            raise HomeAssistantError(
                f"Scene was uploaded but readback failed: {exc}. "
                "Check the bulb before retrying"
            ) from exc
        if not expected or not detail or any(
            detail.get(key) != expected[key] for key in ("metadata", "palette")
        ):
            raise HomeAssistantError(
                "Scene was uploaded but readback verification failed; "
                "check the bulb before retrying"
            )
        try:
            handles = await self._scene_call(lambda session: session.list_scenes())
        except HomeAssistantError as exc:
            raise HomeAssistantError(
                f"Scene was uploaded and verified, but refreshing the scene list failed: {exc}"
            ) from exc
        if assigned[0] not in handles:
            raise HomeAssistantError("Uploaded scene is missing from the bulb's scene list")
        existing = self.data or {}
        scene_handles = [bytes([identifier]) for identifier in handles]
        names = {
            handle: existing.get("scene_names", {}).get(handle, scene_fallback_name(handle))
            for handle in scene_handles
        }
        names[assigned] = name
        self.async_set_updated_data(
            {**existing, "scene_handles": scene_handles, "scene_names": names}
        )
        if activate:
            try:
                await self._scene_call(lambda session: session.play_scene(assigned))
            except HomeAssistantError as exc:
                raise HomeAssistantError(
                    f"Scene was saved and verified, but activation failed: {exc}"
                ) from exc

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

    async def async_select_scenes_path(self) -> Path:
        """Select the catalogue by file presence without blocking the event loop."""
        path: Path = await self.hass.async_add_executor_job(
            _select_scenes_path, Path(self.hass.config.config_dir)
        )
        if path != self.scenes_path:
            self.scenes_path = path
            self._scene_lookup = None
        _LOGGER.debug("Using scene database: %s", path)
        return path

    async def async_reload_scenes(self) -> None:
        """Reload the scene lookup after scenes.json has been updated."""
        scenes_path = await self.async_select_scenes_path()
        self._scene_lookup = await SceneLookup.from_path_async(scenes_path)
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
