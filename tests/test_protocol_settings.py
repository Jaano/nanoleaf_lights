"""Offline optional settings, capability profiles, and CoAP adapter tests."""

# ruff: noqa: ASYNC109

import importlib.util
import json
import struct
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, call

import pytest

from nl_ltpdu import LtpduSession, _create_tlv
from nl_ltpdu_v3 import V3Controls, color_temperature_range, device_capabilities


class SettingsSession(V3Controls):
    def __init__(self, model: str | None = "NL67", firmware: str | None = "5.1.45") -> None:
        self._model = model
        self._firmware_version = firmware
        self.responses: dict[tuple[str, int], bytes] = {}
        self.calls: list[tuple[str, bytes, int, float]] = []

    async def _request(
        self, endpoint: str, payload: bytes = b"", method: int = 0, timeout: float = 10.0
    ) -> bytes:
        self.calls.append((endpoint, payload, method, timeout))
        return self.responses.get((endpoint, method), b"")


def config(mode: str = "explicit") -> dict[str, Any]:
    result: dict[str, Any] = {
        "mode": mode,
        "riseRampMinutes": 30,
        "fallRampMinutes": 45,
        "morningRampMinutes": 20,
        "daylightTargetK": 6500,
        "eveningTargetK": 2700,
        "morningTargetK": 4000,
    }
    if mode == "explicit":
        result.update({"riseTimeOfDayMinutes": 420, "fallTimeOfDayMinutes": 1320})
    else:
        result.update({"sunriseOffsetMinutes": -15, "sunsetOffsetMinutes": 30})
    return result


EXPLICIT = bytes.fromhex(
    "0101000320010201a4020205280302001e0402002d050200140602009a07020172080200fa"
)
SUNRISE = bytes.fromhex(
    "01010002200102fff10202001e0302001e0402002d050200140602009a07020172080200fa"
)


@pytest.mark.parametrize(("mode", "expected"), [("explicit", EXPLICIT), ("sunriseSunset", SUNRISE)])
async def test_circadian_complete_wire_config(mode: str, expected: bytes) -> None:
    session = SettingsSession()
    await session.set_circadian_config(config(mode), timeout=4)
    assert session.calls == [("ac", expected, 1, 4)]


async def test_homekit_config_has_no_version_prefix() -> None:
    session = SettingsSession("NL45", "1.3.17")
    await session.set_circadian_config(config())
    assert session.calls[-1][1] == EXPLICIT[3:]


@pytest.mark.parametrize(
    ("raw", "mode"),
    [
        (EXPLICIT, "explicit"),
        (b"\x00" + EXPLICIT, "explicit"),
        (b"\x00" + EXPLICIT[3:], "explicit"),
        (SUNRISE, "sunriseSunset"),
    ],
)
async def test_circadian_reads_versioned_bare_and_optional_status(raw: bytes, mode: str) -> None:
    session = SettingsSession()
    session.responses = {("lb/0/ac/en", 0): b"\x01", ("ac", 0): raw}
    settings = await session.query_circadian_settings()
    assert settings["enabled"] is True
    parsed = settings["config"]
    assert parsed["mode"] == mode
    assert parsed["riseRampMinutes"] == 30
    assert parsed["daylightTargetK"] == 6493
    assert parsed["eveningTargetK"] == 2702
    if mode == "sunriseSunset":
        assert parsed["sunriseOffsetMinutes"] == -15


async def test_circadian_enable_for_old_firmware_powers_on_after_success() -> None:
    session = SettingsSession("NL67", "4.1.3")
    await session.set_circadian_enabled(True)
    assert session.calls == [("ac/en", b"\x01", 1, 10.0), ("lb/0/oo", b"\x01", 1, 10.0)]
    session.calls.clear()
    await session.set_circadian_enabled(False)
    assert session.calls == [("ac/en", b"\x00", 1, 10.0)]


@pytest.mark.parametrize(("status", "message"), [(2, "clock"), (3, "value"), (5, "read")])
async def test_circadian_status_errors_do_not_power_on(status: int, message: str) -> None:
    session = SettingsSession()
    session.responses[("lb/0/ac/en", 1)] = bytes([status])
    with pytest.raises(RuntimeError, match=message):
        await session.set_circadian_enabled(True)
    assert [call[0] for call in session.calls] == ["lb/0/ac/en"]


