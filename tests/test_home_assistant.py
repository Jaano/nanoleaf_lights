"""Offline Home Assistant entities, actions, setup, and configuration flows."""

# Dynamic HA fixtures exercise real service schemas with the installed validator.
# ruff: noqa: ANN001, ANN003, ANN201, ANN202, ANN204

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import probatio as vol
import pytest
import yaml

DIRECTORY = Path(__file__).resolve().parents[1] / "custom_components/nanoleaf_lights"
DOMAIN = "nanoleaf_lights"


@pytest.fixture
def settings(monkeypatch):
    """Import platforms/actions while replacing HA, without opening device sessions."""

    def register(name, **attributes):
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
        return module

    package = register("_nanoleaf_settings_tests")
    package.__path__ = [str(DIRECTORY)]

    class State(Enum):
        LOADED = "loaded"
        NOT_LOADED = "not_loaded"

    class Responses(Enum):
        NONE = "none"
        ONLY = "only"

    class Platform(Enum):
        LIGHT = "light"
        BUTTON = "button"
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"
        SWITCH = "switch"

    class BaseEntity:
        @classmethod
        def __class_getitem__(cls, value) -> type[BaseEntity]:
            return cls

        def __init__(self, coordinator):
            self.coordinator = coordinator

        @property
        def available(self):
            return self.coordinator.last_update_success

    @dataclass(frozen=True)
    class Description:
        key: str
        name: str | None = None
        icon: str | None = None
        entity_category: str | None = None
        translation_key: str | None = None
        device_class: str | None = None
        native_unit_of_measurement: str | None = None

    extractor = AsyncMock(side_effect=lambda call: call.target_entries)
    register("homeassistant")
    register("homeassistant.config_entries", ConfigEntry=object, ConfigEntryState=State)
    register(
        "homeassistant.core",
        HomeAssistant=object,
        ServiceCall=object,
        SupportsResponse=Responses,
        callback=lambda method: method,
    )
    register(
        "homeassistant.const",
        EntityCategory=SimpleNamespace(CONFIG="config", DIAGNOSTIC="diagnostic"),
        Platform=Platform,
    )
    register(
        "homeassistant.exceptions",
        ServiceValidationError=type("ServiceValidationError", (RuntimeError,), {}),
    )
    register("homeassistant.helpers")
    register("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    register("homeassistant.helpers.device_registry", DeviceInfo=dict)
    register("homeassistant.helpers.update_coordinator", CoordinatorEntity=BaseEntity)
    register("homeassistant.helpers.service", async_extract_config_entry_ids=extractor)
    register(
        "homeassistant.helpers.config_validation",
        string=vol.All(str),
        entity_ids_or_uuids=lambda value: [value] if isinstance(value, str) else value,
        config_entry_only_config_schema=lambda domain: {"config_entry_only": domain},
    )
    register("homeassistant.helpers.entity_registry", async_migrate_entries=AsyncMock())
    register("homeassistant.components")
    register("homeassistant.components.switch", SwitchEntity=type("SwitchEntity", (), {}))
    register("homeassistant.components.button", ButtonEntity=type("ButtonEntity", (), {}))
    register(
        "homeassistant.components.sensor",
        SensorEntity=type("SensorEntity", (), {}),
        SensorEntityDescription=Description,
        SensorDeviceClass=SimpleNamespace(TIMESTAMP="timestamp"),
    )
    register(f"{package.__name__}.coordinator", NanoleafLtpduCoordinator=object)
    register(
        f"{package.__name__}.entity",
        NanoleafLtpduEntity=BaseEntity,
        entity_unique_id=lambda entry, suffix: f"{entry.unique_id or entry.entry_id}-{suffix}",
    )
    register(f"{package.__name__}.nl_api", NanoleafCloudApi=object)

    def load(name):
        spec = importlib.util.spec_from_file_location(
            f"{package.__name__}.{name}", DIRECTORY / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        return module

    def load_integration():
        spec = importlib.util.spec_from_file_location(
            package.__name__,
            DIRECTORY / "__init__.py",
            submodule_search_locations=[str(DIRECTORY)],
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        return module

    return SimpleNamespace(
        switch=load("switch"),
        button=load("button"),
        sensor=load("sensor"),
        services=load("services"),
        states=State,
        responses=Responses,
        extractor=extractor,
        load_integration=load_integration,
        load_entity=lambda: load("entity"),
    )


def coordinator(**data):
    return SimpleNamespace(
        config_entry=SimpleNamespace(unique_id="device-e20"),
        last_update_success=True,
        data=data,
        async_set_circadian_enabled=AsyncMock(),
        async_set_power_loss_recovery=AsyncMock(),
        async_sync_clock_and_location=AsyncMock(),
        async_set_circadian_config=AsyncMock(),
        async_set_device_time=AsyncMock(),
        async_set_device_location=AsyncMock(),
        async_get_motion_capabilities=AsyncMock(return_value={"motions": []}),
        async_scene_action=AsyncMock(),
    )


async def test_refresh_button_downloads_to_selected_path_before_reloading(
    settings, monkeypatch, tmp_path
) -> None:
    from nl_api import NanoleafCloudApi

    public_path = tmp_path / "www/nanoleaf-lights/scenes.json"
    runtime = coordinator()
    runtime.scenes_path = tmp_path / ".storage/nanoleaf_lights/scenes.json"
    runtime.async_select_scenes_path = AsyncMock(return_value=public_path)
    runtime.async_reload_scenes = AsyncMock()
    effects = [{"uuid": "police", "name": "Police", "palette": "00ff00"}]

    def build_scenes(path):
        runtime.async_select_scenes_path.assert_awaited_once()
        runtime.async_reload_scenes.assert_not_awaited()
        NanoleafCloudApi._write_scenes(path, effects)

    api = SimpleNamespace(build_scenes=build_scenes)
    monkeypatch.setattr(settings.button, "NanoleafCloudApi", lambda: api)
    button = settings.button.NanoleafRefreshScenesButton(runtime)
    button.hass = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=lambda function, *args: function(*args))
    )

    await button.async_press()

    button.hass.async_add_executor_job.assert_awaited_once_with(api.build_scenes, public_path)
    runtime.async_reload_scenes.assert_awaited_once()
    assert json.loads(public_path.read_text()) == {"effects": effects}
    assert not runtime.scenes_path.exists()


@pytest.mark.asyncio
async def test_switches_expose_cached_state_and_write_only_when_requested(settings):
    runtime = coordinator(
        capabilities={"circadian": True, "power_loss_recovery": True},
        circadian={"enabled": False, "config": {"mode": "explicit"}},
        clock={"epoch_seconds": 1},
        location={"latitude": 59, "longitude": 24},
        power_loss_recovery=True,
    )
    circadian = settings.switch.NanoleafCircadianSwitch(runtime)
    recovery = settings.switch.NanoleafPowerLossRecoverySwitch(runtime)
    assert circadian.available and not circadian.is_on
    assert recovery.available and recovery.is_on
    assert circadian.extra_state_attributes == {
        "configuration": {"mode": "explicit"},
        "clock": {"epoch_seconds": 1},
        "location": {"latitude": 59, "longitude": 24},
    }
    runtime.async_set_circadian_enabled.assert_not_awaited()
    runtime.async_set_power_loss_recovery.assert_not_awaited()
    await circadian.async_turn_on()
    await circadian.async_turn_off()
    await recovery.async_turn_off()
    assert [call.args for call in runtime.async_set_circadian_enabled.await_args_list] == [
        (True,),
        (False,),
    ]
    runtime.async_set_power_loss_recovery.assert_awaited_once_with(False)


@pytest.mark.parametrize(
    "key,value", [("circadian", {"enabled": None}), ("power_loss_recovery", None)]
)
def test_switch_unavailable_for_missing_readback(settings, key, value):
    runtime = coordinator(
        capabilities={"circadian": True, "power_loss_recovery": True}, **{key: value}
    )
    cls = (
        settings.switch.NanoleafCircadianSwitch
        if key == "circadian"
        else settings.switch.NanoleafPowerLossRecoverySwitch
    )
    switch = cls(runtime)
    assert switch.is_on is None
    assert not switch.available


def test_unsupported_switch_and_disconnected_switch_unavailable(settings):
    runtime = coordinator(capabilities={"circadian": False}, circadian={"enabled": True})
    switch = settings.switch.NanoleafCircadianSwitch(runtime)
    assert not switch.available
    runtime.data["capabilities"]["circadian"] = True
    runtime.last_update_success = False
    assert not switch.available


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_sync_button_delegates_to_configured_ha_clock_and_location(settings, enabled):
    runtime = coordinator(
        capabilities={"circadian": True, "time": True, "location": True},
        circadian={"enabled": enabled, "config": None},
    )
    button = settings.button.NanoleafSyncClockLocationButton(runtime)
    assert button.available
    runtime.async_sync_clock_and_location.assert_not_awaited()
    await button.async_press()
    runtime.async_sync_clock_and_location.assert_awaited_once_with()
    runtime.data["capabilities"]["location"] = False
    assert not button.available


@pytest.mark.parametrize(
    "supported,circadian",
    [(False, {"enabled": False}), (True, None), (True, {"enabled": None})],
)
def test_sync_button_unavailable_without_circadian_support_or_readback(
    settings, supported, circadian
):
    runtime = coordinator(
        capabilities={"circadian": supported, "time": True, "location": True},
        circadian=circadian,
    )
    button = settings.button.NanoleafSyncClockLocationButton(runtime)
    assert not button.available
    runtime.async_sync_clock_and_location.assert_not_awaited()


@pytest.mark.asyncio
async def test_circadian_diagnostics_register_disabled_and_use_bulb_readbacks(settings):
    runtime = coordinator(
        capabilities={"time": True, "location": True},
        circadian={"enabled": False},
        clock={"epoch_seconds": 1791968002, "timezone_offset_seconds": 10800},
        location={"latitude": 59.437, "longitude": 24.527},
    )
    entities = []
    await settings.sensor.async_setup_entry(
        SimpleNamespace(), SimpleNamespace(runtime_data=runtime), entities.extend
    )
    sensors = {
        sensor.entity_description.key: sensor
        for sensor in entities
        if getattr(sensor, "entity_description", None)
        and sensor.entity_description.key.startswith("circadian_")
    }
    assert set(sensors) == {
        "circadian_clock", "circadian_timezone_offset", "circadian_latitude", "circadian_longitude"
    }
    assert sensors["circadian_clock"].native_value == datetime(2026, 10, 14, 8, 53, 22, tzinfo=UTC)
    assert sensors["circadian_clock"].entity_description.device_class == "timestamp"
    assert sensors["circadian_timezone_offset"].native_value == "UTC+03:00"
    assert sensors["circadian_latitude"].native_value == 59.437
    assert sensors["circadian_longitude"].native_value == 24.527
    for sensor in sensors.values():
        assert sensor.available
        assert not sensor._attr_entity_registry_enabled_default
        assert sensor.entity_description.entity_category == "diagnostic"
        assert sensor._attr_unique_id == f"device-e20-{sensor.entity_description.key}"
    runtime.data["clock"]["epoch_seconds"] += 60
    assert sensors["circadian_clock"].native_value == datetime(2026, 10, 14, 8, 54, 22, tzinfo=UTC)
    runtime.async_set_device_time.assert_not_awaited()
    runtime.async_set_device_location.assert_not_awaited()
    runtime.async_sync_clock_and_location.assert_not_awaited()


def test_thread_sensors_expose_partial_readbacks_and_unavailable_failed_groups(settings):
    runtime = coordinator(
        thread={"network_name": "Home", "channel": 0, "child": True, "pan_id": None},
        thread_readbacks={"capabilities": None, "role": {"child": True}, "network": {"status": 0}},
    )
    sensors = {
        description.key: settings.sensor.NanoleafDiagnosticSensor(runtime, description)
        for description in settings.sensor._THREAD_SENSORS
    }
    assert sensors["thread_network_name"].available
    assert sensors["thread_network_name"].native_value == "Home"
    assert sensors["thread_channel"].available
    assert sensors["thread_channel"].native_value == 0
    assert sensors["thread_role"].available
    assert sensors["thread_role"].native_value == "child"
    assert not sensors["thread_capabilities"].available
    assert not sensors["thread_pan_id"].available
    runtime.data["thread_readbacks"]["capabilities"] = {"full": True}
    runtime.data["thread"]["full"] = True
    assert sensors["thread_capabilities"].available
    runtime.last_update_success = False
    assert all(not sensor.available for sensor in sensors.values())
    runtime.last_update_success = True
    runtime.data = {"thread": None}
    assert all(not sensor.available for sensor in sensors.values())


@pytest.mark.parametrize("epoch", [0, None, 2**64 - 1])
def test_circadian_clock_unset_or_out_of_range_is_unknown(settings, epoch):
    runtime = coordinator(capabilities={"time": True}, clock={"epoch_seconds": epoch})
    description = settings.sensor._CIRCADIAN_SENSORS[0]
    sensor = settings.sensor.NanoleafDiagnosticSensor(runtime, description)
    assert sensor.available
    assert sensor.native_value is None


@pytest.mark.parametrize(
    "offset,expected", [(0, "UTC+00:00"), (-12600, "UTC-03:30"), (20701, "UTC+05:45:01")]
)
def test_circadian_diagnostics_preserve_zero_coordinates_and_signed_timezone(
    settings, offset, expected
):
    runtime = coordinator(
        capabilities={"time": True, "location": True},
        clock={"timezone_offset_seconds": offset},
        location={"latitude": 0.0, "longitude": -24.527},
    )
    values = {
        description.key: settings.sensor.NanoleafDiagnosticSensor(runtime, description).native_value
        for description in settings.sensor._CIRCADIAN_SENSORS
    }
    assert values["circadian_timezone_offset"] == expected
    assert values["circadian_latitude"] == 0.0
    assert values["circadian_longitude"] == -24.527


def test_circadian_diagnostics_gate_each_readback_independently(settings):
    runtime = coordinator(
        capabilities={"time": True, "location": False},
        clock={"epoch_seconds": 1, "timezone_offset_seconds": 0},
        location={"latitude": 0, "longitude": 0},
    )
    sensors = {
        description.key: settings.sensor.NanoleafDiagnosticSensor(runtime, description)
        for description in settings.sensor._CIRCADIAN_SENSORS
    }
    assert sensors["circadian_clock"].available
    assert not sensors["circadian_latitude"].available
    runtime.data["capabilities"]["location"] = True
    assert sensors["circadian_latitude"].available
    runtime.data["clock"] = None
    assert not sensors["circadian_clock"].available
    assert not sensors["circadian_timezone_offset"].available
    assert sensors["circadian_longitude"].available
    runtime.data["location"] = None
    assert not sensors["circadian_latitude"].available
    assert sensors["circadian_latitude"].native_value is None
    runtime.last_update_success = False
    assert all(not sensor.available for sensor in sensors.values())
    runtime.data = None
    assert all(sensor.native_value is None for sensor in sensors.values())


def test_motion_sensor_explains_supported_animations_and_preserves_options(settings):
    capabilities = {
        "motions": [
            {
                "id": 65536,
                "name": "fade",
                "options": [{"name": "transTime", "minimum": 1, "maximum": 600}],
            },
            {"name": "highlight", "options": []},
            {"name": "random", "options": []},
        ],
        "palette_types": ["HSV"],
        "max_palette_size": 7,
        "max_stored_scenes": 12,
        "supports_static_scenes": False,
    }
    runtime = coordinator(capabilities={"motions": True}, motion_capabilities=capabilities)
    sensor = settings.sensor.NanoleafMotionCapabilitiesSensor(runtime)
    assert sensor.available
    assert sensor.native_value == "Fade, Highlight, Random"
    assert sensor.extra_state_attributes == capabilities
    runtime.data["motion_capabilities"] = None
    assert sensor.native_value is None
    assert not sensor.available


def test_motion_sensor_explains_empty_list(settings):
    runtime = coordinator(capabilities={"motions": True}, motion_capabilities={"motions": []})
    sensor = settings.sensor.NanoleafMotionCapabilitiesSensor(runtime)
    assert sensor.available
    assert sensor.native_value == "No supported animations"


def test_motion_sensor_long_list_keeps_full_details_in_attributes(settings):
    capabilities = {
        "motions": [
            {"name": f"Motion 0x{identifier:08x}", "id": identifier, "options": []}
            for identifier in range(100)
        ]
    }
    runtime = coordinator(capabilities={"motions": True}, motion_capabilities=capabilities)
    sensor = settings.sensor.NanoleafMotionCapabilitiesSensor(runtime)
    assert sensor.available
    assert sensor.native_value == "Supports many animations; see attributes for the full list"
    assert len(sensor.native_value) <= 255
    assert sensor.extra_state_attributes == capabilities


class Registry:
    def __init__(self):
        self.actions = {}
        self.registrations = 0

    def has_service(self, domain, name):
        return (domain, name) in self.actions

    def async_register(self, domain, name, handler, **options):
        self.actions[(domain, name)] = (handler, options)
        self.registrations += 1

    async def invoke(self, hass, name, data, target_entries=()):
        handler, options = self.actions[(DOMAIN, name)]
        data = options["schema"](data)
        call = SimpleNamespace(hass=hass, data=data, target_entries=set(target_entries))
        return await handler(call)


def harness_capabilities():
    return {
        **dict.fromkeys(["circadian", "time", "location", "motions", "scene_upload"], True),
        "scene_effects": ["fade", "random", "highlight"],
    }


def harness(settings):
    registry = Registry()
    loaded = coordinator(capabilities=harness_capabilities())
    other = coordinator(capabilities=harness_capabilities())
    entries = {
        "a": SimpleNamespace(
            entry_id="a", domain=DOMAIN, state=settings.states.LOADED, runtime_data=loaded
        ),
        "b": SimpleNamespace(
            entry_id="b", domain=DOMAIN, state=settings.states.LOADED, runtime_data=other
        ),
        "matter": SimpleNamespace(
            entry_id="matter", domain="matter", state=settings.states.LOADED, runtime_data=object()
        ),
    }
    hass = SimpleNamespace(
        services=registry, config_entries=SimpleNamespace(async_get_entry=entries.get)
    )
    return hass, registry, entries


def explicit_config():
    return {
        "mode": "explicit",
        "daylight_target_kelvin": 6500,
        "evening_target_kelvin": 2700,
        "morning_target_kelvin": 4000,
        "rise_ramp_minutes": 30,
        "fall_ramp_minutes": 60,
        "morning_ramp_minutes": 10,
        "rise_time_minutes": 480,
        "fall_time_minutes": 1200,
    }


@pytest.mark.asyncio
async def test_actions_registered_once_and_no_connection_during_registration(settings):
    hass, registry, entries = harness(settings)
    await settings.services.async_setup_services(hass)
    await settings.services.async_setup_services(hass)
    assert registry.registrations == 6
    getter = registry.actions[(DOMAIN, "get_motion_capabilities")][1]
    assert getter["supports_response"] is settings.responses.ONLY
    for entry in [entries["a"], entries["b"]]:
        entry.runtime_data.async_set_device_time.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize("action,upload", [("preview_scene", False), ("upload_scene", True)])
async def test_scene_actions_target_integration_and_preserve_safe_defaults(settings, action, upload):
    hass, registry, entries = harness(settings)
    await settings.services.async_setup_services(hass)
    await registry.invoke(hass, action, {"scene": "police", "entity_id": "light.bulb"}, ["a"])
    expected = {"upload": upload}
    if upload:
        expected["activate"] = False
    entries["a"].runtime_data.async_scene_action.assert_awaited_once_with(
        "police", "fade", 24, direction="left", segment=50, **expected
    )
    entries["b"].runtime_data.async_scene_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["preview_scene", "upload_scene"])
