"""Shared offline transport and Home Assistant import fixtures."""

# ruff: noqa: ANN001, ANN003, ANN201, ANN202, ANN204, ANN206

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest


@pytest.fixture
def factory(monkeypatch):
    """Load the factory without importing Home Assistant or network backends."""
    package_name = "_nanoleaf_session_tests"
    package = types.ModuleType(package_name)
    directory = Path(__file__).resolve().parents[1] / "custom_components/nanoleaf_lights"
    package.__path__ = [str(directory)]
    monkeypatch.setitem(sys.modules, package_name, package)
    events = []

    class AuthRejectedError(RuntimeError):
        pass

    def backend(label):
        class Backend:
            connect_error = None
            auth_error = None
            pair_error = None
            close_error = None
            instances: ClassVar[list] = []

            def __init__(self):
                self.token = None
                self.closed = False
                self.instances.append(self)

            @classmethod
            async def connect(cls, ip, port, **options):
                events.append((label, "connect", ip, port, options))
                if cls.connect_error:
                    raise cls.connect_error
                return cls()

            async def auth(self, token, timeout):  # noqa: ASYNC109 -- backend API
                events.append((label, "auth", token, timeout))
                if self.auth_error:
                    raise self.auth_error
                self.token = token

            async def pair(self, pin, timeout):  # noqa: ASYNC109 -- backend API
                events.append((label, "pair", pin, timeout))
                if self.pair_error:
                    raise self.pair_error
                self.token = b"paired-token"
                return self.token

            async def close(self):
                events.append((label, "close"))
                self.closed = True
                if self.close_error:
                    raise self.close_error

        return Backend

    legacy = types.ModuleType(f"{package_name}.nl_ltpdu")
    legacy.AuthRejectedError = AuthRejectedError
    legacy.LtpduSession = backend("legacy")
    modern = types.ModuleType(f"{package_name}.nl_ltpdu_v3")
    modern.LtpduV3Session = backend("v3")
    from nl_ltpdu_v3 import (
        EFFECT_ID_MAX,
        _parse_effect,
        build_effect_v2,
        color_temperature_range,
        device_capabilities,
    )
    modern.EFFECT_ID_MAX = EFFECT_ID_MAX
    modern._parse_effect = _parse_effect
    modern.build_effect_v2 = build_effect_v2
    modern.color_temperature_range = color_temperature_range
    modern.device_capabilities = device_capabilities
    monkeypatch.setitem(sys.modules, legacy.__name__, legacy)
    monkeypatch.setitem(sys.modules, modern.__name__, modern)
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.session", directory / "session.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return SimpleNamespace(
        module=module,
        modern=modern.LtpduV3Session,
        legacy=legacy.LtpduSession,
        rejection=AuthRejectedError,
        events=events,
        package=package_name,
        directory=directory,
    )


@pytest.fixture
def ha_modules(factory, monkeypatch):
    """Provide the small HA surface required for coordinator and light checks."""

    def register(name, **attributes):
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
        return module

    class Coordinator:
        @classmethod
        def __class_getitem__(cls, value):
            return cls

    class Entity:
        pass

    class AbortFlow(Exception):
        """Mirror of homeassistant.data_entry_flow.AbortFlow."""

        def __init__(self, reason):
            super().__init__(reason)
            self.reason = reason

    class ConfigFlow:
        """Minimal HA ConfigFlow; tests set ``existing_entries``."""

        existing_entries: ClassVar[list] = []

        def __init_subclass__(cls, **kwargs):
            pass

        @property
        def context(self):
            if "_context" not in self.__dict__:
                self.__dict__["_context"] = {}
            return self.__dict__["_context"]

        @property
        def unique_id(self):
            return self.context.get("unique_id")

        async def async_set_unique_id(self, unique_id, *, raise_on_progress=True):
            self.context["unique_id"] = unique_id
            self.context["raise_on_progress"] = raise_on_progress

        def _abort_if_unique_id_configured(self, updates=None):
            for entry in self.existing_entries:
                if self.unique_id is not None and entry.unique_id == self.unique_id:
                    if updates:
                        entry.data = {**entry.data, **updates}
                    raise AbortFlow("already_configured")

        def _async_abort_entries_match(self, match_dict):
            for entry in self.existing_entries:
                if all(entry.data.get(key) == value for key, value in match_dict.items()):
                    raise AbortFlow("already_configured")

        def async_create_entry(self, **kwargs):
            return {"type": "create_entry", **kwargs}

        def async_show_form(self, **kwargs):
            return kwargs

        def async_update_reload_and_abort(self, entry, **kwargs):
            return kwargs

    class SchemaKey:
        def __init__(self, value, default=None):
            self.value = value
            self.default = default

    register("homeassistant")
    register(
        "homeassistant.config_entries",
        ConfigEntry=object,
        ConfigFlow=ConfigFlow,
    )
    register("homeassistant.data_entry_flow", AbortFlow=AbortFlow)
    register("homeassistant.core", HomeAssistant=object, callback=lambda method: method)
    register(
        "homeassistant.exceptions",
        # Distinct, as in Home Assistant.
        ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (RuntimeError,), {}),
        HomeAssistantError=RuntimeError,
    )
    register("homeassistant.helpers")
    register(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=Coordinator,
        UpdateFailed=type("UpdateFailed", (RuntimeError,), {}),
    )
    register("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    register("homeassistant.util")
    register("homeassistant.util.color", color_temperature_to_hs=lambda kelvin: (30.0, 20.0))
    register("homeassistant.helpers.device_registry")
    register("homeassistant.helpers.selector")
    register("voluptuous", Schema=lambda mapping: mapping, Optional=SchemaKey, Required=SchemaKey)
    register("homeassistant.components")
    register(
        "homeassistant.components.light",
        ATTR_BRIGHTNESS="brightness",
        ATTR_COLOR_TEMP_KELVIN="color_temp_kelvin",
        ATTR_EFFECT="effect",
        ATTR_HS_COLOR="hs_color",
        ColorMode=SimpleNamespace(HS="hs", COLOR_TEMP="color_temp"),
        LightEntity=Entity,
        LightEntityFeature=SimpleNamespace(EFFECT=1),
    )
    legacy = sys.modules[f"{factory.package}.nl_ltpdu"]
    from nl_ltpdu import (
        SCENE_DIRECTIONS,
        EndpointStatusError,
        _parse_scene_data,
        build_simple_scene_tlv,
        preset_scene_name,
    )
    legacy._parse_scene_data = _parse_scene_data
    legacy.build_simple_scene_tlv = build_simple_scene_tlv
    legacy.EndpointStatusError = EndpointStatusError
    legacy.preset_scene_name = preset_scene_name
    legacy.SCENE_DIRECTIONS = SCENE_DIRECTIONS
    legacy.SceneLookup = object
    legacy.SessionExpiredError = type("SessionExpiredError", (RuntimeError,), {})
    register(
        f"{factory.package}.entity",
        NanoleafLtpduEntity=type("NanoleafEntity", (), {}),
        entity_unique_id=lambda entry, suffix: f"{entry.unique_id or entry.entry_id}-{suffix}",
    )

    def load(name):
        spec = importlib.util.spec_from_file_location(
            f"{factory.package}.{name}", factory.directory / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        return module

    return SimpleNamespace(
        coordinator=load("coordinator"),
        light=load("light"),
        config_flow=load("config_flow"),
        AbortFlow=AbortFlow,
    )
