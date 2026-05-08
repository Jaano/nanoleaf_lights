"""Nanoleaf cloud API clients.

NanoleafCloudApi  — scene/effect discovery at https://my.nanoleaf.me/api
NanoleafFirmwareApi — firmware update checks at https://firmware.nanoleaf.me
"""

from __future__ import annotations

import colorsys
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

_LOGGER = logging.getLogger(__name__)

Effect = dict[str, str]
RawEffect = dict[str, Any]

# ---------------------------------------------------------------------------
# Cloud effects API
# ---------------------------------------------------------------------------

_CLOUD_BASE      = "https://my.nanoleaf.me/api"
_EFFECTS_SEARCH  = f"{_CLOUD_BASE}/v1/effects/search"

_DEFAULT_PAGE_SIZE   = 50
_DEFAULT_RETRY_PAUSE = 2
_DEFAULT_MAX_RETRIES = 3


class NanoleafCloudApi:
    """Client for the Nanoleaf cloud scene/effect API."""

    def __init__(
        self,
        page_size: int   = _DEFAULT_PAGE_SIZE,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        retry_pause: int = _DEFAULT_RETRY_PAUSE,
        timeout: int     = 15,
    ) -> None:
        self.page_size   = page_size
        self.max_retries = max_retries
        self.retry_pause = retry_pause
        self.timeout     = timeout

    def post_effects_search(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """POST to v1/effects/search; return JSON or None on 403 (hard page cap)."""
        for attempt in range(1, self.max_retries + 1):
            try:
                r = requests.post(_EFFECTS_SEARCH, json=payload, timeout=self.timeout)
                r.raise_for_status()
                return r.json()
            except requests.HTTPError as e:
                status = e.response.status_code
                if status == 403:
                    return None  # API hard page cap — caller treats as end of results
                _LOGGER.warning("HTTP %s on attempt %d: %s", status, attempt, e)
                if status < 500 or attempt == self.max_retries:
                    raise
            except requests.RequestException as e:
                _LOGGER.warning("Request error on attempt %d: %s", attempt, e)
                if attempt == self.max_retries:
                    raise
            time.sleep(self.retry_pause)
        return None

    @staticmethod
    def normalise_effect(e: RawEffect) -> Effect | None:
        """Return {uuid, name, palette} or None when palette is absent."""
        raw_palette: list[RawEffect] | None = e.get("palette")  # type: ignore[assignment]
        if not raw_palette:
            return None

        def _rrggbb(h: float, s: float, b: float) -> str:
            r, g, bv = colorsys.hsv_to_rgb(h / 360, s / 100, b / 100)
            return f"{int(r * 255):02x}{int(g * 255):02x}{int(bv * 255):02x}"

        palette_hex = "".join(
            _rrggbb(float(c["hue"]), float(c["saturation"]), float(c["brightness"]))
            for c in raw_palette
        )
        return {
            "uuid":    str(e.get("uuid") or ""),
            "name":    str(e.get("effect_name") or ""),
            "palette": palette_hex,
        }

    @staticmethod
    def _write_scenes(scenes_path: Path, effects: list[Effect]) -> None:
        scenes_path.parent.mkdir(parents=True, exist_ok=True)
        with scenes_path.open("w") as f:
            json.dump({"effects": effects}, f, indent=2, ensure_ascii=False)
            f.write("\n")

    def build_scenes(
        self,
        scenes_path: Path,
        *,
        save_raw: bool = False,
        progress_cb: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Fetch all Nanoleaf cloud effects and write scenes.json.

        Fetches with both sort=top and sort=recent (featured + community each)
        for full catalogue coverage. Deduplicates by UUID and sorts by name.

        Args:
            scenes_path: Path to write scenes.json.
            save_raw:    If True, also write raw API responses to
                         <label>_raw.json files in the same directory.
            progress_cb: Called with each progress message string.
                         Defaults to _LOGGER.info when None.

        Returns:
            Dict with keys ``effects_count`` and ``duplicates_removed``.
        """
        _progress = progress_cb or _LOGGER.info

        effects: list[Effect] = []
        seen: set[str] = set()

        _progress("Fetching effects …")
        for sort in ("top", "recent"):
            for featured in (True, False):
                label = ("featured" if featured else "community") + "_" + sort
                raw_items: list[RawEffect] = []
                page = 1
                for page_items in self.iter_effect_pages(featured=featured, sort=sort):
                    if page_items is None:
                        _progress(f"  {label} page {page} ... 403 — end of results")
                        break
                    _progress(f"  {label} page {page} ... {len(page_items)} items")
                    page += 1
                    raw_items.extend(page_items)
                    for e in page_items:
                        norm = self.normalise_effect(e)
                        if norm and norm["uuid"] not in seen:
                            seen.add(norm["uuid"])
                            effects.append(norm)
                    self._write_scenes(scenes_path, effects)
                    if not page_items:
                        break
                if save_raw:
                    raw_path = scenes_path.parent / f"{label}_raw.json"
                    with raw_path.open("w") as f:
                        json.dump(raw_items, f, indent=2, ensure_ascii=False)
                        f.write("\n")
                    _progress(f"  raw saved → {raw_path.name} ({len(raw_items)} items)")

        effects, duplicates_removed = self._dedup_effects(effects, _progress)

        effects.sort(key=lambda e: (e.get("name") or "").casefold())
        self._write_scenes(scenes_path, effects)
        _progress(f"  {len(effects)} effects with palette")
        _progress(f"Written {scenes_path}")

        return {"effects_count": len(effects), "duplicates_removed": duplicates_removed}

    @staticmethod
    def _dedup_effects(
        effects: list[Effect],
        _progress: Callable[[str], None],
    ) -> tuple[list[Effect], int]:
        """Remove duplicate effects by UUID; return (deduped, count_removed)."""
        uuid_count: dict[str, int] = {}
        for e in effects:
            uuid_count[e["uuid"]] = uuid_count.get(e["uuid"], 0) + 1
        duplicates = {uid: n for uid, n in uuid_count.items() if n > 1}
        if not duplicates:
            _progress("  No UUID duplicates.")
            return effects, 0
        _progress(f"  WARNING: {len(duplicates)} duplicate UUID(s) found — removing extras:")
        for uid, n in duplicates.items():
            name = next(e["name"] for e in effects if e["uuid"] == uid)
            _progress(f"    {uid}  {name!r}  ({n}x)")
        seen_final: set[str] = set()
        deduped: list[Effect] = []
        for e in effects:
            if e["uuid"] not in seen_final:
                seen_final.add(e["uuid"])
                deduped.append(e)
        return deduped, len(effects) - len(deduped)

    def iter_effect_pages(
        self, featured: bool, sort: str
    ) -> Generator[list[dict[str, Any]] | None]:
        """Yield one page of raw effect dicts at a time until the endpoint is exhausted.

        Stops on an empty data array or a 403 (hard server page cap).
        Intermediate short pages are not treated as terminators.
        """
        page = 1
        while True:
            payload: dict[str, Any] = {
                "page":     page,
                "items":    self.page_size,
                "featured": featured,
                "sort":     sort,
            }
            data = self.post_effects_search(payload)
            if data is None:
                yield None  # signals 403 end-of-results to caller
                return
            items = data.get("data", [])
            yield items
            if not items:
                return
            page += 1


# ---------------------------------------------------------------------------
# Firmware API
# ---------------------------------------------------------------------------

_FIRMWARE_BASE = "https://firmware.nanoleaf.me"


class NanoleafFirmwareApi:
    """Client for the Nanoleaf smartdownload firmware API."""

    def __init__(self, timeout: int = 15) -> None:
        self.timeout = timeout

    def check_update(self, device: dict[str, Any]) -> dict[str, Any]:
        """POST device info to smartdownload; 404 means no update available."""
        url = f"{_FIRMWARE_BASE}/smartdownload"
        r = requests.post(url, json=device, timeout=self.timeout)
        if r.status_code not in (200, 404):
            r.raise_for_status()
        return r.json()

    def download(self, url: str, dest: str) -> None:
        """Stream firmware binary from url to dest, printing progress."""
        r = requests.get(url, timeout=self.timeout * 4, stream=True)
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        received = 0
        with Path(dest).open("wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
                received += len(chunk)
                if total:
                    _LOGGER.debug("%d/%d bytes (%.0f%%)", received, total, 100 * received / total)
        _LOGGER.info("Saved %d bytes to %s", received, dest)
