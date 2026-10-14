"""Offline settings coordination, schedule validation and HA temperature tests."""

# ruff: noqa: ANN001, ANN201, ANN202, F811

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
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
    coordinator._thread_updated_at = 0.0
    coordinator._settings_lock = asyncio.Lock()
    coordinator._scene_lookup = object()
    coordinator.data = {
        "device": {"model_number": "NL67", "firmware_version": "5.1.45"},
        "capabilities": {
            "circadian": True,
            "time": True,
            "location": True,
            "power_loss_recovery": True,
            "motions": True,
        },
    }
    coordinator.async_set_updated_data = lambda data: setattr(coordinator, "data", data)
    coordinator.async_request_refresh = AsyncMock()
    device = SimpleNamespace(
        query_device_info=AsyncMock(
            return_value={
                "model_number": "NL67",
                "firmware_version": "5.1.45",
            }
        ),
        query_thread_capabilities=AsyncMock(return_value={"full": True}),
        query_thread_role=AsyncMock(return_value={"child": True}),
        query_thread_network_info=AsyncMock(return_value={"status": 0, "channel": 15}),
        list_scenes=AsyncMock(return_value=[]),
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


@pytest.mark.parametrize("failed", ["capabilities", "role", "network"])
async def test_thread_groups_fail_independently_and_keep_other_readbacks(settings, failed):
    coordinator, device = settings
    methods = {
        "capabilities": device.query_thread_capabilities,
        "role": device.query_thread_role,
        "network": device.query_thread_network_info,
    }
    methods[failed].side_effect = RuntimeError("endpoint returned status 4")
    await coordinator._refresh_static_data()
    assert coordinator.data["thread_readbacks"][failed] is None
    for group, method in methods.items():
        method.assert_awaited_once()
        if group != failed:
            assert coordinator.data["thread_readbacks"][group] is not None
    device.list_scenes.assert_awaited_once()


async def test_missing_thread_group_retries_after_interval_without_reconnect(
    settings, ha_modules, monkeypatch
):
    coordinator, device = settings
    clock = [100.0]
    monkeypatch.setattr(ha_modules.coordinator, "monotonic", lambda: clock[0])
    device.query_thread_capabilities.side_effect = [RuntimeError("rejected"), {"full": True}]
    await coordinator._refresh_static_data()
    coordinator.data = await coordinator._async_update_data()
    clock[0] = 159.0
    coordinator.data = await coordinator._async_update_data()
    assert device.query_thread_capabilities.await_count == 1
    clock[0] = 160.0
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.data["thread"]["full"] is True
    assert coordinator.data["thread"]["channel"] == 15
    assert device.query_thread_capabilities.await_count == 2
    device.query_thread_role.assert_awaited_once()
    device.query_thread_network_info.assert_awaited_once()
    clock[0] = 220.0
    coordinator.data = await coordinator._async_update_data()
    assert device.query_thread_capabilities.await_count == 2
    device.close.assert_not_awaited()


async def test_thread_retry_timeout_preserves_light_and_partial_readbacks(settings):
    coordinator, device = settings
    coordinator.data["thread_readbacks"] = {"network": {"status": 0, "channel": 15}}
    device.query_thread_capabilities.side_effect = TimeoutError("gone")
    result = await coordinator._async_update_data()
    assert result["light"] == {"power": True}
    assert result["thread"]["channel"] == 15
    assert result["thread_readbacks"]["capabilities"] is None
    device.query_thread_role.assert_not_awaited()
    device.query_circadian_settings.assert_not_awaited()
    device.close.assert_awaited_once()
    assert coordinator._session is None


async def test_thread_reconnect_clears_stale_groups_and_stops_on_broken_cipher(
    settings, ha_modules
):
    coordinator, device = settings
    coordinator.data["thread_readbacks"] = {"network": {"status": 0, "channel": 15}}
    coordinator._session = None
    coordinator._open_session = AsyncMock(return_value=device)
    device._cipher_corrupted = True
    device.query_thread_capabilities.side_effect = RuntimeError("wrapped timeout")
    with pytest.raises(ha_modules.coordinator.UpdateFailed, match="device diagnostics"):
        await coordinator._async_update_data()
    assert coordinator.data["thread"] is None
    assert all(value is None for value in coordinator.data["thread_readbacks"].values())
    device.query_thread_role.assert_not_awaited()
    device.list_scenes.assert_not_awaited()
    device.query_light_state.assert_not_awaited()
    device.close.assert_awaited_once()


async def test_thread_dataset_rejected_status_is_missing_and_retried(settings):
    coordinator, device = settings
    device.query_thread_network_info.return_value = {"status": 4, "network_name": None}
    await coordinator._refresh_thread_data()
    assert coordinator.data["thread_readbacks"]["network"] is None
    assert coordinator.data["thread"]["child"] is True


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


@pytest.mark.parametrize("initial_info", [RuntimeError("temporary di error"), {}])
async def test_failed_metadata_is_retried_without_reconnecting(
    settings, ha_modules, monkeypatch, initial_info
):
    coordinator, device = settings
    clock = [100.0]
    monkeypatch.setattr(ha_modules.coordinator, "monotonic", lambda: clock[0])
    device.query_device_info.side_effect = [
        initial_info,
        {"model_number": "NL67", "firmware_version": "5.1.45"},
    ]
    await coordinator._refresh_static_data()
    coordinator.data = await coordinator._async_update_data()
    assert not coordinator.data["capabilities"]["circadian"]
    assert device.query_device_info.await_count == 1
    device.query_circadian_settings.assert_not_awaited()

    clock[0] = 159.0
    coordinator.data = await coordinator._async_update_data()
    assert device.query_device_info.await_count == 1

    clock[0] = 160.0
    coordinator.data = await coordinator._async_update_data()
    assert device.query_device_info.await_count == 2
    assert coordinator.data["capabilities"]["circadian"]
    assert coordinator.data["circadian"]["enabled"] is False
    assert coordinator.data["motion_capabilities"]["motions"] == [{"name": "fade"}]
    assert coordinator._session is device
    device.close.assert_not_awaited()


async def test_settings_read_preserves_unrelated_updates_while_awaiting(settings):
    coordinator, device = settings
    started, resume = asyncio.Event(), asyncio.Event()

    async def clock(**kwargs: Any):
        started.set()
        await resume.wait()
        return {"epoch_seconds": 42, "timezone_offset_seconds": 0}

    device.query_device_time.side_effect = clock
    poll = asyncio.create_task(coordinator._refresh_settings({"clock"}))
    await started.wait()
    coordinator.async_set_updated_data(
        {
            **coordinator.data,
            "scene_names": {b"\x01": "New name"},
        }
    )
    resume.set()
    await poll
    assert coordinator.data["scene_names"] == {b"\x01": "New name"}
    assert coordinator.data["clock"]["epoch_seconds"] == 42


async def test_motion_action_does_not_get_overwritten_by_inflight_settings_poll(settings):
    coordinator, device = settings
    started, resume = asyncio.Event(), asyncio.Event()
    coordinator.data["motion_capabilities"] = {"motions": [{"name": "old"}]}
    device.query_motion_capabilities.return_value = {"motions": [{"name": "new"}]}

    async def clock(**kwargs: Any):
        started.set()
        await resume.wait()
        return {"epoch_seconds": 42, "timezone_offset_seconds": 0}

    device.query_device_time.side_effect = clock
    poll = asyncio.create_task(coordinator._refresh_settings({"clock"}))
    await started.wait()
    action = asyncio.create_task(coordinator.async_get_motion_capabilities())
    await asyncio.sleep(0)
    device.query_motion_capabilities.assert_not_awaited()
    resume.set()
    _, result = await asyncio.gather(poll, action)
    assert coordinator.data["motion_capabilities"] == result == {"motions": [{"name": "new"}]}


async def test_settings_write_and_readback_are_serialized_with_polling(settings):
    coordinator, device = settings
    started, resume = asyncio.Event(), asyncio.Event()

    async def clock(**kwargs: Any):
        started.set()
        await resume.wait()
        return {"epoch_seconds": 42, "timezone_offset_seconds": 0}

    device.query_device_time.side_effect = clock
    device.query_power_loss_recovery.return_value = True
    poll = asyncio.create_task(coordinator._refresh_settings({"clock"}))
    await started.wait()
    action = asyncio.create_task(coordinator.async_set_power_loss_recovery(True))
    await asyncio.sleep(0)
    device.set_power_loss_recovery.assert_not_awaited()
    resume.set()
    await asyncio.gather(poll, action)
    device.set_power_loss_recovery.assert_awaited_once_with(True)
    assert coordinator.data["power_loss_recovery"] is True


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
