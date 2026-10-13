"""Offline checks for shared settings carried through legacy CoAP operations."""

import importlib.util
import json
import struct
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, call

import pytest

from nl_ltpdu import LtpduSession, _create_tlv

COMPONENT_DIRECTORY = Path(__file__).resolve().parents[1] / "custom_components/nanoleaf_lights"


def make_session(model: str | None = "NL67", firmware: str = "4.1.3") -> LtpduSession:
    session = LtpduSession("fd00::1", 5683, None, None, model=model)
    session._firmware_version = firmware
    session.get_ltpdu = AsyncMock()
    session.send_ltpdu = AsyncMock()
    return session


def response(endpoint: str, payload: bytes = b"", status: int = 0) -> bytes:
    return _create_tlv(1, endpoint.encode()) + _create_tlv(3, bytes([status]) + payload)


@pytest.mark.asyncio
async def test_legacy_request_preserves_coap_get_and_full_payload() -> None:
    session = make_session()
    session.get_ltpdu.return_value = response("tm", bytes(range(8)))
    assert await session._request("tm", timeout=3) == bytes(range(8))
    session.get_ltpdu.assert_awaited_once_with(
        _create_tlv(1, b"tm") + _create_tlv(2, b""), timeout=3
    )
    session.send_ltpdu.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_request_preserves_coap_post_and_inner_ci_header() -> None:
    session = make_session()
    payload = _create_tlv(0x0902, b"\x01")
    session.send_ltpdu.return_value = response("ci", payload)
    assert await session._request("ci", _create_tlv(0x0902, b""), method=1) == payload
    session.send_ltpdu.assert_awaited_once_with(
        _create_tlv(1, b"ci") + _create_tlv(2, _create_tlv(0x0902, b"")), timeout=10.0
    )
    session.get_ltpdu.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    b"",
    _create_tlv(1, b"wrong") + _create_tlv(3, b"\x00"),
    _create_tlv(1, b"tm"),
    _create_tlv(1, b"tm") + _create_tlv(3, b""),
    _create_tlv(1, b"tm") + _create_tlv(2, b"\x00"),
    _create_tlv(1, b"tm") + b"\x00\x03\x00\x02\x00",
])
async def test_legacy_request_rejects_malformed_operation_response(data: bytes) -> None:
    session = make_session()
    session.get_ltpdu.return_value = data
    with pytest.raises(RuntimeError):
        await session._request("tm")


@pytest.mark.asyncio
async def test_legacy_request_rejects_operation_error() -> None:
    session = make_session()
    session.get_ltpdu.return_value = response("tm", status=2)
    with pytest.raises(RuntimeError, match="status 0x02"):
        await session._request("tm")


@pytest.mark.asyncio
async def test_legacy_request_rejects_unsupported_method_before_sending() -> None:
    session = make_session()
    with pytest.raises(ValueError, match="GET or POST"):
        await session._request("tm", method=3)
    session.get_ltpdu.assert_not_awaited()
    session.send_ltpdu.assert_not_awaited()


@pytest.mark.asyncio
async def test_device_info_caches_tlv_model_and_firmware_for_settings_profile() -> None:
    session = make_session(model=None, firmware="")
    device_info = (
        b"\x03\x00\x01A"
        + b"\x0b\x00\x0267"
        + b"\xfe\x00\x03\x04\x01\x03"
    )
    session.get_ltpdu.return_value = response("di", device_info)
    info = await session.query_device_info()
    assert info["firmware_version"] == "4.1.3"
    assert info["model_number"] == "NL67"
    assert session._firmware_version == "4.1.3"
    assert session._model == "NL67"


@pytest.mark.asyncio
async def test_device_info_caches_fixed_layout_firmware_and_retains_discovery_model() -> None:
    session = make_session(model="NL45", firmware="")
    device_info = (
        b"HW1".ljust(10, b"\x00") + b"3.0.2".ljust(8, b"\x00")
        + b"TESTSERIAL1" + bytes(range(8))
    )
    session.get_ltpdu.return_value = response("di", device_info)
    info = await session.query_device_info()
    assert info["firmware_version"] == "3.0.2"
    assert info["model_number"] == "NL45"
    assert session._firmware_version == "3.0.2"
    assert session._model == "NL45"


