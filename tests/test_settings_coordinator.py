"""Offline settings coordination, schedule validation and HA temperature tests."""

# ruff: noqa: ANN001, ANN201, ANN202, F811

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_session_factory import factory, ha_modules  # noqa: F401 -- pytest fixtures


@pytest.fixture
def settings(ha_modules):
    cls = ha_modules.coordinator.NanoleafLtpduCoordinator
    coordinator = cls.__new__(cls)
    coordinator.hass = SimpleNamespace(
        config=SimpleNamespace(
            latitude=59.437,
            longitude=24.753,
            time_zone="Europe/Tallinn",
        )
    )
    coordinator.config_entry = SimpleNamespace(data={"model": "NL67"})
    coordinator.connected = True
    coordinator._settings_updated_at = 0.0
    coordinator._scene_lookup = object()
    coordinator.data = {
        "capabilities": {
            "circadian": True,
            "time": True,
            "location": True,
            "power_loss_recovery": True,
            "motions": True,
        }
    }
    coordinator.async_set_updated_data = lambda data: setattr(coordinator, "data", data)
    coordinator.async_request_refresh = AsyncMock()
    device = SimpleNamespace(
        set_device_time=AsyncMock(),
        set_device_location=AsyncMock(),
        set_circadian_enabled=AsyncMock(),
        set_circadian_config=AsyncMock(),
        set_power_loss_recovery=AsyncMock(),
        query_circadian_settings=AsyncMock(return_value={"enabled": False, "config": None}),
        query_device_time=AsyncMock(
            return_value={"epoch_seconds": 1, "timezone_offset_seconds": 0}
        ),
        query_device_location=AsyncMock(return_value={"latitude": 59.437, "longitude": 24.753}),
        query_power_loss_recovery=AsyncMock(return_value=False),
        query_motion_capabilities=AsyncMock(return_value={"motions": [{"name": "fade"}]}),
        query_light_state=AsyncMock(return_value={"power": True}),
        get_current_scene=AsyncMock(return_value=b""),
        close=AsyncMock(),
    )
    coordinator._session = device
    return coordinator, device


async def test_settings_read_false_is_valid_and_unknown_is_none(settings):
    coordinator, device = settings
    device.query_device_location.side_effect = RuntimeError("unsupported")
    await coordinator._refresh_settings()
    assert coordinator.data["power_loss_recovery"] is False
    assert coordinator.data["circadian"] == {"enabled": False, "config": None}
    assert coordinator.data["location"] is None
    assert coordinator.connected
    assert coordinator.data["motion_capabilities"]["motions"][0]["name"] == "fade"


async def test_settings_gate_does_not_probe_unsupported_features(settings):
    coordinator, device = settings
    coordinator.data["capabilities"] = {}
    await coordinator._refresh_settings()
    device.query_circadian_settings.assert_not_awaited()
    device.query_motion_capabilities.assert_not_awaited()
    assert coordinator.data["circadian"] is None


async def test_poll_keeps_basic_controls_when_optional_settings_rejected(settings):
    coordinator, device = settings
    device.query_circadian_settings.side_effect = RuntimeError("unknown endpoint")
    result = await coordinator._async_update_data()
    assert result["light"] == {"power": True}
    assert result["circadian"] is None
    coordinator.data = result
    await coordinator._async_update_data()
    assert device.query_circadian_settings.await_count == 1
    assert device.query_light_state.await_count == 2


async def test_settings_transport_failure_closes_and_reconnects(settings):
    coordinator, device = settings
    device.query_circadian_settings.side_effect = TimeoutError("gone")
    result = await coordinator._async_update_data()
    assert result["light"] == {"power": True}
    assert result["circadian"] is None
    device.close.assert_awaited_once()
    device.query_device_time.assert_not_awaited()
    assert not coordinator.connected
    assert coordinator._session is None
    assert coordinator._settings_updated_at > 0


async def test_legacy_wrapped_timeout_does_not_continue_on_corrupted_cipher(settings):
    coordinator, device = settings
    device._cipher_corrupted = True
    device.query_circadian_settings.side_effect = RuntimeError("get_ltpdu timed out")
    result = await coordinator._async_update_data()
    assert result["light"] == {"power": True}
    device.query_device_time.assert_not_awaited()
    device.close.assert_awaited_once()


async def test_auto_cct_syncs_before_enabling_and_reads_back(settings):
    coordinator, device = settings
    calls = []
    for method in ("set_device_time", "set_device_location", "set_circadian_enabled"):
        getattr(device, method).side_effect = lambda *args, name=method: calls.append(name)
    device.query_circadian_settings.return_value = {"enabled": True, "config": None}
    await coordinator.async_set_circadian_enabled(True)
    assert calls == ["set_device_time", "set_device_location", "set_circadian_enabled"]
    device.set_device_location.assert_awaited_once_with(59.437, 24.753)
    assert coordinator.data["circadian"]["enabled"] is True
    coordinator.async_request_refresh.assert_awaited_once()


