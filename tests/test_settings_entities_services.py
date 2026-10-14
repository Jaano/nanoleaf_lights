"""Offline settings entities/action tests using a small Home Assistant surface."""

# Dynamic HA fixtures exercise real service schemas with the installed validator.
# ruff: noqa: ANN001, ANN003, ANN201, ANN202, ANN204

from __future__ import annotations

import importlib.util
import json
import sys
import types
from dataclasses import dataclass
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
        name: str
        icon: str
        entity_category: str

    extractor = AsyncMock(side_effect=lambda call: call.target_entries)
    register("homeassistant")
    register("homeassistant.config_entries", ConfigEntry=object, ConfigEntryState=State)
    register(
        "homeassistant.core", HomeAssistant=object, ServiceCall=object, SupportsResponse=Responses
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
    )
    register("homeassistant.components")
    register("homeassistant.components.switch", SwitchEntity=type("SwitchEntity", (), {}))
    register("homeassistant.components.button", ButtonEntity=type("ButtonEntity", (), {}))
    register(
        "homeassistant.components.sensor",
        SensorEntity=type("SensorEntity", (), {}),
        SensorEntityDescription=Description,
    )
    register(f"{package.__name__}.coordinator", NanoleafLtpduCoordinator=object)
    register(f"{package.__name__}.entity", NanoleafLtpduEntity=BaseEntity)
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
    )


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


def test_motion_sensor_exposes_count_and_complete_parsed_options(settings):
    capabilities = {
        "motions": [
            {
                "id": 65536,
                "name": "fade",
                "options": [{"name": "transTime", "minimum": 1, "maximum": 600}],
            }
        ],
        "palette_types": ["HSV"],
        "max_palette_size": 7,
        "max_stored_scenes": 12,
        "supports_static_scenes": False,
    }
    runtime = coordinator(capabilities={"motions": True}, motion_capabilities=capabilities)
    sensor = settings.sensor.NanoleafMotionCapabilitiesSensor(runtime)
    assert sensor.available
    assert sensor.native_value == 1
    assert sensor.extra_state_attributes == capabilities
    runtime.data["motion_capabilities"] = None
    assert sensor.native_value is None
    assert not sensor.available


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


def harness(settings):
    registry = Registry()
    loaded = coordinator(
        capabilities=dict.fromkeys(["circadian", "time", "location", "motions"], True)
    )
    other = coordinator(
        capabilities=dict.fromkeys(["circadian", "time", "location", "motions"], True)
    )
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
    assert registry.registrations == 4
    getter = registry.actions[(DOMAIN, "get_motion_capabilities")][1]
    assert getter["supports_response"] is settings.responses.ONLY
    for entry in [entries["a"], entries["b"]]:
        entry.runtime_data.async_set_device_time.assert_not_awaited()


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
    assert len(registry.actions) == 4
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
        async_refresh=AsyncMock(),
        data={"device": {"serial_number": "SERIAL", "eui64": "0011223344556677"}},
    )
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
        )
    )
    monkeypatch.setattr(integration, "NanoleafLtpduCoordinator", lambda hass, entry: runtime)
    assert await integration.async_setup_entry(hass, entry)
    assert entry.runtime_data is runtime
    entity = entity_module.NanoleafLtpduEntity(runtime)
    assert entity.device_info["identifiers"] == {(DOMAIN, "e20")}
    assert entity.device_info["serial_number"] == "SERIAL"
    assert getattr(entity, "device_entry", None) is None
