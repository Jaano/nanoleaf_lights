"""Offline NL45 and NL67 scene action tests; no live bulb writes."""

# ruff: noqa: ANN001, ANN003, ANN201, ANN202

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nl_ltpdu import SceneLookup, _parse_scene_data, build_simple_scene_tlv
from nl_ltpdu_v3 import EFFECT_ID_MAX, _parse_effect, build_effect_v2, device_capabilities


def _write_catalogue(path: Path, name: str, palette: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"effects": [
        {"uuid": "police", "name": name, "palette": palette},
    ]}))


@pytest.fixture
def scenes(factory, ha_modules, tmp_path):
    cls = ha_modules.coordinator.NanoleafLtpduCoordinator
    coordinator = cls.__new__(cls)
    coordinator.config_entry = SimpleNamespace(data={"model": "NL45"})
    coordinator.hass = SimpleNamespace(
        config=SimpleNamespace(config_dir=str(tmp_path)),
        async_add_executor_job=AsyncMock(side_effect=lambda function, *args: function(*args))
    )
    coordinator.scenes_path = tmp_path / "www/nanoleaf-lights/scenes.json"
    _write_catalogue(coordinator.scenes_path, "Police 1", "ff00000000000000ff")
    coordinator._scene_lookup = None
    coordinator._scene_lock = asyncio.Lock()
    coordinator.connected = True
    coordinator.data = {
        "capabilities": device_capabilities("NL45", "1.6.49"),
        "scene_names": {},
    }
    coordinator.async_set_updated_data = lambda data: setattr(coordinator, "data", data)
    coordinator.async_request_refresh = AsyncMock()
    saved = {}
    device = factory.legacy()
    device.list_scenes = AsyncMock(side_effect=lambda: bytes(sorted(saved)))
    device.get_scene = AsyncMock(side_effect=lambda handle: _parse_scene_data(saved[handle[0]]))

    async def add(payload):
        await asyncio.sleep(0)
        identifier = payload[2]
        saved[identifier] = payload
        return bytes([identifier])

    device.add_scene = AsyncMock(side_effect=add)
    device.preview_scene = AsyncMock()
    device.play_scene = AsyncMock()
    device.reauth = AsyncMock()
    device.delete_scene = AsyncMock()
    coordinator._session = device
    return SimpleNamespace(coordinator=coordinator, device=device, saved=saved)


def populate(ctx, identifiers):
    for identifier in identifiers:
        ctx.saved[identifier] = build_simple_scene_tlv("00ff00", identifier)


@pytest.mark.parametrize(("public_present", "private_present"), [
    (True, True),
    (True, False),
    (False, True),
    (False, False),
])
async def test_catalogue_path_selection_uses_file_presence(
    scenes, ha_modules, tmp_path, public_present: bool, private_present: bool
) -> None:
    ctx = scenes
    public_path = ctx.coordinator.scenes_path
    private_path = tmp_path / ".storage/nanoleaf_lights/scenes.json"
    if not public_present:
        public_path.unlink()
    if private_present:
        _write_catalogue(private_path, "Private", "00ff00")
    cached = object()
    ctx.coordinator._scene_lookup = cached
    expected = public_path if public_present or not private_present else private_path

    assert await ctx.coordinator.async_select_scenes_path() == expected
    assert ctx.coordinator.scenes_path == expected
    assert ctx.coordinator._scene_lookup is (None if expected != public_path else cached)
    ctx.coordinator.hass.async_add_executor_job.assert_awaited_once_with(
        ha_modules.coordinator._select_scenes_path, tmp_path
    )


