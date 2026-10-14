"""Offline settings encoding, validation, status, and capability tests."""

# ruff: noqa: ASYNC109

from typing import Any

import pytest

from nl_ltpdu_v3 import (
    V3Controls,
    color_temperature_range,
    device_capabilities,
)


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