async def test_circadian_sunrise_status_has_actionable_failure() -> None:
    session = SettingsSession()
    session.responses[("ac", 1)] = b"\x02"
    with pytest.raises(RuntimeError, match="time, timezone and location"):
        await session.set_circadian_config(config("sunriseSunset"))


async def test_circadian_no_stored_config_is_none() -> None:
    session = SettingsSession()
    session.responses = {("lb/0/ac/en", 0): b"\x00", ("ac", 0): b"\x00"}
    assert await session.query_circadian_settings() == {"config": None, "enabled": False}


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "auto"},
        {"riseRampMinutes": -1},
        {"fallRampMinutes": 65536},
        {"daylightTargetK": 0},
        {"morningTargetK": True},
        {"riseTimeOfDayMinutes": 1440},
        {"sunriseOffsetMinutes": 0},
    ],
)
async def test_invalid_circadian_config_never_writes(change: dict[str, Any]) -> None:
    session = SettingsSession()
    with pytest.raises(ValueError):
        await session.set_circadian_config({**config(), **change})
    assert session.calls == []


async def test_partial_circadian_config_requires_all_fields() -> None:
    session = SettingsSession()
    partial = config("sunriseSunset")
    del partial["sunsetOffsetMinutes"]
    with pytest.raises(ValueError, match="all timing"):
        await session.set_circadian_config(partial)
    assert session.calls == []


async def test_circadian_whitepoints_follow_model_limits() -> None:
    session = SettingsSession()
    await session.set_circadian_config({**config(), "morningTargetK": 1200})
    assert session.calls[-1][1][-2:] == bytes.fromhex("01dc")


async def test_device_clock_big_endian_signed_timezone() -> None:
    session = SettingsSession()
    await session.set_device_time(0x0102030405060708, -7200)
    assert session.calls == [
        ("tm", bytes.fromhex("0102030405060708"), 1, 10.0),
        ("tz", (-7200).to_bytes(8, "big", signed=True), 1, 10.0),
    ]
    session.responses = {
        ("tm", 0): bytes.fromhex("0102030405060708"),
        ("tz", 0): (-7200).to_bytes(8, "big", signed=True),
    }
    assert await session.query_device_time() == {
        "epoch_seconds": 0x0102030405060708,
        "timezone_offset_seconds": -7200,
    }


async def test_clock_omitted_timezone_does_not_set_it() -> None:
    session = SettingsSession()
    await session.set_device_time(1)
    assert [call[0] for call in session.calls] == ["tm"]
    session.calls.clear()
    with pytest.raises(ValueError):
        await session.set_device_time(1, 1 << 63)
    assert session.calls == []


async def test_location_order_scaling_and_javascript_rounding() -> None:
    session = SettingsSession()
    await session.set_device_location(59.437, 24.753)
    assert session.calls == [("lglt", bytes.fromhex("000060b10000e82d"), 1, 10.0)]
    session.responses[("lglt", 0)] = session.calls[-1][1]
    assert await session.query_device_location() == {"latitude": 59.437, "longitude": 24.753}
    await session.set_device_location(-1.2345, -2.3455)
    assert session.calls[-1][1] == (-2345).to_bytes(4, "big", signed=True) + (-1234).to_bytes(
        4, "big", signed=True
    )


@pytest.mark.parametrize(
    ("latitude", "longitude"), [(91, 0), (0, 181), (float("nan"), 0), (0, float("inf")), (True, 0)]
)
async def test_location_validation(latitude: float, longitude: float) -> None:
    session = SettingsSession()
    with pytest.raises(ValueError):
        await session.set_device_location(latitude, longitude)
    assert session.calls == []


async def test_power_loss_v3_state_and_write_encoding() -> None:
    session = SettingsSession()
    session.responses[("lb/0/plr", 0)] = b"\x02\x00"
    assert await session.query_power_loss_recovery() is True
    await session.set_power_loss_recovery(False)
    assert session.calls[-1] == ("lb/0/plr", b"\x01\x00", 1, 10.0)


async def test_legacy_power_loss_inner_commands() -> None:
    session = SettingsSession("NL67", "4.1.3")
    session.responses[("ci", 1)] = bytes.fromhex("8902000101")
    assert await session.query_power_loss_recovery() is True
    assert session.calls[-1][1] == bytes.fromhex("09020000")
    session.responses[("ci", 1)] = bytes.fromhex("8901000100")
    await session.set_power_loss_recovery(True)
    assert session.calls[-1][1] == bytes.fromhex("0901000101")