@pytest.mark.parametrize("public_present", [True, False])
async def test_first_poll_loads_selected_catalogue_even_when_bulb_is_offline(
    scenes, ha_modules, monkeypatch, tmp_path, public_present: bool
) -> None:
    ctx = scenes
    public_path = ctx.coordinator.scenes_path
    private_path = tmp_path / ".storage/nanoleaf_lights/scenes.json"
    _write_catalogue(private_path, "Private", "00ff00")
    if public_present:
        _write_catalogue(public_path, "Public", "00ff00")
    else:
        public_path.unlink()
    monkeypatch.setattr(ha_modules.coordinator, "SceneLookup", SceneLookup)
    ctx.coordinator._session = None
    ctx.coordinator._open_session = AsyncMock(side_effect=RuntimeError("offline"))

    with pytest.raises(RuntimeError, match="Cannot open session: offline"):
        await ctx.coordinator._async_update_data()

    expected_name = "Public" if public_present else "Private"
    assert ctx.coordinator._scene_lookup.resolve("00ff00") == (expected_name, "police", "exact")
    assert ctx.coordinator.scenes_path == (public_path if public_present else private_path)


async def test_reload_switches_lookup_and_effect_names_to_new_public_catalogue(
    scenes, ha_modules, monkeypatch, tmp_path
) -> None:
    ctx = scenes
    public_path = ctx.coordinator.scenes_path
    public_path.unlink()
    _write_catalogue(tmp_path / ".storage/nanoleaf_lights/scenes.json", "Private", "00ff00")
    monkeypatch.setattr(ha_modules.coordinator, "SceneLookup", SceneLookup)
    populate(ctx, [1])
    ctx.coordinator.data["scene_handles"] = [b"\x01"]
    ctx.device.get_scene.side_effect = (
        lambda handle, **kwargs: _parse_scene_data(ctx.saved[handle[0]])
    )

    await ctx.coordinator.async_reload_scenes()
    assert ctx.coordinator.data["scene_names"] == {b"\x01": "Private"}
    _write_catalogue(public_path, "Public", "00ff00")
    await ctx.coordinator.async_reload_scenes()

    assert ctx.coordinator.scenes_path == public_path
    assert ctx.coordinator.data["scene_names"] == {b"\x01": "Public"}


async def test_scene_action_reselects_public_catalogue_after_it_is_created(
    scenes, tmp_path
) -> None:
    ctx = scenes
    public_path = ctx.coordinator.scenes_path
    public_path.unlink()
    _write_catalogue(tmp_path / ".storage/nanoleaf_lights/scenes.json", "Private", "00ff00")
    await ctx.coordinator.async_scene_action("police", "fade", 24, upload=False)

    _write_catalogue(public_path, "Public", "0000ff")
    await ctx.coordinator.async_scene_action("police", "fade", 24, upload=False)

    assert ctx.coordinator.scenes_path == public_path
    assert ctx.device.preview_scene.await_args_list[0].args == (
        build_simple_scene_tlv("00ff00", 1),
    )
    assert ctx.device.preview_scene.await_args_list[1].args == (
        build_simple_scene_tlv("0000ff", 1),
    )
    assert ctx.device.preview_scene.await_count == 2


async def test_invalid_public_catalogue_does_not_fall_back_to_valid_private_file(
    scenes, tmp_path
) -> None:
    ctx = scenes
    ctx.coordinator.scenes_path.write_text("not JSON")
    _write_catalogue(tmp_path / ".storage/nanoleaf_lights/scenes.json", "Private", "00ff00")

    with pytest.raises(RuntimeError, match="Cannot prepare scene"):
        await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True)

    assert ctx.coordinator.scenes_path == tmp_path / "www/nanoleaf-lights/scenes.json"
    ctx.device.list_scenes.assert_not_awaited()
    ctx.device.add_scene.assert_not_awaited()


@pytest.fixture(params=["scenes", "native_scenes"], ids=["NL45", "NL67"])
def scene_context(request):
    ctx = request.getfixturevalue(request.param)
    native = request.param == "native_scenes"
    ctx.handle = bytes.fromhex("01020304") if native else b"\x01"
    ctx.storage_key = ctx.handle if native else 1
    ctx.empty_handles = [] if native else b""
    ctx.parse = _parse_effect if native else _parse_scene_data
    ctx.populate = lambda identifiers: (populate_native if native else populate)(ctx, identifiers)

    def build_payload(effect="fade", transition=24):
        if native:
            return build_effect_v2(
                "ff00000000ff", 0x01020304, "Police 1", effect=effect, transition=transition
            )
        return build_simple_scene_tlv("ff00000000ff", 1, effect=effect, transition=transition)

    ctx.build_payload = build_payload
    return ctx