async def test_native_zero_transition_validates_all_targets_before_writing(settings, action):
    hass, registry, entries = harness(settings)
    entries["b"].runtime_data.data["capabilities"]["scene_protocol"] = "effectV2"
    await settings.services.async_setup_services(hass)
    with pytest.raises(RuntimeError, match="transition of at least 1"):
        await registry.invoke(hass, action, {"scene": "police", "transition": 0}, ["a", "b"])
    for identifier in ("a", "b"):
        entries[identifier].runtime_data.async_scene_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_zero_transition_remains_valid(settings):
    hass, registry, entries = harness(settings)
    await settings.services.async_setup_services(hass)
    await registry.invoke(hass, "preview_scene", {"scene": "police", "transition": 0}, ["a"])
    entries["a"].runtime_data.async_scene_action.assert_awaited_once_with(
        "police", "fade", 0, upload=False, direction="left", segment=50
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    {"scene": ""},
    {"scene": "police", "transition": -1},
    {"scene": "police", "transition": 256},
    {"scene": "police", "transition": 1.5},
    {"scene": "police", "effect": "sparkle"},
    {"scene": "police", "direction": "sideways"},
    {"scene": "police", "segment": -1},
    {"scene": "police", "segment": 256},
    {"scene": "police", "segment": 1.5},
    {"scene": "police", "activate": "false"},
])
async def test_scene_action_schema_rejects_invalid_values(settings, data):
    hass, registry, entries = harness(settings)
    await settings.services.async_setup_services(hass)
    with pytest.raises(vol.Invalid):
        await registry.invoke(hass, "upload_scene", data, ["a"])
    entries["a"].runtime_data.async_scene_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_scene_actions_validate_all_targets_before_writing(settings):
    hass, registry, entries = harness(settings)
    entries["b"].runtime_data.data["capabilities"]["scene_upload"] = False
    await settings.services.async_setup_services(hass)
    with pytest.raises(RuntimeError, match="does not support scene_upload"):
        await registry.invoke(hass, "upload_scene", {"scene": "police"}, ["a", "b"])
    entries["a"].runtime_data.async_scene_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_circadian_action_passes_complete_config_only_to_integration_targets(settings):
    hass, registry, entries = harness(settings)
    await settings.services.async_setup_services(hass)
    config = explicit_config()
    await registry.invoke(
        hass, "set_circadian_config", {**config, "entity_id": "light.bulb"}, ["a", "matter"]
    )
    entries["a"].runtime_data.async_set_circadian_config.assert_awaited_once_with(config)
    entries["b"].runtime_data.async_set_circadian_config.assert_not_awaited()
    settings.extractor.assert_awaited_once()
    assert settings.extractor.await_args.args[0].data["entity_id"] == ["light.bulb"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"rise_time_minutes": None},
        {"sunset_offset_minutes": 0},
        {"rise_ramp_minutes": -1},
        {"morning_target_kelvin": 0},
    ],
)
async def test_invalid_full_config_rejected_before_device_io(settings, mutation):
    hass, registry, entries = harness(settings)
    await settings.services.async_setup_services(hass)
    data = {**explicit_config(), **mutation, "config_entry_id": "a"}
    if mutation.get("rise_time_minutes", 1) is None:
        data.pop("rise_time_minutes")
    with pytest.raises(vol.Invalid):
        await registry.invoke(hass, "set_circadian_config", data)
    entries["a"].runtime_data.async_set_circadian_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_sunrise_mode_requires_explicit_offsets_without_stored_defaults(settings):
    hass, registry, entries = harness(settings)
    await settings.services.async_setup_services(hass)
    data = explicit_config()
    data.pop("rise_time_minutes")
    data.pop("fall_time_minutes")
    data.update(mode="sunrise_sunset", sunrise_offset_minutes=-15, sunset_offset_minutes=30)
    await registry.invoke(hass, "set_circadian_config", {**data, "config_entry_id": "a"})
    entries["a"].runtime_data.async_set_circadian_config.assert_awaited_once_with(data)