async def test_motion_capabilities_only_report_returned_known_ids() -> None:
    session = SettingsSession()
    session.responses[("effect/motions", 0)] = bytes.fromhex("0a000c0001000000110000deadbeef")
    capabilities = await session.query_motion_capabilities()
    assert [motion["name"] for motion in capabilities["motions"]] == [
        "fade",
        "intertwine",
        "Motion 0xdeadbeef",
    ]
    assert capabilities["motion_source"] == "device"
    assert capabilities["motions"][-1]["id"] == 0xDEADBEEF
    assert capabilities["motions"][-1]["options"] == []
    assert capabilities["max_palette_size"] == 7
    assert capabilities["max_stored_scenes"] == 12
    fade = capabilities["motions"][0]
    assert fade["options"] == [
        {"name": "transTime", "type": "int", "default": 24, "min": 1, "max": 600}
    ]
    intertwine = capabilities["motions"][1]
    assert intertwine["options"][0]["min"] == 1


async def test_legacy_motions_explicit_baseline_profile() -> None:
    session = SettingsSession("NL45", "1.3.17")
    capabilities = await session.query_motion_capabilities()
    assert [motion["name"] for motion in capabilities["motions"]] == ["fade", "highlight", "random"]
    assert any(option["name"] == "loop" for option in capabilities["motions"][0]["options"])
    assert capabilities["motion_source"] == "profile"
    assert session.calls == []


async def test_one_d_motions_query_known_and_unknown_uuids() -> None:
    session = SettingsSession("NL71K1", "4.0.0")
    plugins = b'["db8ce916-88de-484f-bdbb-afd732053105", "future"]'
    session.responses[("ci", 1)] = b"\x81\x07" + len(plugins).to_bytes(2, "big") + plugins
    result = await session.query_motion_capabilities()
    assert session.calls[-1][1] == b"\x01\x07\x00\x00"
    assert result["motion_source"] == "device"
    assert [motion["name"] for motion in result["motions"]] == ["bounce", "future"]
    assert result["motions"][0]["options"][0]["min"] == 3
    assert result["motions"][1]["uuid"] == "future"


async def test_one_d_malformed_motion_response_never_falls_back() -> None:
    session = SettingsSession("NL71K1", "4.0.0")
    with pytest.raises(RuntimeError, match="Truncated"):
        await session.query_motion_capabilities()