@pytest.mark.parametrize("effect", ["fade", "random", "highlight"])
async def test_upload_verifies_motion_and_palette_and_refreshes_effects(scene_context, effect):
    ctx = scene_context
    await ctx.coordinator.async_scene_action("police", effect, 17, upload=True)
    assert ctx.saved == {ctx.storage_key: ctx.build_payload(effect, 17)}
    ctx.device.get_scene.assert_awaited_once_with(ctx.handle)
    ctx.device.play_scene.assert_not_awaited()
    ctx.device.delete_scene.assert_not_awaited()
    assert ctx.coordinator.data["scene_handles"] == [ctx.handle]
    assert ctx.coordinator.data["scene_names"] == {ctx.handle: "Police 1"}
    ctx.coordinator.async_request_refresh.assert_awaited_once()


async def test_upload_allocates_unused_id_and_excludes_builtin_presets(scenes):
    populate(scenes, [1, 2, 4, 244, 251, 254])
    await scenes.coordinator.async_scene_action("police", "fade", 24, upload=True)
    assert scenes.device.add_scene.await_args.args[0][2] == 3
    assert set(scenes.saved) == {1, 2, 3, 4, 244, 251, 254}
    assert scenes.saved[1] == build_simple_scene_tlv("00ff00", 1)


async def test_twelve_user_scenes_reject_upload_but_allow_preview(scenes):
    populate(scenes, [*range(1, 13), *range(244, 255)])
    with pytest.raises(RuntimeError, match="12 saved user scenes"):
        await scenes.coordinator.async_scene_action("police", "fade", 24, upload=True)
    scenes.device.add_scene.assert_not_awaited()
    await scenes.coordinator.async_scene_action("police", "fade", 24, upload=False)
    scenes.device.preview_scene.assert_awaited_once_with(
        build_simple_scene_tlv("ff00000000ff", 1)
    )
    scenes.device.play_scene.assert_not_awaited()


async def test_eleven_user_scenes_plus_all_presets_allow_one_upload(scenes):
    populate(scenes, [*range(1, 12), *range(244, 255)])
    await scenes.coordinator.async_scene_action("police", "fade", 24, upload=True)
    scenes.device.add_scene.assert_awaited_once()
    assert scenes.coordinator.data["scene_names"][b"\x0c"] == "Police 1"


async def test_concurrent_uploads_cannot_exceed_capacity(scene_context):
    ctx = scene_context
    ctx.populate(range(1, 12))
    results = await asyncio.gather(
        ctx.coordinator.async_scene_action("police", "fade", 24, upload=True),
        ctx.coordinator.async_scene_action("police", "random", 24, upload=True),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RuntimeError) for result in results) == 1
    assert len(ctx.saved) == 12
    ctx.device.add_scene.assert_awaited_once()


async def test_activation_follows_verification_and_scene_list_refresh(scene_context):
    ctx = scene_context

    async def play(handle):
        assert ctx.device.get_scene.await_count == 1
        assert ctx.device.list_scenes.await_count == 2
        assert ctx.coordinator.data["scene_names"][handle] == "Police 1"

    ctx.device.play_scene.side_effect = play
    await ctx.coordinator.async_scene_action(
        "police", "fade", 24, upload=True, activate=True
    )
    ctx.device.play_scene.assert_awaited_once_with(ctx.handle)


