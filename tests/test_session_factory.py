"""Offline protocol selection and scene compatibility checks; no HA/device I/O."""

# Dynamic fixtures emulate both transport classes and the optional HA runtime.
# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, ANN204, ANN206

import asyncio
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
    from nl_ltpdu_v3 import color_temperature_range, device_capabilities
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


@pytest.mark.asyncio
async def test_modern_authentication_wins_without_legacy_attempt(factory):
    session = await factory.module.connect_session(
        "::1", 12566, model="NL67", name="Bulb-E20", token=b"token", timeout=3
    )
    assert isinstance(session, factory.modern)
    assert session.token == b"token"
    assert factory.events == [
        ("v3", "connect", "::1", 12566, {"model": "NL67", "name": "Bulb-E20", "timeout": 3}),
        ("v3", "auth", b"token", 3),
    ]


@pytest.mark.asyncio
async def test_failed_modern_auth_closes_before_legacy_authentication(factory):
    factory.modern.auth_error = RuntimeError("new handshake unavailable")
    session = await factory.module.connect_session("::1", 5653, token=b"old-token")
    assert isinstance(session, factory.legacy)
    assert session.token == b"old-token"
    assert factory.modern.instances[0].closed
    assert [event[:2] for event in factory.events] == [
        ("v3", "connect"),
        ("v3", "auth"),
        ("v3", "close"),
        ("legacy", "connect"),
        ("legacy", "auth"),
    ]


@pytest.mark.asyncio
async def test_pairing_uses_legacy_after_modern_failure_and_exposes_token(factory):
    factory.modern.pair_error = RuntimeError("pairing unsupported")
    session = await factory.module.connect_session("::1", 5653, pin="1234-567-8901")
    assert isinstance(session, factory.legacy)
    assert session.token == b"paired-token"
    assert factory.modern.instances[0].closed
    assert [event[1] for event in factory.events] == ["connect", "pair", "close", "connect", "pair"]


@pytest.mark.asyncio
async def test_public_probe_falls_back_without_authentication(factory):
    factory.modern.connect_error = RuntimeError("not v3")
    session = await factory.module.connect_session("::1", 5653)
    assert isinstance(session, factory.legacy)
    assert [event[:2] for event in factory.events] == [("v3", "connect"), ("legacy", "connect")]


@pytest.mark.asyncio
async def test_both_failures_are_reported_and_both_sessions_close(factory):
    factory.modern.auth_error = RuntimeError("v3 failure")
    factory.legacy.auth_error = RuntimeError("legacy failure")
    with pytest.raises(RuntimeError, match="LTPDUv3: v3 failure; legacy CoAP: legacy failure"):
        await factory.module.connect_session("::1", 12566, token=b"token")
    assert factory.modern.instances[0].closed
    assert factory.legacy.instances[0].closed


@pytest.mark.asyncio
async def test_token_rejection_survives_transport_failure_for_ha_reauth(factory):
    factory.modern.auth_error = factory.rejection("expired token")
    factory.legacy.connect_error = RuntimeError("KEX timed out")
    with pytest.raises(factory.rejection, match=r"expired token.*KEX timed out"):
        await factory.module.connect_session("::1", 12566, token=b"token")
    assert factory.modern.instances[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["auth", "pair"])
async def test_cancellation_closes_session_and_never_falls_back(factory, operation):
    setattr(factory.modern, f"{operation}_error", asyncio.CancelledError())
    arguments = {"token": b"token"} if operation == "auth" else {"pin": "12345678901"}
    with pytest.raises(asyncio.CancelledError):
        await factory.module.connect_session("::1", 12566, **arguments)
    assert factory.modern.instances[0].closed
    assert not factory.legacy.instances


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_prevent_fallback(factory):
    factory.modern.auth_error = RuntimeError("auth failed")
    factory.modern.close_error = RuntimeError("already closed")
    session = await factory.module.connect_session("::1", 12566, token=b"token")
    assert isinstance(session, factory.legacy)


@pytest.mark.asyncio
async def test_ambiguous_credentials_rejected_before_network_work(factory):
    with pytest.raises(ValueError):
        await factory.module.connect_session("::1", 12566, token=b"token", pin="12345678901")
    assert not factory.events


def test_scene_handle_conversion_preserves_v3_leading_zero_bytes(factory):
    handle = bytes.fromhex("000000fb")
    assert factory.module.scene_handle_bytes(handle) == handle
    assert factory.module.scene_handle_bytes(251) == b"\xfb"
    assert factory.module.scene_fallback_name(handle) == "Scene 0x000000fb"


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

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            pass

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
    register("homeassistant.core", HomeAssistant=object, callback=lambda method: method)
    register(
        "homeassistant.exceptions",
        ConfigEntryAuthFailed=RuntimeError,
        HomeAssistantError=RuntimeError,
    )
    register("homeassistant.helpers")
    register(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=Coordinator,
        UpdateFailed=RuntimeError,
    )
    register("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
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
    legacy.SceneLookup = object
    legacy.SessionExpiredError = type("SessionExpiredError", (RuntimeError,), {})
    register(f"{factory.package}.entity", NanoleafLtpduEntity=type("NanoleafEntity", (), {}))

    def load(name):
        spec = importlib.util.spec_from_file_location(
            f"{factory.package}.{name}", factory.directory / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        return module

    return SimpleNamespace(
        coordinator=load("coordinator"), light=load("light"), config_flow=load("config_flow")
    )


@pytest.mark.asyncio
async def test_coordinator_keeps_opaque_ids_and_prefers_device_names(ha_modules):
    requested = []

    class Device:
        async def query_device_info(self, **kwargs):
            return {"model": "NL67"}

        async def query_thread_capabilities(self, **kwargs):
            return {}

        query_thread_role = query_thread_capabilities
        query_thread_network_info = query_thread_capabilities

        async def list_scenes(self, **kwargs):
            return [17, b"\x00\x00\x00\x11"]

        async def get_scene(self, handle, **kwargs):
            requested.append(handle)
            return {"name": f"Bulb {handle.hex()}", "palette": "ff0000"}

    coordinator = ha_modules.coordinator.NanoleafLtpduCoordinator.__new__(
        ha_modules.coordinator.NanoleafLtpduCoordinator
    )
    coordinator._session = Device()
    coordinator._scene_lookup = SimpleNamespace(
        resolve=lambda palette: ("Cloud palette name", "id")
    )
    coordinator.data = None
    coordinator.config_entry = SimpleNamespace(data={"model": "NL67"})
    coordinator.async_set_updated_data = lambda data: setattr(coordinator, "data", data)
    await coordinator._refresh_static_data()
    assert requested == [b"\x11", b"\x00\x00\x00\x11"]
    assert coordinator.data["scene_names"] == {
        b"\x11": "Bulb 11",
        b"\x00\x00\x00\x11": "Bulb 00000011",
    }


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
async def test_real_task_cancellation_during_auth_closes_without_fallback(factory, monkeypatch):
    started = asyncio.Event()

    async def pending_auth(self, token, timeout):  # noqa: ASYNC109 -- backend API
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(factory.modern, "auth", pending_auth)
    task = asyncio.create_task(factory.module.connect_session("::1", 12566, token=b"token"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert factory.modern.instances[0].closed
    assert not factory.legacy.instances


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

    async def connect(*args, **options):
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