@pytest.mark.asyncio
async def test_circadian_action_accepts_legacy_whitepoint(settings):
    hass, registry, entries = harness(settings)
    await settings.services.async_setup_services(hass)
    data = {**explicit_config(), "daylight_target_kelvin": 6535}
    await registry.invoke(hass, "set_circadian_config", {**data, "config_entry_id": "a"})
    entries["a"].runtime_data.async_set_circadian_config.assert_awaited_once_with(data)


def test_schedule_error_does_not_disable_circadian_or_sync(settings):
    runtime = coordinator(
        capabilities={"circadian": True, "time": True, "location": True},
        circadian={"enabled": False, "config": None, "config_error": "read status 2"},
    )
    switch = settings.switch.NanoleafCircadianSwitch(runtime)
    button = settings.button.NanoleafSyncClockLocationButton(runtime)
    assert switch.available
    assert not switch.is_on
    assert switch.extra_state_attributes["configuration_error"] == "read status 2"
    assert button.available


@pytest.mark.asyncio
async def test_optional_time_defaults_delegated_and_location_fields_forwarded(settings):
    hass, registry, entries = harness(settings)
    await settings.services.async_setup_services(hass)
    await registry.invoke(hass, "set_device_time", {"config_entry_id": "a"})
    runtime = entries["a"].runtime_data
    runtime.async_set_device_time.assert_awaited_once_with(
        epoch_seconds=None, timezone_offset_seconds=None
    )
    await registry.invoke(
        hass,
        "set_device_location",
        {"config_entry_id": "a", "latitude": 59.437, "longitude": 24.753},
    )
    runtime.async_set_device_location.assert_awaited_once_with(latitude=59.437, longitude=24.753)