async def test_activation_failure_reports_that_scene_was_already_saved(scene_context):
    ctx = scene_context
    ctx.device.play_scene.side_effect = RuntimeError("Play rejected")
    with pytest.raises(RuntimeError, match="saved and verified, but activation failed"):
        await ctx.coordinator.async_scene_action(
            "police", "fade", 24, upload=True, activate=True
        )
    ctx.device.add_scene.assert_awaited_once()
    assert ctx.coordinator.data["scene_names"][ctx.handle] == "Police 1"


@pytest.mark.parametrize("failure", ["empty", "timeout"])
async def test_post_upload_listing_failure_never_retries_write(scene_context, failure):
    ctx = scene_context
    response = ctx.empty_handles if failure == "empty" else TimeoutError("List timed out")
    ctx.device.list_scenes.side_effect = [ctx.empty_handles, response]
    with pytest.raises(RuntimeError, match="scene list"):
        await ctx.coordinator.async_scene_action(
            "police", "fade", 24, upload=True, activate=True
        )
    ctx.device.add_scene.assert_awaited_once()
    ctx.device.play_scene.assert_not_awaited()


@pytest.mark.parametrize("field", ["metadata", "palette"])
async def test_readback_mismatch_reports_failure_and_never_activates(scenes, field):
    detail = _parse_scene_data(build_simple_scene_tlv("ff00000000ff", 1))
    detail[field] = b"incorrect" if field == "metadata" else "ffffff"
    scenes.device.get_scene.side_effect = None
    scenes.device.get_scene.return_value = detail
    with pytest.raises(RuntimeError, match="verification failed"):
        await scenes.coordinator.async_scene_action(
            "police", "fade", 24, upload=True, activate=True
        )
    scenes.device.add_scene.assert_awaited_once()
    scenes.device.play_scene.assert_not_awaited()
    scenes.device.delete_scene.assert_not_awaited()


async def test_readback_expiry_reauthenticates_without_reuploading(scene_context, ha_modules):
    ctx = scene_context
    detail = ctx.parse(ctx.build_payload())
    ctx.device.get_scene.side_effect = [
        ha_modules.coordinator.SessionExpiredError("expired"),
        detail,
    ]
    await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True)
    ctx.device.add_scene.assert_awaited_once()
    ctx.device.reauth.assert_awaited_once()
    assert ctx.device.get_scene.await_count == 2


@pytest.mark.parametrize("stage", ["add_scene", "get_scene"])
async def test_transport_failure_is_explicit_and_does_not_retry_upload(scene_context, stage):
    ctx = scene_context
    getattr(ctx.device, stage).side_effect = TimeoutError("Timed out")
    message = "outcome is unknown" if stage == "add_scene" else "uploaded but readback failed"
    with pytest.raises(RuntimeError, match=message) as error:
        await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True)
    assert "before retrying" in str(error.value)
    ctx.device.add_scene.assert_awaited_once()
    ctx.device.play_scene.assert_not_awaited()
    ctx.device.delete_scene.assert_not_awaited()
    if stage == "add_scene":
        ctx.device.get_scene.assert_not_awaited()


@pytest.mark.parametrize("returned", [b"", b"\x01\x02", b"\x00", b"\xfb", b"\x02"])
async def test_invalid_or_occupied_returned_ids_are_not_success(scenes, returned):
    populate(scenes, [2])
    scenes.device.add_scene.side_effect = None
    scenes.device.add_scene.return_value = returned
    with pytest.raises(RuntimeError, match="invalid or occupied"):
        await scenes.coordinator.async_scene_action("police", "fade", 24, upload=True)
    scenes.device.get_scene.assert_not_awaited()


async def test_device_assigned_different_free_id_is_verified(scenes):
    async def add(payload):
        stored = payload[:2] + b"\x05" + payload[3:]
        scenes.saved[5] = stored
        return b"\x05"

    scenes.device.add_scene.side_effect = add
    await scenes.coordinator.async_scene_action("police", "fade", 24, upload=True)
    scenes.device.get_scene.assert_awaited_once_with(b"\x05")
    assert scenes.coordinator.data["scene_names"][b"\x05"] == "Police 1"


