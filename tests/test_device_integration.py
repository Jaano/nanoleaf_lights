"""Integration tests — require a real device reachable via bulb.json.

Run with:
    pytest tests/test_device_integration.py -v -s

Tests run in declaration order and communicate via the module-level ``_state``
dict.  Each test opens its own KEX + auth session independently (no shared
session).  The device must be **paired** before the test run starts; the first
test revokes the token and re-pairs so the suite owns a fresh token.

The device config is loaded from ``bulb.json`` in the project root.
Override with the ``NANOLEAF_CONF`` environment variable.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from cli import ESSENTIALS_MAX_COLORS, build_simple_scene_tlv, normalize_scene_palette
from nl_ltpdu import LtpduSession

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONF_PATH: Path = Path(os.environ.get("NANOLEAF_CONF", "bulb.json"))
TIMEOUT = 30.0

# Single-color palettes used by scene tests.
_RED_PALETTE = "ff0000"
_BLUE_PALETTE = "0000ff"
_GREEN_PALETTE = "00ff00"
_YELLOW_PALETTE = "ffff00"
_CYAN_PALETTE = "00ffff"
_MAGENTA_PALETTE = "ff00ff"
_WHITE_PALETTE = "ffffff"

# 7-color palette using all primaries + secondaries + white — every color
# is a pure HSB triple that round-trips through RGB↔HSB without precision loss.
_MAX_PALETTE = (
    _RED_PALETTE + _GREEN_PALETTE + _BLUE_PALETTE
    + _YELLOW_PALETTE + _CYAN_PALETTE + _MAGENTA_PALETTE + _WHITE_PALETTE
)

# Palette with consecutive identical colors to exercise compact-repeats encoding:
# 3× red + 2× green + 2× blue = 3 runs, 7 total entries.
_COMPACT_PALETTE = _RED_PALETTE * 3 + _GREEN_PALETTE * 2 + _BLUE_PALETTE * 2


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

# Module-level dict used to pass state between tests (token, scene IDs, …).
# Tests are order-dependent; running a single test in isolation may skip.
_state: dict[str, Any] = {}


def _load_conf() -> dict[str, Any]:
    with CONF_PATH.open() as f:
        return json.load(f)


def _save_conf(config: dict[str, Any]) -> None:
    with CONF_PATH.open("w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def device_conf():
    """Load and validate the device config file once per test run."""
    if not CONF_PATH.exists():
        pytest.skip(f"Device config not found: {CONF_PATH} — set NANOLEAF_CONF or create bulb.json")
    conf = _load_conf()
    if not conf.get("ip_address") or not conf.get("port"):
        pytest.skip("Device config is missing ip_address or port")
    if not conf.get("pin"):
        pytest.skip("Device config is missing 'pin' — needed to re-pair during integration tests")
    return conf


@pytest_asyncio.fixture(scope="module", autouse=True)
async def restore_state(device_conf):
    """Restore the initial light state after the full test suite completes."""
    yield
    state = _state.get("initial_state")
    if not state:
        return
    conf = _load_conf()
    if not conf.get("token"):
        return
    await asyncio.sleep(3.0)  # let any in-flight device responses settle
    for _attempt in range(3):
        session = await _open_authed_session(conf)
        try:
            await session.set_light_state(
                on=state["power"],
                brightness=state["brightness"],
                hue=state["hue"],
                saturation=state["saturation"],
                timeout=TIMEOUT,
            )
            break  # success
        except RuntimeError:
            if _attempt >= 2:
                raise
        finally:
            await session.close()
        await asyncio.sleep(3.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _open_authed_session(conf: dict[str, Any]) -> LtpduSession:
    """KEX + auth using the token currently stored in *conf*.

    A short preamble sleep prevents the NL67 from rate-limiting rapid
    successive key-exchange handshakes.  Retries once with a longer backoff
    if KEX still times out or auth is rejected (status 0x08 = device busy).
    """
    token_hex: str = conf["token"]
    await asyncio.sleep(0.3)  # preamble: give device time to settle
    for attempt in range(2):
        try:
            session = await LtpduSession.kex(
                conf["ip_address"], conf["port"], model=conf.get("model"), timeout=TIMEOUT
            )
            await session.auth(bytes.fromhex(token_hex), timeout=TIMEOUT)
            return session
        except RuntimeError:
            if attempt >= 1:
                raise
            await asyncio.sleep(3.0)
    raise RuntimeError("unreachable")


async def _alloc_scene_id(session: LtpduSession) -> int:
    """Return the first free scene slot (1-243) not yet stored on the device."""
    occupied = set(await session.list_scenes(timeout=TIMEOUT))
    free = next((i for i in range(1, 244) if i not in occupied), None)
    if free is None:
        raise RuntimeError("No free scene slots available (1-243 all occupied)")
    return free


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeviceIntegration:
    """Full-device integration suite.  Tests execute in declaration order."""

    # ------------------------------------------------------------------
    # 1. Unpair — revoke existing token
    # ------------------------------------------------------------------

    async def test_01_unpair(self, device_conf):
        """Revoke the stored token so we own a clean pairing state."""
        conf = _load_conf()
        token_hex = conf.get("token")
        if not token_hex:
            # Already unpaired — not a skip, just nothing to do.
            print("no token in config — already unpaired")
            return

        session = await LtpduSession.kex(
            conf["ip_address"], conf["port"], model=conf.get("model"), timeout=TIMEOUT
        )
        try:
            await session.auth(bytes.fromhex(token_hex), timeout=TIMEOUT)
            await session.unpair(timeout=TIMEOUT)
        finally:
            await session.close()

        # Remove token from config so pair can start fresh.
        conf.pop("token", None)
        conf.pop("token_issued", None)
        _save_conf(conf)
        print("unpaired — token revoked")

    # ------------------------------------------------------------------
    # 2. Pair — obtain a fresh token
    # ------------------------------------------------------------------

    async def test_02_pair(self, device_conf):
        """Pair using the PIN stored in config; save the new token."""
        conf = _load_conf()
        pin = conf["pin"]

        session = await LtpduSession.kex(
            conf["ip_address"], conf["port"], model=conf.get("model"), timeout=TIMEOUT
        )
        try:
            token = await session.pair(pin, timeout=TIMEOUT)
        finally:
            await session.close()

        assert len(token) == 8, f"expected 8-byte token, got {len(token)}"

        conf["token"] = token.hex()
        _save_conf(conf)
        _state["token"] = token.hex()
        print(f"paired — token: {token.hex()}")

    # ------------------------------------------------------------------
    # 3. Device info
    # ------------------------------------------------------------------

    async def test_03_device_info(self, device_conf):
        """Query device info and verify mandatory fields are present."""
        conf = _load_conf()
        session = await _open_authed_session(conf)
        try:
            info = await session.query_device_info(timeout=TIMEOUT)
        finally:
            await session.close()

        print(f"device info: {info}")
        assert info.get("firmware_version"), "firmware_version missing"
        assert info.get("hardware_version") or info.get("serial_number"), \
            "at least one of hardware_version / serial_number must be present"

    # ------------------------------------------------------------------
    # 4. Query light state
    # ------------------------------------------------------------------

    async def test_04_query_light_state(self, device_conf):
        """Read current light state and verify field types and ranges."""
        conf = _load_conf()
        session = await _open_authed_session(conf)
        try:
            state = await session.query_light_state(timeout=TIMEOUT)
        finally:
            await session.close()

        print(f"light state: {state}")
        assert isinstance(state["power"], bool)
        assert 0 <= state["brightness"] <= 100
        assert 0 <= state["hue"] < 360  # hue wraps; 360 == 0
        assert 0 <= state["saturation"] <= 100
        assert 1200 <= state["color_temp"] <= 6500, \
            f"color_temp {state['color_temp']} outside 1200-6500 K range"

        _state["initial_state"] = state

    # ------------------------------------------------------------------
    # 5. Power on
    # ------------------------------------------------------------------

    async def test_05_power_on(self, device_conf):
        """Turn the light on and verify it reports on."""
        conf = _load_conf()
        session = await _open_authed_session(conf)
        try:
            await session.set_light_state(on=True, timeout=TIMEOUT)
        finally:
            await session.close()

        await asyncio.sleep(1.5)

        # Retry up to 3× — a late response from the write session can arrive
        # during the first read and corrupt the cipher state.
        last_exc: Exception | None = None
        power_state: dict | None = None
        for _attempt in range(3):
            session = await _open_authed_session(conf)
            try:
                power_state = await session.query_light_state(timeout=TIMEOUT)
                break
            except RuntimeError as exc:
                last_exc = exc
            finally:
                await session.close()
            await asyncio.sleep(3.0)
        if power_state is None:
            raise last_exc  # type: ignore[misc]

        assert power_state["power"] is True, "expected power=True after turning on"

    # ------------------------------------------------------------------
    # 6. Set brightness
    # ------------------------------------------------------------------

    async def test_06_set_brightness(self, device_conf):
        """Set brightness to 50 and read it back."""
        conf = _load_conf()
        session = await _open_authed_session(conf)
        try:
            await session.set_light_state(brightness=50, timeout=TIMEOUT)
        finally:
            await session.close()

        await asyncio.sleep(1.5)

        session = await _open_authed_session(conf)
        try:
            state = await session.query_light_state(timeout=TIMEOUT)
        finally:
            await session.close()

        assert state["brightness"] == 50, \
            f"expected brightness=50, got {state['brightness']}"

    # ------------------------------------------------------------------
    # 7. Set hue + saturation
    # ------------------------------------------------------------------

    async def test_07_set_hue_saturation(self, device_conf):
        """Set hue=120 saturation=80 (green) and read back."""
        conf = _load_conf()
        session = await _open_authed_session(conf)
        try:
            await session.set_light_state(hue=120, saturation=80, timeout=TIMEOUT)
        finally:
            await session.close()

        await asyncio.sleep(1.5)

        session = await _open_authed_session(conf)
        try:
            state = await session.query_light_state(timeout=TIMEOUT)
        finally:
            await session.close()

        assert state["hue"] == 120, f"expected hue=120, got {state['hue']}"
        assert state["saturation"] == 80, \
            f"expected saturation=80, got {state['saturation']}"

    # ------------------------------------------------------------------
    # 8. Set color temperature
    # ------------------------------------------------------------------

    async def test_08_set_color_temp(self, device_conf):
        """Set color temperature to 4000 K and read back.

        Skipped on HSB-only models that do not support CCT (e.g. NL45).
        Uses two separate sessions: the NL67 switches colour modes on the CCT
        write and may return a non-standard response to queries on the same
        session immediately after.
        """
        conf = _load_conf()
        model = conf.get("model", "")
        if model.upper() in ("NL45", "NL55"):
            pytest.skip(f"model {model} does not support CCT")

        # Session 1: write.
        session = await _open_authed_session(conf)
        try:
            await session.set_light_state(color_temp=4000, timeout=TIMEOUT)
        finally:
            await session.close()

        await asyncio.sleep(1.5)  # let device finish mode switch

        # Session 2: fresh KEX so cipher state is clean.
        # Retry up to 3 times: a throttled device may deliver the CT-write
        # response late, which corrupts the first read attempt.
        last_exc: Exception | None = None
        ct_state: dict | None = None
        for _attempt in range(3):
            session = await _open_authed_session(conf)
            try:
                ct_state = await session.query_light_state(timeout=TIMEOUT)
                break
            except RuntimeError as exc:
                last_exc = exc
            finally:
                await session.close()
            await asyncio.sleep(3.0)
        if ct_state is None:
            raise last_exc  # type: ignore[misc]

        assert ct_state["color_temp"] == 4000, \
            f"expected color_temp=4000, got {ct_state['color_temp']}"

    # ------------------------------------------------------------------
    # 9. Identify (blink)
    # ------------------------------------------------------------------

    async def test_09_identify(self, device_conf):
        """Trigger identify (blink) — no return value to assert, just no exception."""
        conf = _load_conf()
        session = await _open_authed_session(conf)
        try:
            await session.identify(timeout=TIMEOUT)
        finally:
            await session.close()

        print("identify sent (blink triggered)")

    # ------------------------------------------------------------------
    # 10. List scenes (initial — may be empty)
    # ------------------------------------------------------------------

    async def test_10_list_scenes_initial(self, device_conf):
        """List scenes; record initial count for comparison after add."""
        conf = _load_conf()
        session = await _open_authed_session(conf)
        try:
            handles = await session.list_scenes(timeout=TIMEOUT)
        finally:
            await session.close()

        _state["initial_scene_count"] = len(handles)
        print(f"initial scenes: {list(handles)}")

    # ------------------------------------------------------------------
    # 11. Add a scene
    # ------------------------------------------------------------------

    async def test_11_add_scene(self, device_conf):
        """Add a simple FADE scene with a solid red palette."""
        await asyncio.sleep(1.0)  # let device settle after rapid sessions 01-10
        conf = _load_conf()
        session = await _open_authed_session(conf)
        try:
            slot = await _alloc_scene_id(session)
            scene_data = build_simple_scene_tlv(
                _RED_PALETTE, scene_id=slot, effect="fade", transition=24
            )
            assigned = await session.add_scene(scene_data, timeout=TIMEOUT)
        finally:
            await session.close()

        assert len(assigned) >= 1, "add_scene should return at least 1 byte"
        # Use the pre-allocated slot — the device response byte is unreliable
        # on NL67 (returns 0x00 even when the scene is stored at a different slot).
        _state["test_scene_id"] = slot
        print(f"scene added (slot={slot}, assigned={assigned.hex() if assigned else 'empty'})")

    # ------------------------------------------------------------------
    # 12. List scenes — count increased
    # ------------------------------------------------------------------

    async def test_12_list_scenes_after_add(self, device_conf):
        """Scene count must be at least initial + 1 after add."""
        conf = _load_conf()
        session = await _open_authed_session(conf)
        try:
            handles = await session.list_scenes(timeout=TIMEOUT)
        finally:
            await session.close()

        print(f"scenes after add: {list(handles)}")
        assert len(handles) >= _state.get("initial_scene_count", 0) + 1

    # ------------------------------------------------------------------
    # 13. Get scene detail
    # ------------------------------------------------------------------

    async def test_13_get_scene(self, device_conf):
        """Read back the just-added scene and verify palette round-trip."""
        conf = _load_conf()
        scene_id = _state.get("test_scene_id")
        if scene_id is None:
            pytest.skip("No scene from test_11_add_scene")

        session = await _open_authed_session(conf)
        try:
            detail = await session.get_scene(bytes([scene_id]), timeout=TIMEOUT)
        finally:
            await session.close()

        assert detail is not None, "get_scene returned None"
        print(f"scene detail: {detail}")
        assert "palette" in detail, "scene detail missing 'palette'"
        assert detail["palette"], "scene palette is empty"

    # ------------------------------------------------------------------
    # 14. Play (activate) the scene
    # ------------------------------------------------------------------

    async def test_14_play_scene(self, device_conf):
        """Activate the test scene on the device."""
        conf = _load_conf()
        scene_id = _state.get("test_scene_id")
        if scene_id is None:
            pytest.skip("No scene from test_11_add_scene")

        session = await _open_authed_session(conf)
        try:
            await session.play_scene(bytes([scene_id]), timeout=TIMEOUT)
        finally:
            await session.close()

        print(f"playing scene 0x{scene_id:02x}")

    # ------------------------------------------------------------------
    # 15. Get current scene
    # ------------------------------------------------------------------

    async def test_15_get_current_scene(self, device_conf):
        """Query active scene; must match the one we just played."""
        conf = _load_conf()
        scene_id = _state.get("test_scene_id")
        if scene_id is None:
            pytest.skip("No scene from test_11_add_scene")

        session = await _open_authed_session(conf)
        try:
            await asyncio.sleep(1.5)  # allow device to commit the active scene
            current = await session.get_current_scene(timeout=TIMEOUT)
        finally:
            await session.close()

        print(f"current scene: {current.hex() if current else '(none)'}")
        assert current, "no active scene reported after play_scene"
        assert current[0] == scene_id, \
            f"expected scene 0x{scene_id:02x}, got 0x{current[0]:02x}"

    # ------------------------------------------------------------------
    # 16. Preview a scene (transient, not saved)
    # ------------------------------------------------------------------

    async def test_16_preview_scene(self, device_conf):
        """Preview a blue palette scene without persisting it — no exception expected.

        preview_scene plays a running animation; query_light_state reads the
        instantaneous color mid-animation which is not guaranteed to be blue,
        so we only verify the call succeeds.
        """
        conf = _load_conf()
        scene_data = build_simple_scene_tlv(
            _BLUE_PALETTE, scene_id=1, effect="fade", transition=10
        )
        session = await _open_authed_session(conf)
        try:
            await session.preview_scene(scene_data, timeout=TIMEOUT)
        finally:
            await session.close()

        print("preview sent (blue fade) — no exception")

    # ------------------------------------------------------------------
    # 17. Delete the test scene
    # ------------------------------------------------------------------

    async def test_17_delete_scene(self, device_conf):
        """Delete the scene added in test_11 and verify it is gone."""
        conf = _load_conf()
        scene_id = _state.get("test_scene_id")
        if scene_id is None:
            pytest.skip("No scene from test_11_add_scene")

        # Session 1: delete.
        session = await _open_authed_session(conf)
        try:
            await session.delete_scene(bytes([scene_id]), timeout=TIMEOUT)
        finally:
            await session.close()

        await asyncio.sleep(1.5)  # let device process delete before next query

        # Session 2: verify the scene is gone.
        session = await _open_authed_session(conf)
        try:
            handles = await session.list_scenes(timeout=TIMEOUT)
        finally:
            await session.close()

        assert scene_id not in handles, \
            f"scene 0x{scene_id:02x} still listed after delete"
        print(f"scene 0x{scene_id:02x} deleted; remaining: {list(handles)}")

    # ------------------------------------------------------------------
    # 18. Add multiple scenes then delete in bulk
    # ------------------------------------------------------------------

    async def test_18_bulk_delete(self, device_conf):
        """Add two scenes then delete all and verify none remain."""
        conf = _load_conf()

        # Session 1 (write): add two scenes.  Retry if a late CoAP retransmission
        # from the previous test's session corrupts the first response.
        last_exc: Exception | None = None
        for _attempt in range(3):
            session = await _open_authed_session(conf)
            try:
                for palette in (_RED_PALETTE, _GREEN_PALETTE):
                    slot = await _alloc_scene_id(session)
                    scene_data = build_simple_scene_tlv(
                        palette, scene_id=slot, effect="fade", transition=24
                    )
                    await session.add_scene(scene_data, timeout=TIMEOUT)
                last_exc = None
                break
            except (ValueError, RuntimeError) as exc:
                last_exc = exc
            finally:
                await session.close()
            await asyncio.sleep(3.0)
        if last_exc is not None:
            raise last_exc

        await asyncio.sleep(1.5)

        # Session 2 (verify + delete): list all, delete every one, verify empty.
        # _alloc_scene_id may have allocated extra slots on retried attempts;
        # deleting everything in handles_before covers leftovers too.
        handles_after: list[int] | None = None
        session = await _open_authed_session(conf)
        try:
            handles_before = await session.list_scenes(timeout=TIMEOUT)
            print(f"scenes before bulk delete: {list(handles_before)}")
            assert handles_before, "expected at least one scene before bulk delete"
            for byte in list(handles_before):
                await session.delete_scene(bytes([byte]), timeout=TIMEOUT)
            handles_after = await session.list_scenes(timeout=TIMEOUT)
        finally:
            await session.close()

        assert handles_after is not None
        assert len(handles_after) == 0, \
            f"expected 0 scenes after bulk delete, got {list(handles_after)}"
        print("all scenes deleted")

    # ------------------------------------------------------------------
    # 19. Max-palette scene (7 colors, full round-trip)
    # ------------------------------------------------------------------

    async def test_19_max_palette_scene(self, device_conf):
        """Add a scene with the maximum 7-color palette, verify round-trip, then clean up."""
        conf = _load_conf()

        # Session 1: allocate slot + add scene.
        session = await _open_authed_session(conf)
        try:
            slot = await _alloc_scene_id(session)
            scene_data = build_simple_scene_tlv(
                _MAX_PALETTE,
                scene_id=slot,
                effect="fade",
                transition=24,
                max_colors=ESSENTIALS_MAX_COLORS,
            )
            await session.add_scene(scene_data, timeout=TIMEOUT)
        finally:
            await session.close()

        await asyncio.sleep(1.5)

        # Session 2: verify, play, assert palette round-trip, then delete.
        detail: dict | None = None
        session = await _open_authed_session(conf)
        try:
            handles = await session.list_scenes(timeout=TIMEOUT)
            assert slot in handles, f"max-palette scene (slot={slot}) not listed after add"
            await session.play_scene(bytes([slot]), timeout=TIMEOUT)
            detail = await session.get_scene(bytes([slot]), timeout=TIMEOUT)
            assert detail is not None, "get_scene returned None for max-palette scene"
            await session.delete_scene(bytes([slot]), timeout=TIMEOUT)
        finally:
            await session.close()

        expected = normalize_scene_palette(_MAX_PALETTE)
        assert detail["palette"] == expected, (
            f"max-palette round-trip mismatch:\n"
            f"  got:      {detail['palette']}\n"
            f"  expected: {expected}"
        )
        print(f"max-palette scene (slot={slot}) round-trip OK: {detail['palette']}")

    # ------------------------------------------------------------------
    # 20. Compact-repeats scene
    # ------------------------------------------------------------------

    async def test_20_compact_repeats_scene(self, device_conf):
        """Add a scene built with compact-repeats encoding and verify the device accepts it.

        Palette: 3× red + 2× green + 2× blue (3 runs, 7 entries) encoded with
        compact_repeats=True.  The test checks that add_scene / play_scene /
        get_scene all succeed without error.  Exact palette content is not
        asserted because the device may expand the compact format internally
        before returning it via get_scene.
        """
        conf = _load_conf()

        # Session 1: allocate slot + add scene with compact_repeats=True.
        session = await _open_authed_session(conf)
        try:
            slot = await _alloc_scene_id(session)
            scene_data = build_simple_scene_tlv(
                _COMPACT_PALETTE,
                scene_id=slot,
                effect="fade",
                transition=24,
                compact_repeats=True,
            )
            await session.add_scene(scene_data, timeout=TIMEOUT)
        finally:
            await session.close()

        await asyncio.sleep(1.5)

        # Session 2: play and verify the device can read it back.
        detail: dict | None = None
        session = await _open_authed_session(conf)
        try:
            handles = await session.list_scenes(timeout=TIMEOUT)
            assert slot in handles, f"compact-repeats scene (slot={slot}) not listed after add"
            await session.play_scene(bytes([slot]), timeout=TIMEOUT)
            detail = await session.get_scene(bytes([slot]), timeout=TIMEOUT)
            assert detail is not None, "get_scene returned None for compact-repeats scene"
            assert detail["palette"], "get_scene returned empty palette for compact-repeats scene"
            await session.delete_scene(bytes([slot]), timeout=TIMEOUT)
        finally:
            await session.close()

        print(
            f"compact-repeats scene (slot={slot}) accepted by device; "
            f"palette: {detail['palette']}"
        )

    # ------------------------------------------------------------------
    # 21. Power off — leave in clean state
    # ------------------------------------------------------------------

    async def test_21_power_off(self, device_conf):
        """Turn the light off to leave it in a clean state."""
        conf = _load_conf()
        session = await _open_authed_session(conf)
        try:
            await session.set_light_state(on=False, timeout=TIMEOUT)
        finally:
            await session.close()

        await asyncio.sleep(1.5)

        session = await _open_authed_session(conf)
        try:
            state = await session.query_light_state(timeout=TIMEOUT)
        finally:
            await session.close()

        assert state["power"] is False, \
            f"expected power=False after turning off, got {state['power']}"
        print("light off — integration tests complete")