@pytest.mark.asyncio
async def test_all_targets_validated_before_any_write(settings):
    hass, registry, entries = harness(settings)
    entries["b"].state = settings.states.NOT_LOADED
    await settings.services.async_setup_services(hass)
    with pytest.raises(RuntimeError, match="not loaded"):
        await registry.invoke(hass, "set_device_time", {"device_id": "shared-device"}, ["a", "b"])
    entries["a"].runtime_data.async_set_device_time.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_and_unsupported_targets_fail_without_write(settings):
    hass, registry, entries = harness(settings)
    await settings.services.async_setup_services(hass)
    with pytest.raises(RuntimeError, match="Select an Enhanced"):
        await registry.invoke(hass, "set_device_time", {"device_id": "matter-device"}, ["matter"])
    entries["a"].runtime_data.data["capabilities"]["time"] = False
    with pytest.raises(RuntimeError, match="does not support"):
        await registry.invoke(hass, "set_device_time", {"config_entry_id": "a"})
    entries["a"].runtime_data.async_set_device_time.assert_not_awaited()


@pytest.mark.asyncio
async def test_getter_returns_capabilities_for_requested_devices_only(settings):
    hass, registry, entries = harness(settings)
    await settings.services.async_setup_services(hass)
    first = {"motions": [{"name": "fade"}], "max_palette_size": 7}
    second = {"motions": [{"name": "flow"}], "max_palette_size": 10}
    entries["a"].runtime_data.async_get_motion_capabilities.return_value = first
    entries["b"].runtime_data.async_get_motion_capabilities.return_value = second
    result = await registry.invoke(
        hass, "get_motion_capabilities", {"device_id": ["a", "b"]}, ["a", "b", "matter"]
    )
    assert result == {"devices": {"a": first, "b": second}}