@pytest.mark.asyncio
async def test_shared_clock_settings_use_legacy_get_and_post() -> None:
    session = make_session()
    epoch, zone = 1_800_000_000, -18_000
    session.get_ltpdu.side_effect = [
        response("tm", epoch.to_bytes(8, "big")),
        response("tz", zone.to_bytes(8, "big", signed=True)),
    ]
    assert await session.query_device_time() == {
        "epoch_seconds": epoch, "timezone_offset_seconds": zone
    }
    session.send_ltpdu.side_effect = [response("tm"), response("tz")]
    await session.set_device_time(epoch, zone, timeout=2)
    session.send_ltpdu.assert_has_awaits([
        call(_create_tlv(1, b"tm") + _create_tlv(2, epoch.to_bytes(8, "big")), timeout=2),
        call(_create_tlv(1, b"tz") + _create_tlv(2, zone.to_bytes(8, "big", signed=True)),
             timeout=2),
    ])


@pytest.mark.asyncio
async def test_shared_location_settings_keep_signed_longitude_first() -> None:
    session = make_session()
    location = struct.pack(">ii", -73_986, 40_748)
    session.get_ltpdu.return_value = response("lglt", location)
    assert await session.query_device_location() == {
        "latitude": 40.748, "longitude": -73.986
    }
    session.send_ltpdu.return_value = response("lglt")
    await session.set_device_location(40.748, -73.986)
    session.send_ltpdu.assert_awaited_once_with(
        _create_tlv(1, b"lglt") + _create_tlv(2, location), timeout=10.0
    )


@pytest.mark.asyncio
async def test_shared_legacy_plr_uses_ci_command_ids() -> None:
    session = make_session()
    session.send_ltpdu.return_value = response("ci", _create_tlv(0x8902, b"\x01"))
    assert await session.query_power_loss_recovery() is True
    session.send_ltpdu.assert_awaited_once_with(
        _create_tlv(1, b"ci") + _create_tlv(2, b"\x09\x02\x00\x00"), timeout=10.0
    )
    session.send_ltpdu.reset_mock()
    session.send_ltpdu.return_value = response("ci")
    await session.set_power_loss_recovery(False)
    session.send_ltpdu.assert_awaited_once_with(
        _create_tlv(1, b"ci") + _create_tlv(2, b"\x09\x01\x00\x01\x00"), timeout=10.0
    )
    session.get_ltpdu.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_legacy_circadian_enable_also_turns_on_the_light() -> None:
    session = make_session()
    session.send_ltpdu.side_effect = [response("ac/en"), response("lb/0/oo")]
    await session.set_circadian_enabled(True)
    session.send_ltpdu.assert_has_awaits([
        call(_create_tlv(1, b"ac/en") + _create_tlv(2, b"\x01"), timeout=10.0),
        call(_create_tlv(1, b"lb/0/oo") + _create_tlv(2, b"\x01"), timeout=10.0),
    ])


@pytest.mark.asyncio
async def test_shared_legacy_motion_query_preserves_command_payload() -> None:
    session = make_session(model="NL75K1", firmware="4.0.0")
    plugins = json.dumps(["b3fd723a-aae8-4c99-bf2b-087159e0ef53"]).encode()
    session.send_ltpdu.return_value = response("ci", _create_tlv(0x8107, plugins))
    capabilities = await session.query_motion_capabilities()
    assert [motion["name"] for motion in capabilities["motions"]] == ["fade"]
    assert capabilities["scene_protocol"] == "ciOneD"
    session.send_ltpdu.assert_awaited_once_with(
        _create_tlv(1, b"ci") + _create_tlv(2, b"\x01\x07\x00\x00"), timeout=10.0
    )