@pytest.mark.parametrize("database", [
    {},
    {"effects": []},
    {"effects": [{"uuid": "police", "name": "Bad", "palette": "zzzzzz"}]},
    {"effects": [{"uuid": "police", "name": "Bad", "palette": "000000"}]},
    {"effects": [{"uuid": "police", "name": "Bad", "palette": "ff00"}]},
    {"effects": [{"uuid": "police", "name": "", "palette": "ff0000"}]},
    {"effects": [
        {"uuid": "police", "name": "One", "palette": "ff0000"},
        {"uuid": "police", "name": "Two", "palette": "0000ff"},
    ]},
])
async def test_bad_catalogue_prevents_all_device_io(scenes, database):
    scenes.coordinator.scenes_path.write_text(json.dumps(database))
    with pytest.raises(RuntimeError, match="Cannot prepare scene"):
        await scenes.coordinator.async_scene_action("police", "fade", 24, upload=True)
    scenes.device.list_scenes.assert_not_awaited()
    scenes.device.add_scene.assert_not_awaited()


async def test_missing_database_has_actionable_error(scenes):
    scenes.coordinator.scenes_path.unlink()
    with pytest.raises(RuntimeError, match="Cannot prepare scene"):
        await scenes.coordinator.async_scene_action("police", "fade", 24, upload=False)
    scenes.device.preview_scene.assert_not_awaited()


@pytest.mark.parametrize("model", ["NL55", "UNKNOWN"])
async def test_unsupported_model_is_rejected(scenes, model):
    scenes.coordinator.data["capabilities"] = device_capabilities(model, "5.1.45")
    with pytest.raises(RuntimeError, match="does not support scene_upload"):
        await scenes.coordinator.async_scene_action("police", "fade", 24, upload=True)
    scenes.device.list_scenes.assert_not_awaited()


async def test_modern_session_is_rejected_before_writing(scenes, factory):
    scenes.coordinator._session = factory.modern()
    with pytest.raises(RuntimeError, match="legacy protocol"):
        await scenes.coordinator.async_scene_action("police", "fade", 24, upload=True)
    scenes.device.add_scene.assert_not_awaited()


@pytest.mark.parametrize("transition", [-1, 256, 1.5, True])
async def test_transition_validation_prevents_io(scenes, transition):
    with pytest.raises(RuntimeError, match="integer from 0 to 255"):
        await scenes.coordinator.async_scene_action("police", "fade", transition, upload=True)
    scenes.device.list_scenes.assert_not_awaited()


@pytest.fixture
def native_scenes(scenes, factory, monkeypatch, ha_modules):
    coordinator = scenes.coordinator
    coordinator.config_entry.data["model"] = "NL67"
    coordinator.data["capabilities"] = device_capabilities("NL67", "5.1.45")
    monkeypatch.setattr(ha_modules.coordinator.secrets, "randbelow", lambda bound: 0x01020303)
    saved = {}
    device = factory.modern()
    device.list_scenes = AsyncMock(side_effect=lambda: list(saved))
    device.get_scene = AsyncMock(side_effect=lambda handle: _parse_effect(saved[handle]))
    device.query_motion_capabilities = AsyncMock(return_value={
        "motions": [{"id": identifier} for identifier in (65536, 262144, 327680)],
    })

    async def add(payload):
        await asyncio.sleep(0)
        handle = _parse_effect(payload)["id"]
        assert handle not in saved
        saved[handle] = payload
        return handle

    device.add_scene = AsyncMock(side_effect=add)
    device.preview_scene = AsyncMock()
    device.play_scene = AsyncMock()
    device.reauth = AsyncMock()
    device.delete_scene = AsyncMock()
    coordinator._session = device
    return SimpleNamespace(coordinator=coordinator, device=device, saved=saved)