def test_service_ui_metadata_and_translations_cover_all_action_fields():
    services = yaml.safe_load((DIRECTORY / "services.yaml").read_text())
    assert set(services) == {
        "set_circadian_config",
        "set_device_time",
        "set_device_location",
        "get_motion_capabilities",
        "preview_scene",
        "upload_scene",
    }
    assert not any(
        "default" in field for action in services.values() for field in action["fields"].values()
    )
    for name in ["strings.json", "translations/en.json", "translations/et.json"]:
        translated = json.loads((DIRECTORY / name).read_text())
        assert set(translated["services"]) == set(services)
        for action, metadata in services.items():
            assert set(translated["services"][action]["fields"]) == set(metadata["fields"])
        assert set(translated["entity"]["switch"]) == {"circadian", "power_loss_recovery"}


@pytest.mark.asyncio
async def test_integration_setup_awaits_action_registration_and_includes_switches(
    settings, monkeypatch
):
    integration = settings.load_integration()
    register_actions = AsyncMock()
    monkeypatch.setattr(integration, "async_setup_services", register_actions)
    hass = SimpleNamespace()
    assert await integration.async_setup(hass, {})
    register_actions.assert_awaited_once_with(hass)
    assert "switch" in [platform.value for platform in integration.PLATFORMS]


