"""Offline effectV2 and light control tests."""

# ruff: noqa: ASYNC109 -- fake session preserves the production timeout API.

from __future__ import annotations

from typing import Any

import pytest

from nl_ltpdu_v3 import V3Controls, parse_effects


def tlv(tag: int, data: bytes) -> bytes:
    return bytes([tag]) + len(data).to_bytes(2, "big") + data


SCENE_ID = bytes.fromhex("01020304")


def scene_packet(kind: int = 0, colors: bytes = bytes.fromhex("0000646400f06464")) -> bytes:
    options = b"".join(
        tlv(1, tlv(2, bytes([code])) + tlv(3, value))
        for code, value in (
            (1, bytes.fromhex("012c")),
            (2, b"\x00"),
            (4, b"left"),
            (9, b"\x05"),
            (12, tlv(2, bytes.fromhex("0020"))),
            (31, b"\xaa\xbb"),
        )
    )
    effect = b"".join(
        (
            tlv(3, SCENE_ID),
            tlv(4, bytes.fromhex("00010000")),
            tlv(5, "Aurora 🌌".encode()),
            tlv(6, tlv(1, bytes([kind])) + tlv(2, colors)),
            tlv(7, options),
            tlv(9, tlv(1, b"\x01") + tlv(2, bytes.fromhex("fe0102ff030405")) + tlv(3, b"\x03")),
            tlv(11, b"\x00\xff"),
            tlv(12, bytes.fromhex("000003e8")),
        )
    )
    return tlv(1, tlv(2, effect))


class FakeSession(V3Controls):
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, bytes, int, float]] = []

    async def _request(
        self, endpoint: str, payload: bytes = b"", method: int = 0, timeout: float = 10.0
    ) -> bytes:
        self.requests.append((endpoint, payload, method, timeout))
        return self.responses.get(endpoint, b"")


def test_full_effect_v2_parser() -> None:
    (effect,) = parse_effects(scene_packet())
    assert effect["id"] == SCENE_ID
    assert effect["effect_id"] == 0x01020304
    assert effect["name"] == "Aurora 🌌"
    assert effect["motion_id"] == 65536
    assert effect["motion_name"] == "fade"
    assert effect["fixture_ids"] == [0, 255]
    assert effect["duration"] == 1000
    assert effect["palette"] == "ff00000000ff"
    assert effect["palette_data"] == {
        "type": "HSV",
        "colors": [
            {"h": 0, "s": 100, "v": 100},
            {"h": 240, "s": 100, "v": 100},
        ],
    }
    assert effect["motion_options"] == {
        "transTime": 300,
        "loop": False,
        "linDirection": "left",
        "blockSize": 5,
        "streamControlGroupIndexing": bytes.fromhex("0200020020"),
        "opt31": b"\xaa\xbb",
    }
    assert effect["static_addressing_type"] == 1
    assert effect["leds_per_zone"] == 3
    assert effect["static_data"] == [
        {"id": 0, "r": 254, "g": 1, "b": 2},
        {"id": 2, "r": 3, "g": 4, "b": 5},
    ]


@pytest.mark.parametrize(
    ("kind", "raw", "parsed", "hex_palette"),
    [
        (
            1,
            bytes.fromhex("0a2864"),
            {"type": "CT", "colors": [{"ct": 2600, "brightness": 100}]},
            "",
        ),
        (
            2,
            bytes.fromhex("123456"),
            {"type": "RGB", "colors": [{"r": 18, "g": 52, "b": 86}]},
            "123456",
        ),
    ],
)
def test_palette_types(kind: int, raw: bytes, parsed: dict[str, Any], hex_palette: str) -> None:
    (effect,) = parse_effects(scene_packet(kind, raw))
    assert effect["palette_data"] == parsed
    assert effect["palette"] == hex_palette


def test_unknown_motion_preserved() -> None:
    payload = tlv(2, tlv(3, SCENE_ID) + tlv(4, bytes.fromhex("deadbeef")) + tlv(5, b"Future"))
    (effect,) = parse_effects(payload)
    assert effect["motion_id"] == 0xDEADBEEF
    assert effect["motion_name"] == ""


@pytest.mark.parametrize(
    "raw",
    [b"\x01", bytes.fromhex("010002aa"), scene_packet()[:-1], scene_packet(0, b"\x00\x00\x64")],
)
def test_rejects_truncated_nested_records(raw: bytes) -> None:
    with pytest.raises(RuntimeError, match="Truncated"):
        parse_effects(raw)


async def test_scene_ids_are_four_bytes_and_selection_is_name() -> None:
    session = FakeSession(
        {
            "effect/ids": tlv(8, SCENE_ID + bytes.fromhex("ffffffff")),
            "effect": scene_packet(),
            "lb/0/effect/select": "Aurora 🌌".encode(),
        }
    )
    assert await session.list_scenes() == [SCENE_ID, b"\xff" * 4]
    scene = await session.get_scene(SCENE_ID)
    assert scene is not None and scene["name"] == "Aurora 🌌"
    assert session.requests[-1] == ("effect", bytes.fromhex("08000401020304"), 0, 10.0)
    await session.play_scene(SCENE_ID, timeout=7.0)
    assert session.requests[-1] == ("lb/0/effect/select", "Aurora 🌌".encode(), 1, 7.0)
    assert await session.get_current_scene() == SCENE_ID