def populate_native(ctx, identifiers):
    for identifier in identifiers:
        handle = identifier.to_bytes(4, "big")
        payload = build_effect_v2("00ff00", 1, f"Existing {identifier}")
        ctx.saved[handle] = payload[:3] + handle + payload[7:]


async def test_native_id_collision_does_not_replace_existing_scene(native_scenes):
    ctx = native_scenes
    populate_native(ctx, [0x01020304])
    original = dict(ctx.saved)
    await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True)
    assert all(ctx.saved[handle] == payload for handle, payload in original.items())
    assert bytes.fromhex("01020305") in ctx.saved


async def test_native_id_allocation_wraps_before_reserved_range(
    native_scenes, monkeypatch, ha_modules
):
    ctx = native_scenes
    monkeypatch.setattr(
        ha_modules.coordinator.secrets, "randbelow", lambda bound: EFFECT_ID_MAX - 2
    )
    populate_native(ctx, [EFFECT_ID_MAX - 1])
    await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True)
    assert b"\x00\x00\x00\x01" in ctx.saved


async def test_native_capacity_counts_all_listed_ids_and_preview_uses_no_slot(native_scenes):
    ctx = native_scenes
    populate_native(ctx, [*range(1, 12), 0xFFFF0001])
    with pytest.raises(RuntimeError, match="12 listed scenes"):
        await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True)
    ctx.device.add_scene.assert_not_awaited()
    await ctx.coordinator.async_scene_action("police", "fade", 24, upload=False)
    ctx.device.preview_scene.assert_awaited_once_with(
        build_effect_v2("ff00000000ff", 1, "Police 1")
    )
    assert len(ctx.saved) == 12


async def test_native_duplicate_name_prevents_upload(native_scenes):
    ctx = native_scenes
    ctx.saved[b"\x00\x00\x00\x01"] = build_effect_v2("00ff00", 1, "Police 1")
    with pytest.raises(RuntimeError, match="name already exists"):
        await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True)
    ctx.device.add_scene.assert_not_awaited()


@pytest.mark.parametrize("detail", [None, {"name": ""}, TimeoutError("read failed")])
async def test_native_unreadable_existing_scene_prevents_upload(native_scenes, detail):
    ctx = native_scenes
    populate_native(ctx, [1])
    ctx.device.get_scene.side_effect = None
    if isinstance(detail, Exception):
        ctx.device.get_scene.side_effect = detail
    else:
        ctx.device.get_scene.return_value = detail
    with pytest.raises(RuntimeError):
        await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True)
    ctx.device.add_scene.assert_not_awaited()


@pytest.mark.parametrize("handles", [[b"\x01"], [b"\x00" * 4, b"\x00" * 4]])
async def test_native_invalid_id_list_prevents_upload(native_scenes, handles):
    ctx = native_scenes
    ctx.device.list_scenes.side_effect = None
    ctx.device.list_scenes.return_value = handles
    with pytest.raises(RuntimeError, match="Invalid native scene ID list"):
        await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True)
    ctx.device.add_scene.assert_not_awaited()


@pytest.mark.parametrize("field", ["id", "name", "motion_id", "palette_data", "motion_options"])
async def test_native_readback_mismatch_never_activates(native_scenes, field):
    ctx = native_scenes
    detail = _parse_effect(build_effect_v2("ff00000000ff", 0x01020304, "Police 1"))
    detail[field] = None
    ctx.device.get_scene.side_effect = None
    ctx.device.get_scene.return_value = detail
    with pytest.raises(RuntimeError, match="verification failed"):
        await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True, activate=True)
    ctx.device.add_scene.assert_awaited_once()
    ctx.device.play_scene.assert_not_awaited()


async def test_native_unexpected_returned_id_does_not_activate(native_scenes):
    ctx = native_scenes
    ctx.device.add_scene.side_effect = None
    ctx.device.add_scene.return_value = b"\x01"
    with pytest.raises(RuntimeError, match="unexpected scene ID"):
        await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True, activate=True)
    ctx.device.get_scene.assert_not_awaited()
    ctx.device.play_scene.assert_not_awaited()