@pytest.mark.asyncio
async def test_global_actions_remain_registered_after_entry_unload(settings):
    integration = settings.load_integration()
    hass, registry, entries = harness(settings)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    entries["a"].runtime_data.async_shutdown = AsyncMock()
    assert await integration.async_setup(hass, {})
    assert await integration.async_unload_entry(hass, entries["a"])
    assert len(registry.actions) == 6
    entries["a"].runtime_data.async_shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_uses_own_device_even_with_saved_device_page_options(settings, monkeypatch):
    integration = settings.load_integration()
    entity_module = settings.load_entity()
    entry = SimpleNamespace(
        entry_id="enhanced-entry",
        unique_id="e20",
        data={"name": "Nanoleaf bulb", "model": "NL67"},
        options={"device_link_mode": "companion", "companion_device_id": "matter-device"},
    )
    runtime = SimpleNamespace(
        config_entry=entry,
        async_config_entry_first_refresh=AsyncMock(),
        data={"device": {"serial_number": "SERIAL", "eui64": "0011223344556677"}},
    )
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
        )
    )
    monkeypatch.setattr(integration, "NanoleafLtpduCoordinator", lambda hass, entry: runtime)
    assert await integration.async_setup_entry(hass, entry)
    runtime.async_config_entry_first_refresh.assert_awaited_once()
    assert entry.runtime_data is runtime
    entity = entity_module.NanoleafLtpduEntity(runtime)
    assert entity.device_info["identifiers"] == {(DOMAIN, "e20")}
    assert entity.device_info["serial_number"] == "SERIAL"
    assert getattr(entity, "device_entry", None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("handle", [b"\xfb", b"\x00\x00\x00\xfb"])
async def test_light_reports_and_plays_entire_scene_identifier(ha_modules, handle):
    played = []

    class Device:
        async def play_scene(self, value):
            played.append(value)

    async def call(action):
        await action(Device())

    light = ha_modules.light.NanoleafLtpduLight.__new__(ha_modules.light.NanoleafLtpduLight)
    light.coordinator = SimpleNamespace(
        data={"current_scene": handle, "scene_names": {handle: "Device scene"}},
        async_service_call=call,
    )
    assert light.effect == "Device scene"
    assert light.effect_list == ["Device scene"]
    await light._play_effect("Device scene")
    assert played == [handle]


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied_name", [None, "Essentials-E20"])
async def test_reconfigure_passes_name_and_preserves_it_unless_supplied(
    ha_modules, monkeypatch, supplied_name
):
    calls = []
    session = SimpleNamespace(token=b"old-token", closed=False)

    async def close():
        session.closed = True

    session.close = close

    async def connect(ip, port, **options):
        calls.append((ip, port, options))
        return session

    module = ha_modules.config_flow
    monkeypatch.setattr(module, "_connect_session", connect)
    entry = SimpleNamespace(
        data={
            "ip_address": "::1",
            "port": 12566,
            "model": "NL67",
            "token": b"old-token".hex(),
            "name": "Existing-E20",
        }
    )
    flow = module.NanoleafLtpduConfigFlow()
    flow._get_reconfigure_entry = lambda: entry
    user_input = {"ip_address": "::2", "port": 12566, "model": "NL67"}
    if supplied_name is not None:
        user_input["name"] = supplied_name
    result = await flow.async_step_reconfigure_confirm(user_input)
    assert session.closed
    assert calls == [
        (
            "::2",
            12566,
            {
                "model": "NL67",
                "name": supplied_name or "Existing-E20",
                "pin": None,
                "token": b"old-token",
                "timeout": 10.0,
            },
        )
    ]
    assert result["data"]["name"] == (supplied_name or "Existing-E20")
    assert result["data"]["ip_address"] == "::2"


@pytest.mark.asyncio
async def test_pair_closes_factory_session_when_device_info_query_is_cancelled(
    ha_modules, monkeypatch
):
    session = SimpleNamespace(token=b"new-token", closed=False)

    async def close():
        session.closed = True

    async def query(**kwargs):
        raise asyncio.CancelledError

    session.close = close
    session.query_device_info = query

    async def connect(*args: object, **options):
        assert options["pin"] == "12345678901"
        assert options["name"] == "Essentials-E20"
        return session

    module = ha_modules.config_flow
    monkeypatch.setattr(module, "_connect_session", connect)
    flow = module.NanoleafLtpduConfigFlow()
    flow._discovery = {
        "ip_address": "::1",
        "port": 12566,
        "model": "NL67",
        "name": "Essentials-E20",
    }
    with pytest.raises(asyncio.CancelledError):
        await flow.async_step_pair({"pairing_code": "12345678901"})
    assert session.closed


@pytest.mark.parametrize(("firmware", "minimum"), [("5.1.45", 2100), ("4.1.3", 2127)])
async def test_light_white_temperature_bounds_and_write_agree(ha_modules, firmware, minimum):
    device = SimpleNamespace(set_light_state=AsyncMock())

    async def call(action):
        await action(device)

    async def set_state(**changes):
        await device.set_light_state(**changes)

    light = ha_modules.light.NanoleafLtpduLight.__new__(ha_modules.light.NanoleafLtpduLight)
    light.coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(data={"model": "NL67"}),
        data={"device": {"model_number": "NL67", "firmware_version": firmware}},
        async_service_call=call,
        async_set_light_state=set_state,
        async_request_refresh=AsyncMock(),
    )
    assert light.min_color_temp_kelvin == minimum
    await light.async_turn_on(color_temp_kelvin=2100)
    device.set_light_state.assert_awaited_once_with(on=True, color_temp=minimum)