@pytest.mark.parametrize("raw", [b"\x02", b"\x03", b"\x05"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_circadian_read_status_errors_are_preserved(raw: bytes, enabled: bool) -> None:
    session = SettingsSession()
    session.responses = {("lb/0/ac/en", 0): bytes([int(enabled)]), ("ac", 0): raw}
    assert await session.query_circadian_settings() == {
        "enabled": enabled,
        "config": None,
        "config_error": f"Circadian configuration read returned status {raw[0]}",
    }


async def test_circadian_endpoint_failure_keeps_enabled_flag() -> None:
    session = SettingsSession()
    request = session._request

    async def rejected_config(endpoint: str, **kwargs: Any) -> bytes:
        if endpoint == "ac":
            raise RuntimeError("ac returned status 0x02")
        return await request(endpoint, **kwargs)

    session.responses[("lb/0/ac/en", 0)] = b"\x01"
    session._request = rejected_config
    assert await session.query_circadian_settings() == {
        "enabled": True,
        "config": None,
        "config_error": "ac returned status 0x02",
    }


@pytest.mark.parametrize("error", [TimeoutError("gone"), OSError("network lost")])
async def test_circadian_transport_failure_is_not_an_optional_config_error(
    error: Exception,
) -> None:
    session = SettingsSession()

    async def failed_config(endpoint: str, **kwargs: Any) -> bytes:
        if endpoint == "ac":
            raise error
        return b"\x01"

    session._request = failed_config
    with pytest.raises(type(error)):
        await session.query_circadian_settings()


@pytest.mark.parametrize("flag", ["_broken", "_cipher_corrupted"])
async def test_circadian_corrupted_cipher_failure_is_not_suppressed(flag: str) -> None:
    session = SettingsSession()

    async def failed_config(endpoint: str, **kwargs: Any) -> bytes:
        if endpoint == "ac":
            setattr(session, flag, True)
            raise RuntimeError("cipher corrupted")
        return b"\x00"

    session._request = failed_config
    with pytest.raises(RuntimeError, match="cipher corrupted"):
        await session.query_circadian_settings()


@pytest.mark.parametrize("model", ["NL45", "NL67"])
async def test_circadian_legacy_whitepoint_readback_can_be_written_unchanged(model: str) -> None:
    session = SettingsSession(model, "4.1.3")
    stored = {**config(), "daylightTargetK": 6535}
    await session.set_circadian_config(stored)
    packet = session.calls[-1][1]
    session.responses = {
        ("ac/en", 0): b"\x00",
        ("ac", 0): packet,
    }
    readback = (await session.query_circadian_settings())["config"]
    assert readback["daylightTargetK"] == 6535
    await session.set_circadian_config(readback)
    assert session.calls[-1][1] == packet


async def test_circadian_modern_whitepoint_is_clamped_to_model_maximum() -> None:
    session = SettingsSession()
    await session.set_circadian_config({**config(), "daylightTargetK": 6535})
    assert b"\x06\x02\x00\x9a" in session.calls[-1][1]


@pytest.mark.parametrize(
    ("model", "firmware"),
    [("unknown", "5.1.45"), ("NL67", "bad"), ("NL67", "3.4.99"), ("NL62", "4.1.3")],
)
async def test_unsupported_circadian_profiles_send_nothing(model: str, firmware: str) -> None:
    session = SettingsSession(model, firmware)
    with pytest.raises(NotImplementedError):
        await session.set_circadian_enabled(True)
    assert session.calls == []


def test_profiles_follow_firmware_boundaries_and_unknown_conservatism() -> None:
    assert color_temperature_range("NL67", "4.1.3") == (2127, 6535)
    assert color_temperature_range("NL67", "5.1.45") == (2100, 6500)
    assert color_temperature_range("unknown", "5.1.45") == (2700, 6500)
    assert device_capabilities("NL67", "4.1.3")["scene_protocol"] == "ciEssentials"
    assert device_capabilities("NL67", "5.1.45")["circadian"] is True
    assert device_capabilities("NL45", "1.3.16")["circadian"] is False
    assert device_capabilities("NL45", "1.3.17")["circadian"] is True
    assert device_capabilities("unknown", "5.1.45")["motions"] is False
    assert device_capabilities("NL67", "bad")["time"] is False


async def test_device_info_caches_model_and_firmware_for_shared_settings() -> None:
    session = SettingsSession(None, None)
    session.responses[("di", 0)] = bytes.fromhex("0b00023637fe000305012d")
    assert (await session.query_device_info())["model_number"] == "NL67"
    assert session._model == "NL67"
    assert session._firmware_version == "5.1.45"
    capabilities = await session.query_capabilities()
    assert capabilities["circadian"]
    assert [call[0] for call in session.calls] == ["di"]


@pytest.mark.parametrize("model", [b"67", b"NL67", b"nl67"])
async def test_device_info_normalizes_model_prefix(model: bytes) -> None:
    session = SettingsSession(None, None)
    session.responses[("di", 0)] = b"\x0b" + len(model).to_bytes(2, "big") + model
    assert (await session.query_device_info())["model_number"] == "NL67"
    assert session._model == "NL67"


@pytest.mark.parametrize("kelvin", [2100, 6500])
async def test_modern_bulb_accepts_profile_temperature_boundaries(kelvin: int) -> None:
    session = SettingsSession()
    await session.set_color_temp(kelvin)
    assert session.calls == [("lb/0/ct", kelvin.to_bytes(2, "big"), 1, 10.0)]


@pytest.mark.parametrize("kelvin", [2099, 6501])
async def test_modern_bulb_rejects_outside_profile_temperature(kelvin: int) -> None:
    session = SettingsSession()
    with pytest.raises(ValueError):
        await session.set_light_state(on=True, color_temp=kelvin)
    assert session.calls == []


async def test_legacy_bulb_retains_higher_temperature_limit() -> None:
    session = SettingsSession("NL67", "4.1.3")
    await session.set_color_temp(6535)
    assert session.calls[-1] == ("lb/0/ct", (6535).to_bytes(2, "big"), 1, 10.0)
    session.calls.clear()
    with pytest.raises(ValueError):
        await session.set_color_temp(6536)
    assert session.calls == []


@pytest.mark.parametrize(("model", "firmware", "enabled"), [
    ("NL67", "5.0.0", True), ("NL67", "4.1.0", False), ("NL67", None, False),
    ("NL45", "1.6.49", True), ("NL68", "5.1.45", False),
])
def test_scene_upload_gate(model: str, firmware: str | None, enabled: bool) -> None:
    assert device_capabilities(model, firmware)["scene_upload"] is enabled


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
async def test_light_state_tolerates_rejected_color_temperature() -> None:
    session = make_session("NL62", "1.6.49")
    session.get_ltpdu.return_value = (
        response("lb/0/oo", b"\x00\x01") + response("lb/0/pb", b"\x00\x32")
        + response("lb/0/hu", b"\x00\x78") + response("lb/0/sa", b"\x00\x64")
        + response("lb/0/ct", status=4)
    )
    assert await session.query_light_state() == {
        "power": True, "brightness": 50, "hue": 120, "saturation": 100, "color_temp": None,
    }


@pytest.mark.asyncio
async def test_light_state_rejects_required_endpoint_error() -> None:
    session = make_session("NL62", "1.6.49")
    session.get_ltpdu.return_value = (
        response("lb/0/oo", status=4) + response("lb/0/pb", b"\x00\x32")
        + response("lb/0/hu", b"\x00\x78") + response("lb/0/sa", b"\x00\x64")
        + response("lb/0/ct", b"\x00\x00\x0a\x8c")
    )
    with pytest.raises(RuntimeError, match="lb/0/oo.*status 0x04"):
        await session.query_light_state()


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
@pytest.mark.parametrize("model,value,expected", [("NL67", 3, "router"), ("NL45", 8, "child")])
async def test_legacy_thread_role_uses_model_specific_encoding(
    model: str, value: int, expected: str
) -> None:
    session = make_session(model)
    session.get_ltpdu.return_value = response("th/tr", bytes([value]))
    role = await session.query_thread_role()
    assert role["raw"] == value
    assert [key for key, active in role.items() if key != "raw" and active] == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 4])