async def test_native_missing_previous_scene_after_write_is_reported(native_scenes):
    ctx = native_scenes
    populate_native(ctx, [1])
    ctx.device.list_scenes.side_effect = [
        [b"\x00\x00\x00\x01"], [bytes.fromhex("01020304")],
    ]
    with pytest.raises(RuntimeError, match="changed unexpectedly"):
        await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True, activate=True)
    ctx.device.add_scene.assert_awaited_once()
    ctx.device.play_scene.assert_not_awaited()


async def test_native_zero_transition_prevents_device_io(native_scenes):
    ctx = native_scenes
    with pytest.raises(RuntimeError, match=r"transition must be an integer in 1\.\.600"):
        await ctx.coordinator.async_scene_action("police", "fade", 0, upload=False)
    ctx.device.query_motion_capabilities.assert_not_awaited()
    ctx.device.preview_scene.assert_not_awaited()


async def test_native_unsupported_motion_prevents_writes(native_scenes):
    ctx = native_scenes
    ctx.device.query_motion_capabilities.return_value = {"motions": []}
    with pytest.raises(RuntimeError, match="does not support this scene animation"):
        await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True)
    ctx.device.add_scene.assert_not_awaited()
    ctx.device.list_scenes.assert_not_awaited()


async def test_native_legacy_transport_is_rejected_without_fallback(native_scenes, factory):
    ctx = native_scenes
    ctx.coordinator._session = factory.legacy()
    with pytest.raises(RuntimeError, match="requires native effectV2"):
        await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True)


@pytest.mark.parametrize("firmware", ["4.1.0", None, "invalid"])
async def test_old_or_unknown_nl67_firmware_is_not_enabled(native_scenes, firmware):
    ctx = native_scenes
    ctx.coordinator.data["capabilities"] = device_capabilities("NL67", firmware)
    with pytest.raises(RuntimeError, match="does not support scene_upload"):
        await ctx.coordinator.async_scene_action("police", "fade", 24, upload=True)
    ctx.device.add_scene.assert_not_awaited()


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


# -- NL62 (Secretlab MAGRGB): flow and stripes ------------------------------------------


@pytest.fixture
def nl62_scenes(scenes):
    coordinator = scenes.coordinator
    coordinator.config_entry.data["model"] = "NL62"
    coordinator.data["capabilities"] = device_capabilities("NL62", "1.6.49")
    return scenes


def _metadata(payload: bytes) -> bytes:
    """Return the TLV1 0x01 metadata bytes of a simple-scene payload."""
    assert payload[0] == 1
    return payload[2 : 2 + payload[1]]


async def test_nl62_uploads_stripes_with_direction_and_segment(nl62_scenes):
    await nl62_scenes.coordinator.async_scene_action(
        "police", "stripes", 18, upload=True, direction="right", segment=24
    )
    payload = nl62_scenes.device.add_scene.await_args.args[0]
    # [scene id, stripes=6, transition, direction (right=1), segment]
    assert _metadata(payload) == bytes([1, 6, 18, 1, 24])
    assert nl62_scenes.saved[1] == payload


async def test_nl62_uploads_flow_with_direction_and_loop(nl62_scenes):
    await nl62_scenes.coordinator.async_scene_action(
        "police", "flow", 8, upload=True, direction="up"
    )
    payload = nl62_scenes.device.add_scene.await_args.args[0]
    # [scene id, flow=5, transition, wait, direction (up=2), loop]
    assert _metadata(payload) == bytes([1, 5, 8, 0, 2, 1])


@pytest.mark.parametrize(("direction", "code"), [("left", 0), ("right", 1), ("up", 2), ("down", 3)])
async def test_nl62_direction_names_map_to_the_apps_codes(nl62_scenes, direction, code):
    await nl62_scenes.coordinator.async_scene_action(
        "police", "flow", 8, upload=False, direction=direction
    )
    payload = nl62_scenes.device.preview_scene.await_args.args[0]
    assert _metadata(payload)[4] == code