def _color_light(ha_modules, model: str, *, capabilities=None, light_state=None):
    device = SimpleNamespace(set_light_state=AsyncMock())

    async def call(action):
        await action(device)

    async def set_state(**changes):
        await device.set_light_state(**changes)

    data = {"device": {"model_number": model, "firmware_version": "1.6.49"}}
    if capabilities is not None:
        data["capabilities"] = capabilities
    if light_state is not None:
        data["light"] = light_state
    light = ha_modules.light.NanoleafLtpduLight.__new__(ha_modules.light.NanoleafLtpduLight)
    light._color_mode = "color_temp"
    light.coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(data={"model": model}),
        data=data,
        async_service_call=call,
        async_set_light_state=set_state,
        async_request_refresh=AsyncMock(),
    )
    return light, device


async def test_nl62_light_is_hs_only_and_never_writes_color_temperature(ha_modules):
    light, device = _color_light(
        ha_modules, "NL62", light_state={"power": True, "color_temp": None}
    )
    assert light.supported_color_modes == {"hs"}
    assert light.color_mode == "hs"
    await light.async_turn_on(color_temp_kelvin=2700)
    device.set_light_state.assert_awaited_once_with(on=True, hue=30, saturation=20)
    assert light.color_mode == "hs"


async def test_nl62_explicit_hs_wins_over_converted_color_temperature(ha_modules):
    light, device = _color_light(ha_modules, "NL62")
    await light.async_turn_on(color_temp_kelvin=2700, hs_color=(120.0, 80.0))
    device.set_light_state.assert_awaited_once_with(on=True, hue=120, saturation=80)


async def test_nl62_mode_is_hs_before_the_first_device_read(ha_modules):
    # No capabilities and no light state yet: the configured model decides.
    light, _ = _color_light(ha_modules, "NL62")
    assert light.supported_color_modes == {"hs"}
    assert light.color_mode == "hs"


async def test_color_temperature_capable_light_is_unchanged(ha_modules):
    light, device = _color_light(
        ha_modules, "NL45", light_state={"power": True, "color_temp": 2700}
    )
    assert light.supported_color_modes == {"hs", "color_temp"}
    assert light.color_mode == "color_temp"
    await light.async_turn_on(color_temp_kelvin=3000)
    device.set_light_state.assert_awaited_once_with(on=True, color_temp=3000)


async def test_light_prefers_device_capability_over_configured_model(ha_modules):
    light, _ = _color_light(ha_modules, "NL45", capabilities={"color_temp": False})
    assert light.supported_color_modes == {"hs"}