@pytest.mark.asyncio
async def test_legacy_essentials_motion_profile_does_not_send_unknown_command() -> None:
    session = make_session()
    capabilities = await session.query_motion_capabilities()
    assert [motion["name"] for motion in capabilities["motions"]] == [
        "fade", "highlight", "random"
    ]
    assert capabilities["motion_source"] == "profile"
    session.send_ltpdu.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_settings_reject_unknown_models_without_sending() -> None:
    session = make_session(model="UNKNOWN")
    with pytest.raises(NotImplementedError, match="location"):
        await session.set_device_location(0, 0)
    session.send_ltpdu.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_settings_reject_invalid_input_without_sending() -> None:
    session = make_session()
    with pytest.raises(ValueError, match="latitude"):
        await session.set_device_location(float("nan"), 0)
    with pytest.raises(ValueError, match="epoch_seconds"):
        await session.set_device_time(-1)
    with pytest.raises(ValueError, match="boolean"):
        await session.set_power_loss_recovery(1)
    session.send_ltpdu.assert_not_awaited()


@pytest.mark.asyncio
async def test_component_namespace_settings_work_without_root_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = COMPONENT_DIRECTORY
    package_name = "_nanoleaf_packaged_settings"
    package = types.ModuleType(package_name)
    package.__path__ = [str(directory)]
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, "nl_ltpdu", None)
    monkeypatch.setitem(sys.modules, "nl_ltpdu_v3", None)
    monkeypatch.delitem(sys.modules, f"{package_name}.nl_ltpdu_v3", raising=False)
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.nl_ltpdu", directory / "nl_ltpdu.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    session = module.LtpduSession("fd00::1", 5683, None, None)
    session.get_ltpdu = AsyncMock(return_value=response(
        "di", b"\x03\x00\x01A\x0b\x00\x0267\xfe\x00\x03\x04\x01\x03"
    ))
    capabilities = await session.query_capabilities()
    assert capabilities["circadian"] is True
    assert session._model == "NL67"
    assert session._firmware_version == "4.1.3"
    assert module._settings_controls().__module__ == f"{package_name}.nl_ltpdu_v3"
    session.get_ltpdu.side_effect = [response("tm", bytes(8)), response("tz", bytes(8))]
    assert await session.query_device_time() == {
        "epoch_seconds": 0, "timezone_offset_seconds": 0
    }
    session.send_ltpdu = AsyncMock(return_value=response("lb/0/ct"))
    await session.set_color_temp(6535)


@pytest.mark.asyncio
@pytest.mark.parametrize("model,firmware,minimum,maximum", [
    ("NL67", "4.1.3", 2127, 6535),
    ("NL45", "3.0.2", 2127, 6535),
    ("NL67", "5.1.45", 2100, 6500),
    ("UNKNOWN", "5.1.45", 2700, 6500),
])
@pytest.mark.parametrize("batched", [False, True])
async def test_legacy_color_temperature_matches_model_profile(
    model: str, firmware: str, minimum: int, maximum: int, batched: bool,
) -> None:
    session = make_session(model=model, firmware=firmware)
    session.send_ltpdu.return_value = response("lb/0/ct")
    for kelvin in (minimum, maximum):
        if batched:
            await session.set_light_state(color_temp=kelvin)
        else:
            await session.set_color_temp(kelvin)
    assert session.send_ltpdu.await_args_list == [
        call(_create_tlv(1, b"lb/0/ct") + _create_tlv(2, kelvin.to_bytes(2, "big")),
             timeout=10.0)
        for kelvin in (minimum, maximum)
    ]
    session.send_ltpdu.reset_mock()
    for kelvin in (minimum - 1, maximum + 1):
        with pytest.raises(ValueError, match=f"{minimum}-{maximum}"):
            if batched:
                await session.set_light_state(color_temp=kelvin)
            else:
                await session.set_color_temp(kelvin)
    session.send_ltpdu.assert_not_awaited()