async def test_nl62_flow_and_stripes_preview_without_saving(nl62_scenes):
    for effect, code in (("flow", 5), ("stripes", 6)):
        await nl62_scenes.coordinator.async_scene_action("police", effect, 10, upload=False)
        assert _metadata(nl62_scenes.device.preview_scene.await_args.args[0])[1] == code
    nl62_scenes.device.add_scene.assert_not_awaited()


async def test_nl62_upload_readback_verifies_flow_and_stripes(nl62_scenes):
    await nl62_scenes.coordinator.async_scene_action(
        "police", "stripes", 18, upload=True, direction="down", segment=7
    )
    nl62_scenes.device.get_scene.assert_awaited_once()
    assert nl62_scenes.coordinator.data["scene_names"][b"\x01"] == "Police 1"


@pytest.mark.parametrize("effect", ["flow", "stripes"])
async def test_other_models_reject_nl62_only_effects_before_any_device_io(scenes, effect):
    with pytest.raises(RuntimeError, match="does not support this scene animation"):
        await scenes.coordinator.async_scene_action("police", effect, 24, upload=True)
    scenes.device.list_scenes.assert_not_awaited()
    scenes.device.preview_scene.assert_not_awaited()
    scenes.device.add_scene.assert_not_awaited()


async def test_nl62_presets_do_not_count_toward_the_user_scene_limit(nl62_scenes):
    populate(nl62_scenes, [*range(1, 12), *range(244, 250)])
    await nl62_scenes.coordinator.async_scene_action("police", "stripes", 24, upload=True)
    assert nl62_scenes.device.add_scene.await_args.args[0][2] == 12
    assert {244, 245, 246, 247, 248, 249} <= set(nl62_scenes.saved)


async def test_nl62_full_user_storage_rejects_upload_but_allows_preview(nl62_scenes):
    populate(nl62_scenes, [*range(1, 13), *range(244, 250)])
    with pytest.raises(RuntimeError, match="12 saved user scenes"):
        await nl62_scenes.coordinator.async_scene_action("police", "flow", 24, upload=True)
    nl62_scenes.device.add_scene.assert_not_awaited()
    await nl62_scenes.coordinator.async_scene_action("police", "flow", 24, upload=False)
    nl62_scenes.device.preview_scene.assert_awaited_once()


async def test_nl62_upload_never_takes_a_preset_id(nl62_scenes):
    populate(nl62_scenes, range(1, 12))
    await nl62_scenes.coordinator.async_scene_action("police", "flow", 24, upload=True)
    assigned = nl62_scenes.device.add_scene.await_args.args[0][2]
    assert 1 <= assigned <= 243


@pytest.mark.parametrize("direction", ["", "sideways", None, 1])
async def test_invalid_direction_prevents_device_io(nl62_scenes, direction):
    with pytest.raises(RuntimeError, match="Direction must be"):
        await nl62_scenes.coordinator.async_scene_action(
            "police", "flow", 24, upload=True, direction=direction
        )
    nl62_scenes.device.list_scenes.assert_not_awaited()


@pytest.mark.parametrize("segment", [-1, 256, 1.5, True, None])
async def test_invalid_segment_prevents_device_io(nl62_scenes, segment):
    with pytest.raises(RuntimeError, match="Segment must be an integer from 0 to 255"):
        await nl62_scenes.coordinator.async_scene_action(
            "police", "stripes", 24, upload=True, segment=segment
        )
    nl62_scenes.device.list_scenes.assert_not_awaited()


async def test_nl62_over_modern_session_is_rejected_before_writing(nl62_scenes, factory):
    nl62_scenes.coordinator._session = factory.modern()
    with pytest.raises(RuntimeError, match="legacy protocol"):
        await nl62_scenes.coordinator.async_scene_action("police", "flow", 24, upload=True)
    nl62_scenes.device.add_scene.assert_not_awaited()