@pytest.mark.asyncio
@pytest.mark.parametrize("action,upload", [("preview_scene", False), ("upload_scene", True)])
async def test_nl62_flow_and_stripes_options_reach_the_coordinator(settings, action, upload):
    hass, registry, entries = harness(settings)
    entries["a"].runtime_data.data["capabilities"]["scene_effects"] = [
        "fade", "random", "highlight", "flow", "stripes",
    ]
    await settings.services.async_setup_services(hass)
    await registry.invoke(
        hass,
        action,
        {"scene": "police", "effect": "stripes", "direction": "down", "segment": 24},
        ["a"],
    )
    expected = {"upload": upload, "direction": "down", "segment": 24}
    if upload:
        expected["activate"] = False
    entries["a"].runtime_data.async_scene_action.assert_awaited_once_with(
        "police", "stripes", 24, **expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["preview_scene", "upload_scene"])
@pytest.mark.parametrize("effect", ["flow", "stripes"])
async def test_nl62_only_effects_are_rejected_for_every_target_before_writing(
    settings, action, effect
):
    hass, registry, entries = harness(settings)
    # "a" is an NL62-like target; "b" only supports the base animations.
    entries["a"].runtime_data.data["capabilities"]["scene_effects"] = [
        "fade", "random", "highlight", "flow", "stripes",
    ]
    await settings.services.async_setup_services(hass)
    with pytest.raises(RuntimeError, match=f"does not support the {effect} animation"):
        await registry.invoke(hass, action, {"scene": "police", "effect": effect}, ["a", "b"])
    for identifier in ("a", "b"):
        entries[identifier].runtime_data.async_scene_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_scene_effect_missing_from_capabilities_is_rejected(settings):
    hass, registry, entries = harness(settings)
    del entries["a"].runtime_data.data["capabilities"]["scene_effects"]
    await settings.services.async_setup_services(hass)
    with pytest.raises(RuntimeError, match="does not support the fade animation"):
        await registry.invoke(hass, "preview_scene", {"scene": "police"}, ["a"])
    entries["a"].runtime_data.async_scene_action.assert_not_awaited()


# -- Entity unique IDs and their migration ----------------------------------------------


def test_entity_unique_ids_fall_back_to_the_entry_id(settings):
    entity_module = settings.load_entity()
    with_eui = SimpleNamespace(unique_id="0011223344556677", entry_id="entry-1")
    manual_a = SimpleNamespace(unique_id=None, entry_id="entry-2")
    manual_b = SimpleNamespace(unique_id=None, entry_id="entry-3")
    assert entity_module.entity_unique_id(with_eui, "light") == "0011223344556677-light"
    assert entity_module.entity_unique_id(manual_a, "light") == "entry-2-light"
    assert entity_module.entity_unique_id(manual_b, "light") == "entry-3-light"
    assert entity_module.device_key(manual_a) == "entry-2"


@pytest.mark.asyncio
async def test_legacy_none_unique_ids_are_migrated_to_the_entry_id(settings):
    integration = settings.load_integration()
    migrate = integration.er.async_migrate_entries
    migrate.reset_mock()
    hass = SimpleNamespace()
    entry = SimpleNamespace(unique_id=None, entry_id="entry-2")
    await integration._async_migrate_unique_ids(hass, entry)
    migrate.assert_awaited_once()
    assert migrate.await_args.args[:2] == (hass, "entry-2")
    rename = migrate.await_args.args[2]
    assert rename(SimpleNamespace(unique_id="None-light")) == {"new_unique_id": "entry-2-light"}
    assert rename(SimpleNamespace(unique_id="None-power-loss-recovery")) == {
        "new_unique_id": "entry-2-power-loss-recovery"
    }
    assert rename(SimpleNamespace(unique_id="entry-2-light")) is None


@pytest.mark.asyncio
async def test_entries_with_a_device_unique_id_are_not_migrated(settings):
    integration = settings.load_integration()
    migrate = integration.er.async_migrate_entries
    migrate.reset_mock()
    await integration._async_migrate_unique_ids(
        SimpleNamespace(), SimpleNamespace(unique_id="0011223344556677", entry_id="entry-1")
    )
    migrate.assert_not_awaited()


def test_integration_is_config_entry_only(settings):
    integration = settings.load_integration()
    assert integration.CONFIG_SCHEMA == {"config_entry_only": DOMAIN}


# -- Light commands do not wait for a refresh -------------------------------------------


@pytest.mark.asyncio
async def test_turning_on_and_off_publishes_state_without_requesting_a_refresh(ha_modules):
    light, device = _color_light(ha_modules, "NL45", light_state={"power": False})
    await light.async_turn_on(brightness=255)
    device.set_light_state.assert_awaited_once_with(on=True, brightness=100)
    await light.async_turn_off()
    device.set_light_state.assert_awaited_with(on=False)
    light.coordinator.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_playing_an_effect_still_refreshes_the_current_scene(ha_modules):
    light, device = _color_light(ha_modules, "NL45")
    device.play_scene = AsyncMock()
    light.coordinator.data["scene_names"] = {b"\x05": "Sunset"}
    await light.async_turn_on(effect="Sunset")
    device.play_scene.assert_awaited_once_with(b"\x05")
    light.coordinator.async_request_refresh.assert_awaited_once()