async def test_current_scene_resolves_uncached_name() -> None:
    session = FakeSession(
        {
            "effect/ids": tlv(8, SCENE_ID),
            "effect": scene_packet(),
            "lb/0/effect/select": "Aurora 🌌\x00".encode(),
        }
    )
    assert await session.get_current_scene() == SCENE_ID
    assert [request[0] for request in session.requests] == [
        "lb/0/effect/select",
        "effect/ids",
        "effect",
    ]


async def test_unknown_and_inactive_scenes() -> None:
    session = FakeSession({"effect": tlv(1, b"")})
    assert await session.get_scene(SCENE_ID) is None
    assert await session.get_current_scene() == b""
    with pytest.raises(RuntimeError, match="no name"):
        await session.play_scene(SCENE_ID)
    with pytest.raises(ValueError, match="four bytes"):
        await session.get_scene(b"\x01")


@pytest.mark.parametrize("response", [tlv(8, b"\x01"), tlv(7, SCENE_ID), b"\x08\x00"])
async def test_invalid_scene_ids_fail(response: bytes) -> None:
    with pytest.raises(RuntimeError):
        await FakeSession({"effect/ids": response}).list_scenes()


async def test_scene_cache_removes_deleted_ids() -> None:
    session = FakeSession({"effect": scene_packet(), "effect/ids": tlv(8, b"")})
    await session.get_scene(SCENE_ID)
    assert await session.list_scenes() == []
    assert session._scene_cache() == {}


@pytest.mark.parametrize("firmware", [b"\x05\x01\x2d", b"5.1.45"])
async def test_device_info_firmware_formats(firmware: bytes) -> None:
    payload = b"".join(
        tlv(code, value)
        for code, value in (
            (3, b"1.0\x00"),
            (4, b"N25180B0K50"),
            (6, bytes.fromhex("123456789abcdef0")),
            (10, b"NL67"),
            (11, b"67"),
            (254, firmware),
        )
    )
    info = await FakeSession({"di": payload}).query_device_info()
    assert info["firmware_version"] == "5.1.45"
    assert info["hardware_version"] == "1.0"
    assert info["model_number"] == "NL67"
    assert info["eui64"] == "123456789ABCDEF0"


async def test_light_reads_variable_widths_and_mode() -> None:
    session = FakeSession(
        {
            "lb/0/oo": b"\x00\x01",
            "lb/0/pb": b"\x00\x32",
            "lb/0/hu": b"\x01\x68",
            "lb/0/sa": b"\x00\x64",
            "lb/0/ct": b"\x00\x00\x0a\x28",
            "lb/0/cm": b"\x04",
        }
    )
    assert await session.query_light_state() == {
        "power": True,
        "brightness": 50,
        "hue": 360,
        "saturation": 100,
        "color_temp": 2600,
        "color_mode": "effect",
    }
    assert all(request[2] == 0 for request in session.requests)


async def test_light_writes_and_identify_wire_values() -> None:
    session = FakeSession({})
    await session.set_light_state(on=True, brightness=100, hue=360, saturation=0, color_temp=6500)
    assert session.requests == [
        ("lb/0/oo", b"\x01", 1, 10.0),
        ("lb/0/pb", b"\x00\x64", 1, 10.0),
        ("lb/0/hu", b"\x01\x68", 1, 10.0),
        ("lb/0/sa", b"\x00\x00", 1, 10.0),
        ("lb/0/ct", b"\x19\x64", 1, 10.0),
    ]
    await session.identify()
    assert session.requests[-1] == ("lb/0/id", b"\x01", 1, 10.0)


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"brightness": 101},
        {"hue": 361},
        {"saturation": -1},
        {"color_temp": 1000},
        {"on": True, "brightness": -1},
    ],
)
async def test_invalid_light_values_fail_before_any_write(values: dict[str, Any]) -> None:
    session = FakeSession({})
    with pytest.raises(ValueError):
        await session.set_light_state(**values)
    assert session.requests == []


async def test_thread_dataset_and_flags() -> None:
    session = FakeSession(
        {
            "th/nc": b"\x0c",
            "th/tr": b"\x08",
            "th/ds": bytes.fromhex("000300000f010212340304546573740608fd05000000000000"),
        }
    )
    caps = await session.query_thread_capabilities()
    assert caps["full"] and caps["router_eligible"] and not caps["sleepy"]
    role = await session.query_thread_role()
    assert role["child"] and not role["leader"]
    network = await session.query_thread_network_info()
    assert network["channel"] == 15
    assert network["pan_id"] == "1234"
    assert network["network_name"] == "Test"
    assert network["mesh_local_prefix"] == "fd05000000000000"


@pytest.mark.parametrize(
    "value,expected", list(enumerate(["disabled", "detached", "child", "router", "leader"]))
)
async def test_matter_thread_role_is_enum(value: int, expected: str) -> None:
    session = FakeSession({"th/tr": bytes([value])})
    session._model = "NL67"
    role = await session.query_thread_role()
    assert role["raw"] == value
    assert [key for key, active in role.items() if key != "raw" and active] == [expected]