async def test_disabling_circadian_does_not_change_clock_or_location(settings):
    coordinator, device = settings
    await coordinator.async_set_circadian_enabled(False)
    device.set_device_time.assert_not_awaited()
    device.set_device_location.assert_not_awaited()
    device.set_circadian_enabled.assert_awaited_once_with(False)


async def test_sync_clock_and_location_works_when_circadian_is_supported_but_off(settings):
    coordinator, device = settings
    coordinator.data["circadian"] = {"enabled": False, "config": None}
    await coordinator.async_sync_clock_and_location()
    device.set_device_time.assert_awaited_once()
    device.set_device_location.assert_awaited_once_with(59.437, 24.753)
    device.set_circadian_enabled.assert_not_awaited()


@pytest.mark.parametrize("supported,enabled", [(False, False), (True, None)])
async def test_sync_clock_and_location_rejects_unavailable_circadian_before_writes(
    settings, supported, enabled
):
    coordinator, device = settings
    coordinator.data["capabilities"]["circadian"] = supported
    coordinator.data["circadian"] = {"enabled": enabled}
    with pytest.raises(RuntimeError, match=r"circadian|Circadian"):
        await coordinator.async_sync_clock_and_location()
    device.set_device_time.assert_not_awaited()
    device.set_device_location.assert_not_awaited()


@pytest.mark.parametrize(("month", "offset"), [(1, 7200), (7, 10800)])
async def test_clock_timezone_offset_follows_target_date_dst(settings, month, offset):
    coordinator, device = settings
    epoch = int(datetime(2026, month, 15, tzinfo=UTC).timestamp())
    await coordinator.async_set_device_time(epoch_seconds=epoch)
    device.set_device_time.assert_awaited_once_with(epoch, offset)


async def test_config_complete_translation_and_readback(settings):
    coordinator, device = settings
    config = {
        "mode": "sunrise_sunset",
        "daylight_target_kelvin": 6500,
        "evening_target_kelvin": 2100,
        "morning_target_kelvin": 4000,
        "rise_ramp_minutes": 30,
        "fall_ramp_minutes": 60,
        "morning_ramp_minutes": 15,
        "sunrise_offset_minutes": -15,
        "sunset_offset_minutes": 30,
    }
    wire = {
        "mode": "sunriseSunset",
        "daylightTargetK": 6500,
        "eveningTargetK": 2100,
        "morningTargetK": 4000,
        "riseRampMinutes": 30,
        "fallRampMinutes": 60,
        "morningRampMinutes": 15,
        "sunriseOffsetMinutes": -15,
        "sunsetOffsetMinutes": 30,
    }
    device.query_circadian_settings.return_value = {
        "enabled": False,
        "config": {"version": 0, **wire},
    }
    await coordinator.async_set_circadian_config(config)
    device.set_circadian_config.assert_awaited_once_with(wire)
    device.set_device_time.assert_awaited_once()
    device.set_device_location.assert_awaited_once_with(59.437, 24.753)
    assert coordinator.data["circadian"]["config"] == config
    with pytest.raises(RuntimeError, match="all white points"):
        await coordinator.async_set_circadian_config({"mode": "explicit"})
    assert device.set_circadian_config.await_count == 1


async def test_recovery_readback_is_authoritative(settings):
    coordinator, device = settings
    await coordinator.async_set_power_loss_recovery(True)
    device.set_power_loss_recovery.assert_awaited_once_with(True)
    assert coordinator.data["power_loss_recovery"] is False


async def test_motion_action_queries_device_and_updates_sensor_data(settings):
    coordinator, device = settings
    result = await coordinator.async_get_motion_capabilities()
    device.query_motion_capabilities.assert_awaited_once()
    assert coordinator.data["motion_capabilities"] == result


@pytest.mark.parametrize(("firmware", "minimum"), [("5.1.45", 2100), ("4.1.3", 2127)])
async def test_light_white_temperature_bounds_and_write_agree(ha_modules, firmware, minimum):
    device = SimpleNamespace(set_light_state=AsyncMock())

    async def call(action):
        await action(device)

    light = ha_modules.light.NanoleafLtpduLight.__new__(ha_modules.light.NanoleafLtpduLight)
    light.coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(data={"model": "NL67"}),
        data={"device": {"model_number": "NL67", "firmware_version": firmware}},
        async_service_call=call,
        async_request_refresh=AsyncMock(),
    )
    assert light.min_color_temp_kelvin == minimum
    await light.async_turn_on(color_temp_kelvin=2100)
    device.set_light_state.assert_awaited_once_with(on=True, color_temp=minimum)
