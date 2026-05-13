"""Tests for pure scene helper functions in cli.py.

All tests are offline — no device sessions are opened.
"""

import pytest

from cli import (
    ESSENTIALS_MAX_COLORS,
    build_simple_scene_tlv,
    encode_palette_bytes,
    find_scene_effect,
    normalize_scene_palette,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RED_HEX = "ff0000"   # → hue≈0, sat=100, bri=100 after HSB round-trip
BLUE_HEX = "0000ff"  # → hue≈240, sat=100, bri=100
BLACK_HEX = "000000"  # bri=0, invisible


def _db(*effects):
    """Build a minimal scenes.json dict."""
    return {"effects": list(effects)}


def _effect(uuid, name, palette):
    return {"uuid": uuid, "name": name, "palette": palette}


# ---------------------------------------------------------------------------
# find_scene_effect
# ---------------------------------------------------------------------------


class TestFindSceneEffect:
    def test_uuid_match(self):
        db = _db(_effect("aaaa", "Alpha", RED_HEX))
        result = find_scene_effect(db, "aaaa")
        assert result["name"] == "Alpha"

    def test_exact_name_match(self):
        db = _db(_effect("aaaa", "Police 1", RED_HEX))
        result = find_scene_effect(db, "Police 1")
        assert result["uuid"] == "aaaa"

    def test_exact_name_case_insensitive(self):
        db = _db(_effect("aaaa", "Police 1", RED_HEX))
        result = find_scene_effect(db, "police 1")
        assert result["uuid"] == "aaaa"

    def test_substring_match(self):
        db = _db(_effect("aaaa", "Police 1", RED_HEX))
        result = find_scene_effect(db, "Police")
        assert result["uuid"] == "aaaa"

    def test_ambiguous_substring_exits(self):
        db = _db(
            _effect("aaaa", "Police 1", RED_HEX),
            _effect("bbbb", "Police 2", BLUE_HEX),
        )
        with pytest.raises(SystemExit):
            find_scene_effect(db, "Police")

    def test_missing_exits(self):
        db = _db(_effect("aaaa", "Alpha", RED_HEX))
        with pytest.raises(SystemExit):
            find_scene_effect(db, "Nonexistent")

    def test_uuid_preferred_over_name(self):
        # An effect whose UUID matches the query string should win even if
        # another effect has that string as a name.
        db = _db(
            _effect("exact-uuid", "Some Name", RED_HEX),
            _effect("bbbb", "exact-uuid", BLUE_HEX),
        )
        result = find_scene_effect(db, "exact-uuid")
        assert result["uuid"] == "exact-uuid"


# ---------------------------------------------------------------------------
# encode_palette_bytes
# ---------------------------------------------------------------------------


class TestEncodePaletteBytes:
    def test_valid_single_color(self):
        result = encode_palette_bytes(RED_HEX)
        assert len(result) == 4  # 1 count byte + 3 packed bytes
        assert result[0] == 1

    def test_count_byte_correct(self):
        two_color = RED_HEX + BLUE_HEX
        result = encode_palette_bytes(two_color)
        assert result[0] == 2
        assert len(result) == 7  # 1 + 2×3

    def test_bri_zero_filtered(self):
        # Black (bri=0) comes first, only red survives
        palette = BLACK_HEX + RED_HEX
        result = encode_palette_bytes(palette)
        assert result[0] == 1

    def test_all_zero_exits(self):
        with pytest.raises(SystemExit):
            encode_palette_bytes(BLACK_HEX)

    def test_max_colors_truncation(self):
        eight_colors = RED_HEX * 8
        result = encode_palette_bytes(eight_colors, max_colors=ESSENTIALS_MAX_COLORS)
        assert result[0] == ESSENTIALS_MAX_COLORS

    def test_max_colors_custom(self):
        six_colors = RED_HEX * 6
        result = encode_palette_bytes(six_colors, max_colors=3)
        assert result[0] == 3

    def test_invalid_hex_length_exits(self):
        with pytest.raises(SystemExit):
            encode_palette_bytes("ff00")  # length 4 — not multiple of 6

    def test_bit23_zero_in_standard_mode(self):
        # bit 23 of the packed word must always be 0 in non-compact mode
        result = encode_palette_bytes(RED_HEX + BLUE_HEX)
        for i in range(result[0]):
            offset = 1 + i * 3
            assert result[offset] & 0x80 == 0, f"bit23 set at entry {i}"

    # -- compact repeats -----------------------------------------------------

    def test_compact_no_repeats_same_size(self):
        # Two distinct colors — compact should produce the same byte count
        two_color = RED_HEX + BLUE_HEX
        nc = encode_palette_bytes(two_color)
        cp = encode_palette_bytes(two_color, compact_repeats=True)
        assert nc == cp

    def test_compact_with_repeats_smaller(self):
        # 3 identical reds + 1 blue: compact collapses 3 reds into one entry+count
        palette = RED_HEX * 3 + BLUE_HEX
        nc = encode_palette_bytes(palette)
        cp = encode_palette_bytes(palette, compact_repeats=True)
        assert len(cp) < len(nc)

    def test_compact_count_byte_is_run_count(self):
        # 3 reds (1 run) + 1 blue (1 run) → 2 runs
        palette = RED_HEX * 3 + BLUE_HEX
        cp = encode_palette_bytes(palette, compact_repeats=True)
        assert cp[0] == 2

    def test_compact_bit23_set_for_run(self):
        # The first entry (3 reds) should have bit23=1
        palette = RED_HEX * 3 + BLUE_HEX
        cp = encode_palette_bytes(palette, compact_repeats=True)
        assert cp[1] & 0x80 != 0  # bit23 of first packed word

    def test_compact_repeat_count_byte_present(self):
        # 3 reds → entry: 3B packed + 1B count = 4B; then 1 blue (no repeat): 3B
        # total: 1 (count) + 4 + 3 = 8 bytes
        palette = RED_HEX * 3 + BLUE_HEX
        cp = encode_palette_bytes(palette, compact_repeats=True)
        assert len(cp) == 8


# ---------------------------------------------------------------------------
# normalize_scene_palette
# ---------------------------------------------------------------------------


class TestNormalizeScenePalette:
    def test_same_palette_same_result(self):
        a = normalize_scene_palette(RED_HEX + BLUE_HEX)
        b = normalize_scene_palette(RED_HEX + BLUE_HEX)
        assert a == b

    def test_bri_zero_filtered(self):
        with_black = BLACK_HEX + RED_HEX
        without_black = RED_HEX
        assert normalize_scene_palette(with_black) == normalize_scene_palette(without_black)

    def test_truncation_applied(self):
        eight = RED_HEX * 8
        seven = RED_HEX * 7
        assert normalize_scene_palette(eight) == normalize_scene_palette(seven)

    def test_all_zero_returns_empty(self):
        assert normalize_scene_palette(BLACK_HEX) == ""

    def test_invalid_hex_returns_empty(self):
        assert normalize_scene_palette("zzzzzz") == ""

    def test_different_palettes_differ(self):
        a = normalize_scene_palette(RED_HEX)
        b = normalize_scene_palette(BLUE_HEX)
        assert a != b


# ---------------------------------------------------------------------------
# build_simple_scene_tlv — structure checks
# ---------------------------------------------------------------------------


class TestBuildSimpleSceneTlv:
    def _parse(self, data: bytes) -> tuple[bytes, bytes]:
        """Split TLV1(0x01, meta) + TLV1(0x02, palette)."""
        assert data[0] == 0x01
        meta_len = data[1]
        meta = data[2 : 2 + meta_len]
        rest = data[2 + meta_len :]
        assert rest[0] == 0x02
        pal_len = rest[1]
        palette = rest[2 : 2 + pal_len]
        return meta, palette

    def test_fade_metadata_bytes(self):
        # FADE: [scene_id, 0x01, transition, wait, loop]
        data = build_simple_scene_tlv(RED_HEX, scene_id=5)
        meta, _ = self._parse(data)
        assert meta[0] == 5      # scene_id
        assert meta[1] == 0x01   # FADE code
        assert meta[2] == 24     # default transition
        assert meta[3] == 0      # default wait
        assert meta[4] == 1      # default loop=True

    def test_random_metadata_bytes(self):
        # RANDOM: [scene_id, 0x02, transition, wait]
        data = build_simple_scene_tlv(RED_HEX, scene_id=1, effect="random")
        meta, _ = self._parse(data)
        assert meta[1] == 0x02
        assert len(meta) == 4

    def test_highlight_metadata_bytes(self):
        # HIGHLIGHT: [scene_id, 0x03, transition, wait, main_probability]
        data = build_simple_scene_tlv(RED_HEX, scene_id=1, effect="highlight", main_probability=60)
        meta, _ = self._parse(data)
        assert meta[1] == 0x03
        assert meta[4] == 60

    def test_stream_metadata_bytes(self):
        # STREAM: [scene_id, 0x04]  — no extra bytes
        data = build_simple_scene_tlv(RED_HEX, scene_id=1, effect="stream")
        meta, _ = self._parse(data)
        assert meta[1] == 0x04
        assert len(meta) == 2

    def test_flow_metadata_bytes(self):
        # FLOW: [scene_id, 0x05, transition, wait, direction, loop]
        data = build_simple_scene_tlv(RED_HEX, scene_id=1, effect="flow", direction=3)
        meta, _ = self._parse(data)
        assert meta[1] == 0x05
        assert meta[4] == 3     # direction
        assert len(meta) == 6

    def test_stripes_metadata_bytes(self):
        # STRIPES: [scene_id, 0x06, transition, direction, segment]
        data = build_simple_scene_tlv(RED_HEX, scene_id=1, effect="stripes", segment=40)
        meta, _ = self._parse(data)
        assert meta[1] == 0x06
        assert meta[4] == 40    # segment (after transition, direction)
        assert len(meta) == 5

    def test_palette_count_in_payload(self):
        two_color = RED_HEX + BLUE_HEX
        data = build_simple_scene_tlv(two_color, scene_id=1)
        _, palette = self._parse(data)
        assert palette[0] == 2

    def test_unknown_effect_exits(self):
        with pytest.raises(SystemExit):
            build_simple_scene_tlv(RED_HEX, scene_id=1, effect="bogus")

    def test_no_loop(self):
        data = build_simple_scene_tlv(RED_HEX, scene_id=1, loop=False)
        meta, _ = self._parse(data)
        assert meta[4] == 0  # loop byte