async def test_homekit_thread_numbers_preserve_logged_value_after_leading_zeroes(
    width: int,
) -> None:
    session = make_session("NL45")
    session.get_ltpdu.side_effect = [
        response("th/nc", (12).to_bytes(width, "big")),
        response("th/tr", (16).to_bytes(width, "big")),
    ]
    caps = await session.query_thread_capabilities()
    role = await session.query_thread_role()
    assert caps["raw"] == 12
    assert caps["full"] and caps["router_eligible"]
    assert not caps["minimal"] and not caps["sleepy"]
    assert role["raw"] == 16 and role["router"] and not role["disabled"]


@pytest.mark.asyncio
async def test_homekit_network_selects_legacy_endpoint_and_decodes_nested_tlv8() -> None:
    session = make_session("NL45")
    raw = bytes.fromhex(
        "022a0104486f6d65020219000302319c04085059b0077aed49a3"
        "0510000102030405060708090a0b0c0d0e0f"
    )
    session.get_ltpdu.return_value = response("th/tc", raw)
    network = await session.query_thread_network_info()
    assert network["network_name"] == "Home"
    assert network["channel"] == 25
    assert network["pan_id"] == "9c31"
    assert network["extended_pan_id"] == "5059b0077aed49a3"
    assert network["network_key"] == "000102030405060708090a0b0c0d0e0f"
    assert network["mesh_local_prefix"] is None
    assert network["pskc"] is None
    session.get_ltpdu.assert_awaited_once_with(
        _create_tlv(1, b"th/tc") + _create_tlv(2, b""), timeout=10.0
    )
    session.send_ltpdu.assert_not_awaited()


@pytest.mark.asyncio
async def test_matter_network_keeps_dataset_endpoint() -> None:
    session = make_session("NL67")
    raw = bytes.fromhex("000300001901029c310304486f6d650708fd465a23f008e644")
    session.get_ltpdu.return_value = response("th/ds", raw)
    network = await session.query_thread_network_info()
    assert network["channel"] == 25
    assert network["network_name"] == "Home"
    assert network["pan_id"] == "9c31"
    assert network["mesh_local_prefix"] == "fd465a23f008e644"
    session.get_ltpdu.assert_awaited_once_with(
        _create_tlv(1, b"th/ds") + _create_tlv(2, b""), timeout=10.0
    )


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
