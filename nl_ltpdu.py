"""
nl_ltpdu.py — LTPDU (CoAP/LTPDU) protocol implementation for Nanoleaf devices.
"""

import asyncio
import colorsys
import ipaddress
import json
import logging
import re
import struct
from pathlib import Path
from typing import Any, cast

import aiocoap
from cryptography.hazmat.primitives import ciphers, hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from zeroconf import IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

# Models confirmed to use the legacy "og" key derivation.
_LEGACY_MODELS: frozenset[str] = frozenset({"NL45", "NL55", "NL58", "NL62"})


# mDNS discovery

_LINK_RE = re.compile(r"<([^>]+)>")

class LtpduDiscovery:
    """mDNS discovery, device identification, and config persistence for Nanoleaf light devices."""

    SERVICE_TYPE = "_ltpdu._udp.local."
    _KNOWN_PATHS: list[str] = ["/nlsecure", "/nlltpdu", "/nlpublic"]

    @staticmethod
    async def _fetch_service_info(
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        results: list[dict[str, Any]],
    ) -> None:
        info = AsyncServiceInfo(service_type, name)
        await info.async_request(zeroconf, 3000)
        if not info.addresses_by_version:
            return

        # parsed_addresses() returns '?' as a placeholder when the A/AAAA
        # record has not resolved yet — filter those out before appending.
        addresses = [a for a in info.parsed_addresses() if a != "?"]
        if not addresses:
            return

        port = cast(int, info.port)

        properties: dict[str, str] = {}
        for k, v in info.properties.items():
            key = k.decode("utf-8", errors="replace") if isinstance(k, bytes) else k
            val = v.decode("utf-8", errors="replace").rstrip("\x00") if isinstance(v, bytes) else v
            properties[key] = val

        short_id = None
        if info.server:
            parts = info.server.rstrip(".").split("-")
            if len(parts) >= 3:
                short_id = parts[-1]

        results.append({
            "name": name,
            "service_type": service_type,
            "addresses": addresses,
            "port": port,
            "server": info.server,
            "short_id": short_id,
            "properties": properties,
        })

    @staticmethod
    async def _scan_mdns(service_type: str, timeout: float) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        pending: list[asyncio.Task[Any]] = []
        seen_names: set[str] = set()

        def on_change(
            zeroconf: Zeroconf,
            service_type: str,
            name: str,
            state_change: ServiceStateChange,
        ) -> None:
            # IPVersion.All fires Added once per address family for the same
            # service instance name — deduplicate so we only fetch info once.
            if state_change is ServiceStateChange.Added and name not in seen_names:
                seen_names.add(name)
                task = asyncio.create_task(
                    LtpduDiscovery._fetch_service_info(zeroconf, service_type, name, results)
                )
                pending.append(task)

        # IPVersion.All returns both v4 and v6 addresses.  Callers should
        # prefer the ULA (fd…) IPv6 address when available — Thread border
        # router routing only works over IPv6, and ULA addresses are stable
        # across reboots unlike link-local (fe80…) ones.
        zc = AsyncZeroconf(ip_version=IPVersion.All)
        browser = AsyncServiceBrowser(zc.zeroconf, [service_type], handlers=[on_change])

        try:
            await asyncio.sleep(timeout)

            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            await browser.async_cancel()
            await zc.async_close()

        return results

    @staticmethod
    async def scan(timeout: float = 5.0) -> list[dict[str, Any]]:
        """Browse for _ltpdu._udp.local. services and return a list of discovered device records."""
        return await LtpduDiscovery._scan_mdns(LtpduDiscovery.SERVICE_TYPE, timeout)

    @staticmethod
    def _friendly_name(record: dict[str, Any]) -> str | None:
        name = record.get("name", "")
        svc = "." + LtpduDiscovery.SERVICE_TYPE
        if name.endswith(svc):
            return name[: -len(svc)]
        dot = name.find(".")
        return name[:dot] if dot != -1 else name or None

    @staticmethod
    def identify(record: dict[str, Any]) -> dict[str, str | None]:
        """Extract device identity fields from a discovered mDNS record.

        Returns a dict with keys: name, model, eui64, device_id, firmware.
        Missing fields are None.
        """
        props = record.get("properties", {})
        return {
            "name": LtpduDiscovery._friendly_name(record),
            "model": props.get("md"),
            "eui64": props.get("eui64"),
            "device_id": props.get("id"),
            "firmware": props.get("srcvers"),
        }


# CoAP URI helper


def _coap_uri(ip: str, port: int, path: str) -> str:
    """Build a CoAP URI, wrapping IPv6 addresses in brackets."""
    try:
        if ipaddress.ip_address(ip).version == 6:
            return f"coap://[{ip}]:{port}{path}"
    except ValueError:
        pass  # hostname — no brackets needed
    return f"coap://{ip}:{port}{path}"


# TLV helpers


def _create_tlv(tag: int, data: bytes) -> bytes:
    """Pack tag (uint16) + length (uint16) + data into a TLV byte string."""
    return struct.pack("!HH", tag, len(data)) + data


_LOGGER = logging.getLogger(__name__)


def _decode_tlv(buf: bytes) -> tuple[int, int, bytes]:
    """Unpack the first TLV from buf. Returns (tag, length, value).

    Logs a DEBUG line for the decoded TLV header.
    """
    if len(buf) < 4:
        raise ValueError(f"TLV buffer too short: {len(buf)} bytes")
    tag, length = struct.unpack_from("!HH", buf, 0)
    value = buf[4 : 4 + length]
    _LOGGER.debug(
        "decode_tlv: tag=0x%04x length=%d value=%s (buf_total=%d)",
        tag,
        length,
        value.hex(),
        len(buf),
    )
    return tag, length, value


def _parse_ci_response(resp: bytes) -> tuple[int, bytes]:
    """Parse a ``ci`` (scene control) endpoint response.

    Response layout (flat buffer after decryption)::

        TLV(0x0001, b'ci')                        # path echo
        TLV(0x0003, status_byte + inner_tlv...)   # status + optional payload

    The *inner* bytes following the status byte may contain a nested TLV
    (e.g. ``TLV(0x8703, scene_ids)`` for list_scenes) or be empty for
    write-only operations.

    Returns:
        ``(status, inner)`` where *status* is the device status byte
        (0x00 = OK) and *inner* is the bytes after the status byte inside
        the 0x0003 TLV value.

    Raises:
        ValueError if the buffer is too short or the path echo is missing.
    """
    if len(resp) < 4:
        raise ValueError(f"ci response too short: {len(resp)} bytes")
    _, plen, _ = _decode_tlv(resp)
    off = 4 + plen  # skip TLV(0x0001, b'ci') path echo
    if off + 5 > len(resp):
        raise ValueError("ci response missing status TLV (0x0003)")
    _, _, sval = _decode_tlv(resp[off:])
    if not sval:
        raise ValueError("ci status TLV has empty value")
    return sval[0], sval[1:]


def _read_0801_response(resp: bytes, endpoint: bytes) -> tuple[int, bytes]:
    """Parse the standard lb/-style GET response for an 0x0801-class endpoint.

    Response layout (same as ``lb/`` reads)::

        TLV(0x0001, endpoint)          # path echo
        TLV(0x0003, status + data)     # status byte + optional value

    Returns:
        ``(status, value)`` where *status* is the device status byte (0x00 =
        OK) and *value* is the payload bytes after the status byte.

    Raises:
        ValueError if the response is too short or malformed.
    """
    if len(resp) < 9:  # min: 4+5 for path TLV + 4+1 for status TLV
        raise ValueError(f"0x0801 GET response too short: {len(resp)} bytes")
    _, plen, _ = _decode_tlv(resp)  # skip TLV(0x0001, ep) path echo
    off = 4 + plen
    if off + 5 > len(resp):
        raise ValueError("0x0801 GET response missing status TLV (0x0003)")
    _, _, sval = _decode_tlv(resp[off:])
    if not sval:
        raise ValueError("0x0801 GET status TLV has empty value")
    return sval[0], sval[1:]


def _parse_tlv8(data: bytes) -> dict[int, bytes]:
    """Parse a TLV8 byte string (1-byte tag + 1-byte length + value).

    Returns a dict mapping each tag to its value bytes. Tags that appear
    multiple times are silently overwritten by the last occurrence.
    """
    result: dict[int, bytes] = {}
    off = 0
    while off + 2 <= len(data):
        tag = data[off]
        length = data[off + 1]
        value = data[off + 2 : off + 2 + length]
        result[tag] = value
        off += 2 + length
    return result


def palette_to_hex(colors: list[dict[str, int]]) -> str:
    """Convert a list of {hue, saturation, brightness} dicts to a concatenated rrggbb hex string.

    Repeat-expanded: each entry in *colors* produces exactly one 6-char hex chunk.
    """
    parts = []
    for c in colors:
        r, g, b = colorsys.hsv_to_rgb(c["hue"] / 360, c["saturation"] / 100, c["brightness"] / 100)
        parts.append(f"{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}")
    return "".join(parts)


def decode_palette_hex(hex_str: str) -> list[tuple[int, int, int]]:
    """Decode a concatenated rrggbb hex string into a list of (H, S, B) tuples.

    Each 6-char chunk becomes one (hue 0-360, saturation 0-100, brightness 0-100) tuple.
    """
    result = []
    for i in range(0, len(hex_str) - 5, 6):
        chunk = hex_str[i : i + 6]
        r, g, b = int(chunk[0:2], 16) / 255, int(chunk[2:4], 16) / 255, int(chunk[4:6], 16) / 255
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        result.append((round(h * 360), round(s * 100), round(v * 100)))
    return result


# Scene data parsing


def _parse_scene_data(raw: bytes) -> dict[str, Any] | None:
    """Parse the TLV1 payload returned by get_scene() into a structured dict.

    Format: TLV1(0x01, metadata) + TLV1(0x02, palette)
    TLV1 uses 1-byte tag + 1-byte length.

    metadata bytes: sceneId(1B), effectType(1B), transitTime(1B), waitTime(1B), ...
    palette bytes: count(1B), then per-color 3 packed bytes:
        bits  0-6 : brightness 0-100
        bits  7-13: saturation 0-100
        bits 14-22: hue 0-360
        bit  23   : has_repeat — if set, next byte is repeat count
    """
    if not raw:
        return None

    tlv: dict[int, bytes] = {}
    off = 0
    while off + 2 <= len(raw):
        tag = raw[off]
        length = raw[off + 1]
        tlv[tag] = raw[off + 2 : off + 2 + length]
        off += 2 + length

    result: dict[str, Any] = {}

    meta = tlv.get(0x01, b"")
    if len(meta) >= 4:
        result["effect_type"] = meta[1]
        result["transition_time"] = meta[2]
        result["wait_time"] = meta[3]

    palette_raw = tlv.get(0x02, b"")
    colors: list[dict[str, int]] = []
    if palette_raw:
        count = palette_raw[0]
        off2 = 1
        for _ in range(count):
            if off2 + 3 > len(palette_raw):
                break
            packed = (palette_raw[off2] << 16) | (palette_raw[off2 + 1] << 8) | palette_raw[off2 + 2]
            off2 += 3
            brightness = packed & 0x7F
            saturation = (packed >> 7) & 0x7F
            hue = (packed >> 14) & 0x1FF
            has_repeat = bool(packed >> 23 & 0x1)
            repeat = 0
            if has_repeat and off2 < len(palette_raw):
                repeat = palette_raw[off2]
                off2 += 1
            for _ in range(1 + repeat):
                colors.append({"hue": hue, "saturation": saturation, "brightness": brightness})

    result["palette"] = palette_to_hex(colors)
    return result


# Scene name lookup

_SCENES_PATH = Path(__file__).parent / "scenes.json"


class SceneLookup:
    """Resolve a device palette hex string to a cloud scene name and UUID.

    Build with :meth:`from_path` (synchronous, for CLI use) or
    :meth:`from_path_async` (non-blocking, for Home Assistant).
    Pass ``scenes_path`` to override the default location (next to nl_ltpdu.py).
    """

    def __init__(self, db: dict[str, Any] | None = None) -> None:
        """Build lookup indexes from a pre-loaded *db* dict (or empty if None)."""
        if db is None:
            db = {}

        self._exact: dict[str, tuple[str, str]] = {}
        self._sorted: dict[str, tuple[str, str]] = {}
        # prefix[n] maps the hex of the first n colors to (name, uuid).
        # Used when the device truncates palettes (app caps at 7 colors).
        self._prefix: dict[int, dict[str, tuple[str, str]]] = {}
        for e in db.get("effects") or []:
            name = e.get("name")
            uuid = e.get("uuid")
            pal = e.get("palette")
            if not (name and uuid and pal):
                continue
            entry = (name, uuid)
            self._exact[pal] = entry
            chunks = [pal[i : i + 6] for i in range(0, len(pal), 6)]
            self._sorted["".join(sorted(chunks))] = entry
            # Index every prefix length shorter than the full palette.
            for n in range(1, len(chunks)):
                prefix_hex = "".join(chunks[:n])
                self._prefix.setdefault(n, {}).setdefault(prefix_hex, entry)

    @classmethod
    def from_path(cls, scenes_path: Path | None = None) -> "SceneLookup":
        """Load scenes.json synchronously and return a SceneLookup.

        For use in non-async contexts (CLI, tests).  Do **not** call this
        from inside the Home Assistant event loop — use :meth:`from_path_async`.
        """
        path = scenes_path or _SCENES_PATH
        try:
            with path.open() as f:
                db: dict[str, Any] = json.load(f)
        except (OSError, json.JSONDecodeError):
            db = {}
        return cls(db)

    @classmethod
    async def from_path_async(cls, scenes_path: Path | None = None) -> "SceneLookup":
        """Load scenes.json in a thread executor and return a SceneLookup.

        Safe to call from inside the Home Assistant event loop.
        """
        import asyncio
        path = scenes_path or _SCENES_PATH

        def _load() -> dict[str, Any]:
            try:
                with path.open() as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                return {}

        db = await asyncio.get_event_loop().run_in_executor(None, _load)
        return cls(db)

    def resolve(self, palette_hex: str) -> tuple[str, str, str] | None:
        """Return (name, uuid, method) for *palette_hex*, or None if not found.

        *method* is ``"exact"``, ``"sorted"``, or ``"prefix"``
        (device palette is a truncated prefix of the stored cloud palette).
        """
        if palette_hex in self._exact:
            name, uuid = self._exact[palette_hex]
            return name, uuid, "exact"
        sorted_pal = "".join(sorted(palette_hex[i : i + 6] for i in range(0, len(palette_hex), 6)))
        if sorted_pal in self._sorted:
            name, uuid = self._sorted[sorted_pal]
            return name, uuid, "sorted"
        n_colors = len(palette_hex) // 6
        bucket = self._prefix.get(n_colors)
        if bucket and palette_hex in bucket:
            name, uuid = bucket[palette_hex]
            return name, uuid, "prefix"
        return None


# LTPDU session


class SessionExpiredError(RuntimeError):
    """Raised when the device signals session invalidation.

    This happens when the device's AES-CTR position has drifted (e.g. after
    an idle timeout) and it can no longer decrypt our requests.  It signals
    this by sending a plaintext TLV 0x01F1 error response instead of the
    expected encrypted payload.

    Callers should catch this and call ``LtpduSession.reauth()`` to recover.
    """


class LtpduSession:
    """Holds state for one authenticated LTPDU session with a device.

    Do not construct directly — use LtpduSession.connect() (high-level) or
    LtpduSession.kex() (low-level, skips CoRE discovery).

    Lifecycle:
        First pairing::

            session = await LtpduSession.connect(ip, port, model=model)
            token = await session.pair(pin)   # returns 8-byte token; does NOT store it
            await session.auth(token)         # stores token for reauth()

        Subsequent connections (token already stored)::

            session = await LtpduSession.connect(ip, port, model=model)
            await session.auth(token)

        Session expiry recovery::

            try:
                result = await session.query_light_state()
            except SessionExpiredError:
                await session.reauth()        # requires auth() to have been called first
                result = await session.query_light_state()

        Note: ``pair()`` returns the token but does **not** call ``auth()``
        internally.  You must call ``auth(token)`` after ``pair()`` to enable
        ``reauth()`` and to begin sending control commands.

    Thread/task safety:
        A single AES-CTR cipher context is shared for all requests.  The
        counter position must advance in lock-step on both ends, so only one
        request may be in flight at a time.  All public methods that touch the
        cipher hold ``self._lock`` for the entire encrypt-transmit-decrypt
        sequence.
    """

    def __init__(
        self,
        ip_address: str,
        port: int,
        coap_ctx: aiocoap.Context,
        cipher_ctx: Any,  # cryptography AES-CTR CipherContext (encryptor)
        model: str | None = None,
    ) -> None:
        self._ip = ip_address
        self._port = port
        self._coap = coap_ctx
        self._cipher = cipher_ctx
        self._model: str | None = model
        # Serialize all cipher-using operations.
        self._lock: asyncio.Lock = asyncio.Lock()
        # Set True when encrypt() was called but decrypt() was skipped (timeout).
        # The AES-CTR counter is then out of sync with the device; all further
        # requests must fail immediately so the coordinator closes the session.
        self._cipher_corrupted: bool = False
        # Stored for reauth(); set by auth().
        self._token: bytes | None = None
        # Populated by connect(); empty when only kex() was used.
        self.paths: list[str] = []

    # -- crypto helpers

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt (or decrypt) using the shared AES-CTR cipher context."""
        return self._cipher.update(plaintext)

    # decrypt is identical for CTR mode
    decrypt = encrypt

    # -- factory

    @classmethod
    async def kex(
        cls,
        ip_address: str,
        port: int,
        timeout: float = 10.0,
        retries: int = 2,
        model: str | None = None,
    ) -> "LtpduSession":
        """X25519 key exchange with the device.

        Args:
            model: Device model string from mDNS discovery (``md`` property),
                   e.g. ``"NL67"`` or ``"NL45"``.  Legacy models
                   (NL45/NL55/NL58/NL62) use ASCII label strings for KDF;
                   all others use the hardcoded 32-byte hex salts. 
                   Pass ``None`` to default to Matter KDF.

        Returns an LtpduSession ready for further encrypted exchanges.
        Raises RuntimeError on unexpected response codes or TLV tags.
        """
        our_sk = X25519PrivateKey.generate()
        our_pk_bytes = our_sk.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

        # POST our public key to /nlsecure
        coap_ctx = await aiocoap.Context.create_client_context()
        uri = _coap_uri(ip_address, port, "/nlsecure")
        payload = _create_tlv(0x0101, our_pk_bytes)
        request = aiocoap.Message(code=aiocoap.POST, payload=payload, uri=uri)

        try:
            response = await asyncio.wait_for(
                coap_ctx.request(request).response, timeout=timeout
            )
        except (TimeoutError, asyncio.TimeoutError) as e:
            await coap_ctx.shutdown()
            raise RuntimeError(f"KEX timed out after {timeout}s") from e

        if not response.code.is_successful():
            await coap_ctx.shutdown()
            raise RuntimeError(f"KEX POST returned {response.code}")

        # Extract device public key; detect plaintext error first.
        # The device can return a plaintext TLV 0x01F1 error (e.g. status 0x08
        # when it's busy or a session is being closed).  The PCAP shows the
        # desktop app retries the exact same KEX immediately and it succeeds.
        raw_kex = response.payload
        if len(raw_kex) >= 4 and struct.unpack_from("!H", raw_kex)[0] == 0x01F1:
            _, _, err_status = _decode_tlv(raw_kex)
            if retries > 0:
                await coap_ctx.shutdown()
                return await cls.kex(
                    ip_address, port, timeout=timeout, retries=retries - 1, model=model
                )
            await coap_ctx.shutdown()
            _LOGGER.debug("KEX rejected: raw_kex=%s", raw_kex.hex())
            raise RuntimeError(f"KEX rejected by device — status 0x{err_status.hex()}")

        _, _, dev_pk_bytes = _decode_tlv(raw_kex)
        dev_pk = X25519PublicKey.from_public_bytes(dev_pk_bytes)
        shared_secret = our_sk.exchange(dev_pk)

        # Derive AES key and IV from shared secret.
        if model and model.upper() in _LEGACY_MODELS:
            key_label = b"AES-NL-OPENAPI-KEY"
            iv_label = b"AES-NL-OPENAPI-IV"
        else:
            key_label = bytes.fromhex(
                "bca9e39738e2611c25d243c985d3c592ccbee330e077b89d7978f5fc785cb5e8"
            )
            iv_label = bytes.fromhex(
                "41e2e9eb5e5fa56e800e346d8c2b600742eb49839626faecdc37fb94a3a3c202"
            )

        def _sha1_derive(label: bytes) -> bytes:
            d = hashes.Hash(hashes.SHA1())  # noqa: S303 — protocol-mandated
            d.update(label + shared_secret)
            return d.finalize()[:16]

        aes_key = _sha1_derive(key_label)
        aes_iv = _sha1_derive(iv_label)

        # Single shared AES-128-CTR cipher context (encrypt == decrypt)
        cipher = ciphers.Cipher(
            ciphers.algorithms.AES(aes_key),
            ciphers.modes.CTR(aes_iv),
        )
        cipher_ctx = cipher.encryptor()

        return cls(ip_address, port, coap_ctx, cipher_ctx, model=model)

    @classmethod
    async def connect(
        cls,
        ip_address: str,
        port: int = 5683,
        *,
        model: str | None = None,
        timeout: float = 10.0,
    ) -> "LtpduSession":
        """High-level factory: KEX + CoRE resource discovery in one step.

        Equivalent to calling ``kex()`` followed by ``query_core()`` and
        storing the result in ``session.paths``.  Use this as the primary
        entry point when you need to probe a device before authentication.

        Args:
            ip_address: IPv4 or IPv6 address of the device.
            port:       CoAP port (default 5683).
            model:      Device model string from mDNS (e.g. ``"NL67"``),
                        used to select the KDF salt.  ``None`` → MATTER.
            timeout:    Timeout in seconds for both KEX and CoRE requests.

        Returns:
            LtpduSession with ``paths`` populated from ``/.well-known/core``.
        """
        session = await cls.kex(ip_address, port, timeout=timeout, model=model)
        session.paths = await session.query_core(timeout=timeout)
        return session

    # -- lifecycle

    async def pair(self, pin: str, timeout: float = 10.0) -> bytes:
        """Send PIN and receive access token.

        Args:
            pin: Pairing PIN in any format — dashes are stripped automatically
                 (e.g. "3394-532-2503" or "33945322503").

        Returns:
            8-byte access token as bytes.

        Raises:
            RuntimeError on wrong PIN (unexpected TLV tag) or CoAP error.
        """
        digits = pin.replace("-", "")
        async with self._lock:
            uri = _coap_uri(self._ip, self._port, "/nlsecure")

            # Send encrypted PIN
            payload = self.encrypt(_create_tlv(0x0103, digits.encode("ascii")))
            request = aiocoap.Message(code=aiocoap.POST, payload=payload, uri=uri)

            try:
                response = await asyncio.wait_for(
                    self._coap.request(request).response, timeout=timeout
                )
            except (TimeoutError, asyncio.TimeoutError) as e:
                raise RuntimeError(f"Pair timed out after {timeout}s") from e

            if not response.code.is_successful():
                raise RuntimeError(f"Pair POST returned {response.code}")

            # Decrypt and extract token.
            # On error the device responds with a plaintext TLV (tag 0x01f1 + status byte)
            # instead of an encrypted one. Detect this before decrypting.
            raw = response.payload
            if len(raw) >= 4:
                raw_tag = struct.unpack_from("!H", raw)[0]
                if raw_tag == 0x01F1:
                    # plaintext error response
                    _, _, status = _decode_tlv(raw)
                    _LOGGER.debug("Pair rejected: raw=%s", raw.hex())
                    raise RuntimeError(
                        f"Pair rejected by device — status 0x{status.hex()} "
                        f"(already paired? wrong PIN?)"
                    )

            tag, _, token = _decode_tlv(self.decrypt(raw))
            if tag != 0x0104:
                _LOGGER.debug("Pair unexpected tag: raw=%s", raw.hex())
                raise RuntimeError(
                    f"Pair failed — unexpected TLV tag 0x{tag:04x} "
                    f"(wrong PIN or device already paired?)"
                )

            return token

    async def _auth_locked(self, token: bytes, timeout: float) -> None:
        """Authenticate with *token* — caller must hold ``self._lock``."""
        uri = _coap_uri(self._ip, self._port, "/nlsecure")
        payload = self.encrypt(_create_tlv(0x0104, token))
        request = aiocoap.Message(code=aiocoap.POST, payload=payload, uri=uri)

        try:
            response = await asyncio.wait_for(
                self._coap.request(request).response, timeout=timeout
            )
        except (TimeoutError, asyncio.TimeoutError) as e:
            raise RuntimeError(f"Auth timed out after {timeout}s") from e

        if not response.code.is_successful():
            raise RuntimeError(f"Auth POST returned {response.code}")

        # Detect plaintext error before decrypting (device sends 0x01F1 + status
        # byte in plaintext when auth is rejected).
        raw = response.payload
        if len(raw) >= 4 and struct.unpack_from("!H", raw)[0] == 0x01F1:
            _, _, err_status = _decode_tlv(raw)
            _LOGGER.debug("Auth rejected (plaintext): raw=%s", raw.hex())
            raise RuntimeError(f"Auth rejected by device — status 0x{err_status.hex()}")

        # Auth success response is 12 bytes encrypted (4-byte TLV header +
        # 8-byte payload), NOT the 5-byte 0x01F1+status the reference code
        # assumed.  Accept any decrypted response where either:
        #   - tag is 0x01F1 and status byte is 0x00 (encrypted status), or
        #   - tag is 0x0104 (device echoes/refreshes the token), or
        #   - any other non-error tag (treat as success).
        # Only raise if we can positively identify an encrypted error.
        decrypted = self.decrypt(raw)
        tag, _, status = _decode_tlv(decrypted)
        if tag == 0x01F1 and len(status) >= 1 and status[0] != 0x00:
            _LOGGER.debug("Auth rejected (encrypted): raw=%s decrypted=%s", raw.hex(), decrypted.hex())
            raise RuntimeError(
                f"Auth rejected (encrypted error) — status 0x{status.hex()}"
            )

    async def auth(self, token: bytes, timeout: float = 10.0) -> None:
        """Authenticate with a stored access token.

        Sends the token (TLV 0x0104, encrypted) to /nlsecure and verifies the
        device confirms. Stores *token* for later use by ``reauth()``.

        Raises RuntimeError on rejection or CoAP error.
        """
        async with self._lock:
            await self._auth_locked(token, timeout)
            self._token = token

    async def reauth(self, timeout: float = 10.0) -> None:
        """Re-authenticate after session expiry.

        Performs a fresh KEX and re-authenticates with the stored token,
        replacing the current cipher context in place.  The request lock is
        held across the cipher swap and auth so no other coroutine can send
        with the new cipher before auth completes.

        Typical usage::

            try:
                result = await session.query_light_state()
            except SessionExpiredError:
                await session.reauth()
                result = await session.query_light_state()

        Raises:
            RuntimeError if no token was stored (call auth() first).
            RuntimeError if the re-auth itself fails.
        """
        if self._token is None:
            raise RuntimeError("No stored token — call auth() before reauth()")
        # Fresh KEX uses a new CoAP context; does not touch the shared cipher.
        new_sess = await LtpduSession.kex(self._ip, self._port, timeout=timeout, model=self._model)
        async with self._lock:
            old_coap = self._coap
            old_cipher = self._cipher
            self._coap = new_sess._coap
            self._cipher = new_sess._cipher
            # Authenticate with the new cipher while the lock is held so no
            # other coroutine can slip a request in between the swap and auth.
            try:
                await self._auth_locked(self._token, timeout)
            except Exception:
                # Auth failed — restore previous state and discard new context.
                self._coap = old_coap
                self._cipher = old_cipher
                await new_sess._coap.shutdown()
                raise
        # Auth succeeded — shut down the old context outside the lock.
        await old_coap.shutdown()

    async def delete_token(self, timeout: float = 10.0) -> None:
        """Revoke the current access token from the device (TLV 0x0106).

        Must be called after a successful auth(). After this the device will
        accept new PIN pairing.

        Raises RuntimeError on CoAP error.
        """
        async with self._lock:
            uri = _coap_uri(self._ip, self._port, "/nlsecure")
            payload = self.encrypt(_create_tlv(0x0106, b""))
            request = aiocoap.Message(code=aiocoap.POST, payload=payload, uri=uri)

            try:
                response = await asyncio.wait_for(
                    self._coap.request(request).response, timeout=timeout
                )
            except (TimeoutError, asyncio.TimeoutError) as e:
                raise RuntimeError(f"delete_token timed out after {timeout}s") from e

            if not response.code.is_successful():
                raise RuntimeError(f"delete_token POST returned {response.code}")

            # Detect plaintext error before consuming.
            raw = response.payload
            if len(raw) >= 4 and struct.unpack_from("!H", raw)[0] == 0x01F1:
                _, _, err_status = _decode_tlv(raw)
                raise RuntimeError(
                    f"delete_token rejected by device — status 0x{err_status.hex()}"
                )

            # consume the encrypted response to keep cipher in sync
            self.decrypt(raw)

    async def unpair(self, timeout: float = 10.0) -> None:
        """Revoke our access token on the device.

        Sends TLV 0x0106 (delete token) to /nlsecure — the device forgets our
        pairing and allows a new PIN pairing.  Call the app-layer
        ``remove_token(device_file)`` afterwards to clear the stored credential.

        Raises:
            RuntimeError if the device rejects the request or a CoAP error occurs.
        """
        await self.delete_token(timeout=timeout)

    async def send_ltpdu(self, plaintext: bytes, timeout: float = 10.0) -> bytes:
        """Encrypt *plaintext* and POST it to /nlltpdu.

        Use this after a successful auth() to send animation or control
        packets.  The payload is encrypted with the shared AES-CTR context
        before sending, and the encrypted response is decrypted and returned.

        The request lock is held for the entire encrypt-transmit-decrypt
        sequence so that concurrent callers cannot corrupt the cipher state.

        Args:
            plaintext: Raw (unencrypted) LTPDU payload bytes.
            timeout:   Per-request timeout in seconds.

        Returns:
            Decrypted response payload bytes.

        Raises:
            SessionExpiredError if the device signals session invalidation
                — call reauth() and retry.
            RuntimeError on CoAP error or timeout.
        """
        async with self._lock:
            if self._cipher_corrupted:
                raise RuntimeError("Session cipher is corrupted (previous timeout); reconnect required")
            uri = _coap_uri(self._ip, self._port, "/nlltpdu")
            request = aiocoap.Message(
                code=aiocoap.POST, payload=self.encrypt(plaintext), uri=uri
            )
            try:
                response = await asyncio.wait_for(
                    self._coap.request(request).response, timeout=timeout
                )
            except (TimeoutError, asyncio.TimeoutError) as e:
                self._cipher_corrupted = True
                raise RuntimeError(f"send_ltpdu timed out after {timeout}s") from e
            if not response.code.is_successful():
                raise RuntimeError(f"send_ltpdu POST returned {response.code}")
            raw = response.payload
            # Detect plaintext 0x01F1 error — device has reset its
            # cipher (session timeout / desync).  Raise without decrypting so
            # the caller can reauth() and retry cleanly.
            if len(raw) >= 4 and struct.unpack_from("!H", raw)[0] == 0x01F1:
                _, _, err_status = _decode_tlv(raw)
                _LOGGER.debug("send_ltpdu session expired: req=%s raw=%s", plaintext.hex(), raw.hex())
                raise SessionExpiredError(
                    f"Session expired (send_ltpdu) — device status 0x{err_status.hex()}"
                )
            return self.decrypt(raw)

    async def get_ltpdu(self, plaintext: bytes, timeout: float = 10.0) -> bytes:
        """Encrypt *plaintext* and GET it from /nlltpdu.

        Used for read/query operations (CoAP GET with encrypted payload).
        The request lock is held for the entire encrypt-transmit-decrypt
        sequence.

        Args:
            plaintext: Raw (unencrypted) LTPDU request payload bytes.
            timeout:   Per-request timeout in seconds.

        Returns:
            Decrypted response payload bytes.

        Raises:
            SessionExpiredError if the device signals session invalidation
                — call reauth() and retry.
            RuntimeError on CoAP error or timeout.
        """
        async with self._lock:
            if self._cipher_corrupted:
                raise RuntimeError("Session cipher is corrupted (previous timeout); reconnect required")
            uri = _coap_uri(self._ip, self._port, "/nlltpdu")
            request = aiocoap.Message(
                code=aiocoap.GET, payload=self.encrypt(plaintext), uri=uri
            )
            try:
                response = await asyncio.wait_for(
                    self._coap.request(request).response, timeout=timeout
                )
            except (TimeoutError, asyncio.TimeoutError) as e:
                self._cipher_corrupted = True
                raise RuntimeError(f"get_ltpdu timed out after {timeout}s") from e
            if not response.code.is_successful():
                raise RuntimeError(f"get_ltpdu GET returned {response.code}")
            raw = response.payload
            # Detect plaintext 0x01F1 error — session has expired.
            if len(raw) >= 4 and struct.unpack_from("!H", raw)[0] == 0x01F1:
                _, _, err_status = _decode_tlv(raw)
                _LOGGER.debug("get_ltpdu session expired: req=%s raw=%s", plaintext.hex(), raw.hex())
                raise SessionExpiredError(
                    f"Session expired (get_ltpdu) — device status 0x{err_status.hex()}"
                )
            return self.decrypt(raw)

    async def observe_ltpdu(
        self, plaintext: bytes, timeout: float = 10.0
    ) -> tuple[bytes, object]:
        """Start a CoAP Observe subscription on /nlltpdu (RFC 7641).

        Encrypts *plaintext*, sends a GET with Observe=0 to ``/nlltpdu``,
        awaits the initial response, decrypts it, and returns
        ``(initial_bytes, observation)`` where *observation* is a
        ``ClientObservation`` async iterable for subsequent push notifications.

        Each notification payload must be decrypted by the caller::

            initial, obs = await session.observe_ltpdu(plaintext)
            async for msg in obs:
                decrypted = session.decrypt(msg.payload)

        No other session operations should run concurrently — the shared
        AES-CTR cipher context must advance in lock-step with the device.

        Args:
            plaintext: Unencrypted LTPDU request payload.
            timeout:   Timeout for the initial response in seconds.

        Returns:
            ``(initial_decrypted, observation)`` — the first response bytes
            and the ``ClientObservation`` async iterable.

        Raises:
            SessionExpiredError if the initial response is a plaintext 0x01F1 error.
            RuntimeError if the device does not acknowledge the Observe option
                or on CoAP error / timeout.
        """
        if self._cipher_corrupted:
            raise RuntimeError("Session cipher is corrupted (previous timeout); reconnect required")
        uri = _coap_uri(self._ip, self._port, "/nlltpdu")
        msg = aiocoap.Message(
            code=aiocoap.GET,
            payload=self.encrypt(plaintext),
            uri=uri,
            observe=0,
        )
        # handle_blockwise=False is required so the returned Request has
        # the .observation attribute (BlockwiseRequest does not expose it).
        req = self._coap.request(msg, handle_blockwise=False)
        try:
            response = await asyncio.wait_for(req.response, timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError) as e:
            self._cipher_corrupted = True
            raise RuntimeError(f"observe_ltpdu timed out after {timeout}s") from e
        if not response.code.is_successful():
            raise RuntimeError(f"observe_ltpdu GET returned {response.code}")
        raw = response.payload
        if len(raw) >= 4 and struct.unpack_from("!H", raw)[0] == 0x01F1:
            _, _, err_status = _decode_tlv(raw)
            raise SessionExpiredError(
                f"Session expired (observe_ltpdu) — device status 0x{err_status.hex()}"
            )
        if req.observation is None:
            raise RuntimeError(
                "Device did not acknowledge Observe subscription "
                "(resource may not be observable)"
            )
        return self.decrypt(raw), req.observation

    async def query_core(self, timeout: float = 5.0) -> list[str]:
        """Discover available CoAP resource paths on the device via /.well-known/core.

        Reuses the session's CoAP context.  Falls back to the known LTPDU paths
        when CoRE is not supported.
        """
        uri = _coap_uri(self._ip, self._port, "/.well-known/core")
        request = aiocoap.Message(code=aiocoap.GET, uri=uri)
        try:
            response = await asyncio.wait_for(
                self._coap.request(request).response, timeout=min(timeout, 3.0)
            )
            if response.code.is_successful():
                return _LINK_RE.findall(
                    response.payload.decode("utf-8", errors="replace")
                )
        except (TimeoutError, asyncio.TimeoutError):
            pass
        return list(LtpduDiscovery._KNOWN_PATHS)

    async def query_device_info(self, timeout: float = 10.0) -> dict[str, Any]:
        """Read static device information via the ``di`` endpoint.

        Sends a GET to ``/nlltpdu`` with endpoint ``di`` and parses the fixed-
        layout response:

        Response payload (after status byte, 37 bytes total):
          - hardware_version : 10-byte ASCII (null-padded)
          - firmware_version :  8-byte ASCII (null-padded)
          - serial_number    : 11-byte ASCII
          - eui64            :  8-byte big-endian unsigned int

        Returns:
            dict with keys ``hardware_version``, ``firmware_version``,
            ``serial_number``, ``eui64`` (16-char uppercase hex string).

        Raises:
            RuntimeError on CoAP error, timeout, malformed response, or
            non-zero device status.
        """
        req = _create_tlv(0x0001, b"di") + _create_tlv(0x0002, b"")
        resp = await self.get_ltpdu(req, timeout=timeout)

        # Response layout:
        #   TLV(0x0001, b"di")          — 4 + 2 = 6 bytes
        #   TLV(0x0003, status + data)  — 4 + 1 + 37 = 42 bytes
        if len(resp) < 12:
            raise RuntimeError(f"di response too short: {len(resp)} bytes")

        # skip the 0x0001 path TLV
        _, path_len, _ = _decode_tlv(resp)
        off = 4 + path_len

        # 0x0003 status+data TLV
        _, data_len, data_val = _decode_tlv(resp[off:])
        if data_len < 1:
            raise RuntimeError("di response data empty")

        status = data_val[0]
        if status != 0x00:
            raise RuntimeError(f"di query returned status 0x{status:02x}")

        def _ascii(b: bytes) -> str:
            return b.rstrip(b"\x00").decode("ascii")

        sub = data_val[1:]

        # NL67+: sub-TLV encoded (uint8 tag + uint16 len + value)
        # Known tags: 3=hw_ver(ascii), 4=serial(ascii), 6=eui64(8B), 11=model(ascii),
        #             254=fw_ver(3B: major.minor.patch)
        fields: dict = {}
        off2 = 0
        while off2 + 3 <= len(sub):
            stag = sub[off2]
            slen = int.from_bytes(sub[off2 + 1 : off2 + 3], "big")
            if off2 + 3 + slen > len(sub):
                break
            sval = sub[off2 + 3 : off2 + 3 + slen]
            fields[stag] = sval
            off2 += 3 + slen

        if 0x03 in fields or 0xFE in fields:
            # NL67+ sub-TLV format
            hw_ver = _ascii(fields[0x03]) if 0x03 in fields else ""
            serial = _ascii(fields[0x04]) if 0x04 in fields else ""
            eui64_bytes = fields.get(0x06, b"")
            eui64 = eui64_bytes.hex().upper() if eui64_bytes else ""
            fw_raw = fields.get(0xFE, b"")
            fw_ver = ".".join(str(b) for b in fw_raw) if fw_raw else ""
        elif len(sub) >= 37:
            # NL45 fixed-layout: 10-byte hw, 8-byte fw (ascii), 11-byte serial, 8-byte eui64
            hw_ver = _ascii(sub[0:10])
            fw_ver = _ascii(sub[10:18])
            serial = _ascii(sub[18:29])
            eui64 = sub[29:37].hex().upper()
        else:
            hw_ver = fw_ver = serial = eui64 = ""

        return {
            "hardware_version": hw_ver,
            "firmware_version": fw_ver,
            "serial_number": serial,
            "eui64": eui64,
        }

    async def query_light_state(self, timeout: float = 10.0) -> dict[str, Any]:
        """Batch-read current light state from five lb endpoints.

        Sends a single GET to ``/nlltpdu`` with five concatenated endpoint
        TLV pairs::

            TLV(0x0001, b"lb/0/oo") + TLV(0x0002, b"")   # on/off  → uint16 (2 bytes)
            TLV(0x0001, b"lb/0/pb") + TLV(0x0002, b"")   # brightness → uint16 (2 bytes)
            TLV(0x0001, b"lb/0/hu") + TLV(0x0002, b"")   # hue → uint16 (2 bytes)
            TLV(0x0001, b"lb/0/sa") + TLV(0x0002, b"")   # saturation → uint16 (2 bytes)
            TLV(0x0001, b"lb/0/ct") + TLV(0x0002, b"")   # color temp → uint32 (4 bytes)

        Responses arrive concatenated in the same order. Each pair is::

            TLV(0x0001, path) + TLV(0x0003, status_byte + value_bytes)

        Returns:
            dict with keys:
              - ``power``       : bool  — True = on
              - ``brightness``  : int   — 0-100
              - ``hue``         : int   — 0-360
              - ``saturation``  : int   — 0-100
              - ``color_temp``  : int   — color temperature in Kelvin

        .. note:: **NL67 LTPDU value widths**

            ``power`` (``lb/0/oo``) returns a 2-byte big-endian uint16 (not
            1 byte).  ``color_temp`` (``lb/0/ct``) returns a 4-byte big-endian
            uint32.  All other endpoints return 2-byte uint16 values.
            Parse with ``int.from_bytes(val, "big")`` rather than assuming a
            fixed 2-byte width.

        Raises:
            RuntimeError on CoAP error, timeout, or unexpected response structure.
        """
        endpoints = [b"lb/0/oo", b"lb/0/pb", b"lb/0/hu", b"lb/0/sa", b"lb/0/ct"]
        req = b"".join(
            _create_tlv(0x0001, ep) + _create_tlv(0x0002, b"") for ep in endpoints
        )
        resp = await self.get_ltpdu(req, timeout=timeout)

        # Parse concatenated response pairs:
        #   TLV(0x0001, path) + TLV(0x0003, status + data)
        def _parse_next(buf: bytes, off: int, ep: bytes) -> tuple[bytes, int]:
            """Return (value_bytes, new_offset) for one endpoint pair."""
            if off + 4 > len(buf):
                raise RuntimeError(f"query_light_state: truncated at offset {off}")
            tag, plen, _ = _decode_tlv(buf[off:])
            if tag != 0x0001:
                raise RuntimeError(
                    f"query_light_state: expected 0x0001 path tag, got 0x{tag:04x}"
                )
            off += 4 + plen
            if off + 4 > len(buf):
                raise RuntimeError(f"query_light_state: missing 0x0003 tag for {ep!r}")
            tag, dlen, dval = _decode_tlv(buf[off:])
            if tag != 0x0003:
                raise RuntimeError(
                    f"query_light_state: expected 0x0003 data tag for {ep!r},"
                    f" got 0x{tag:04x}"
                )
            off += 4 + dlen
            if not dval:
                raise RuntimeError(f"query_light_state: empty 0x0003 for {ep!r}")
            status = dval[0]
            if status != 0x00:
                raise RuntimeError(
                    f"query_light_state: {ep!r} returned status 0x{status:02x}"
                )
            return dval[1:], off

        off = 0
        values: dict[bytes, bytes] = {}
        for ep in endpoints:
            val, off = _parse_next(resp, off, ep)
            values[ep] = val

        def _u16(b: bytes) -> int:
            return int.from_bytes(b[:2], "big")

        def _uint(b: bytes) -> int:
            return int.from_bytes(b, "big")

        return {
            "power": bool(_uint(values[b"lb/0/oo"])),
            "brightness": _u16(values[b"lb/0/pb"]),
            "hue": _u16(values[b"lb/0/hu"]),
            "saturation": _u16(values[b"lb/0/sa"]),
            "color_temp": _uint(values[b"lb/0/ct"]),
        }

    # -- Write light parameters

    def _check_write_status(self, resp: bytes, label: str) -> None:
        """Check the 0x0003 status byte in a write response; raise on error."""
        if len(resp) < 4:
            _LOGGER.debug("%s: response too short: resp=%s", label, resp.hex())
            raise RuntimeError(f"{label}: response too short ({len(resp)} bytes)")
        tag, plen, _ = _decode_tlv(resp)
        off = 4 + plen  # skip 0x0001 path echo
        if off + 4 > len(resp):
            _LOGGER.debug("%s: truncated response: resp=%s", label, resp.hex())
            raise RuntimeError(f"{label}: response missing status TLV (truncated at {off})")
        tag, _, dval = _decode_tlv(resp[off:])
        if tag != 0x0003:
            _LOGGER.debug("%s: unexpected tag 0x%04x: resp=%s", label, tag, resp.hex())
            raise RuntimeError(f"{label}: expected status TLV 0x0003, got 0x{tag:04x}")
        if not dval:
            _LOGGER.debug("%s: empty status TLV: resp=%s", label, resp.hex())
            raise RuntimeError(f"{label}: status TLV is empty")
        if dval[0] != 0x00:
            _LOGGER.debug("%s: status error 0x%02x: resp=%s", label, dval[0], resp.hex())
            raise RuntimeError(f"{label} returned status 0x{dval[0]:02x}")

    async def set_power(self, on: bool, timeout: float = 10.0) -> None:
        """Turn the light on or off.

        Args:
            on:      True = on, False = off.
            timeout: Per-request timeout in seconds.

        Raises:
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        value = b"\x01" if on else b"\x00"
        req = _create_tlv(0x0001, b"lb/0/oo") + _create_tlv(0x0002, value)
        resp = await self.send_ltpdu(req, timeout=timeout)
        self._check_write_status(resp, "set_power")

    async def set_brightness(self, brightness: int, timeout: float = 10.0) -> None:
        """Set brightness.

        Args:
            brightness: 0-100.
            timeout:    Per-request timeout in seconds.

        Raises:
            ValueError if brightness is out of range.
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        if not 0 <= brightness <= 100:
            raise ValueError(f"brightness must be 0-100, got {brightness}")
        value = brightness.to_bytes(2, "big")
        req = _create_tlv(0x0001, b"lb/0/pb") + _create_tlv(0x0002, value)
        resp = await self.send_ltpdu(req, timeout=timeout)
        self._check_write_status(resp, "set_brightness")

    async def set_color(
        self,
        hue: int,
        saturation: int,
        timeout: float = 10.0,
    ) -> None:
        """Set hue and saturation in a single atomic POST.

        Args:
            hue:        0-360 degrees.
            saturation: 0-100 percent.
            timeout:    Per-request timeout in seconds.

        Raises:
            ValueError if values are out of range.
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        if not 0 <= hue <= 360:
            raise ValueError(f"hue must be 0-360, got {hue}")
        if not 0 <= saturation <= 100:
            raise ValueError(f"saturation must be 0-100, got {saturation}")
        req = (
            _create_tlv(0x0001, b"lb/0/hu")
            + _create_tlv(0x0002, hue.to_bytes(2, "big"))
            + _create_tlv(0x0001, b"lb/0/sa")
            + _create_tlv(0x0002, saturation.to_bytes(2, "big"))
        )
        resp = await self.send_ltpdu(req, timeout=timeout)
        self._check_write_status(resp, "set_color(hu)")

    async def set_color_temp(self, kelvin: int, timeout: float = 10.0) -> None:
        """Set color temperature.

        Args:
            kelvin:  Color temperature in Kelvin (1200-6500).
            timeout: Per-request timeout in seconds.

        Raises:
            ValueError if kelvin is out of range.
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        if not 1200 <= kelvin <= 6500:
            raise ValueError(f"color temperature must be 1200-6500 K, got {kelvin}")
        # lb/0/ct write is 2-byte uint16 even though the device returns 4 bytes on read.
        value = kelvin.to_bytes(2, "big")
        req = _create_tlv(0x0001, b"lb/0/ct") + _create_tlv(0x0002, value)
        resp = await self.send_ltpdu(req, timeout=timeout)
        self._check_write_status(resp, "set_color_temp")

    async def set_light_state(
        self,
        *,
        on: bool | None = None,
        brightness: int | None = None,
        hue: int | None = None,
        saturation: int | None = None,
        color_temp: int | None = None,
        timeout: float = 10.0,
    ) -> None:
        """Batch-write any combination of light parameters atomically.

        Only the keyword arguments that are not ``None`` are sent. All supplied
        values are packed into a single POST to ``/nlltpdu``.

        Args:
            on:          True = on, False = off. Omit to leave unchanged.
            brightness:  0-100.  Omit to leave unchanged.
            hue:         0-360.  Omit to leave unchanged.
            saturation:  0-100.  Omit to leave unchanged.
            color_temp:  1200-6500 K.  Omit to leave unchanged.
            timeout:     Per-request timeout in seconds.

        Raises:
            ValueError if any supplied value is out of range or nothing is set.
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        parts: list[bytes] = []

        if on is not None:
            parts += [
                _create_tlv(0x0001, b"lb/0/oo"),
                _create_tlv(0x0002, b"\x01" if on else b"\x00"),
            ]
        if brightness is not None:
            if not 0 <= brightness <= 100:
                raise ValueError(f"brightness must be 0-100, got {brightness}")
            parts += [
                _create_tlv(0x0001, b"lb/0/pb"),
                _create_tlv(0x0002, brightness.to_bytes(2, "big")),
            ]
        if hue is not None:
            if not 0 <= hue <= 360:
                raise ValueError(f"hue must be 0-360, got {hue}")
            parts += [
                _create_tlv(0x0001, b"lb/0/hu"),
                _create_tlv(0x0002, hue.to_bytes(2, "big")),
            ]
        if saturation is not None:
            if not 0 <= saturation <= 100:
                raise ValueError(f"saturation must be 0-100, got {saturation}")
            parts += [
                _create_tlv(0x0001, b"lb/0/sa"),
                _create_tlv(0x0002, saturation.to_bytes(2, "big")),
            ]
        if color_temp is not None:
            if not 1200 <= color_temp <= 6500:
                raise ValueError(f"color_temp must be 1200-6500 K, got {color_temp}")
            parts += [
                _create_tlv(0x0001, b"lb/0/ct"),
                _create_tlv(0x0002, color_temp.to_bytes(2, "big")),  # write=2B, read=4B
            ]

        if not parts:
            raise ValueError("set_light_state called with no parameters to set")

        resp = await self.send_ltpdu(b"".join(parts), timeout=timeout)
        self._check_write_status(resp, "set_light_state")

    # -- Scene management (ci endpoint)

    def _ci_inner(self, inner: bytes, expected_tag: int) -> bytes:
        """Extract data from an inner TLV inside a ci response.

        If *inner* starts with a TLV whose tag matches *expected_tag*, return
        its value. Otherwise return *inner* as-is (for forward-compatibility
        when the tag is unknown).
        """
        if len(inner) >= 4:
            itag, _, idata = _decode_tlv(inner)
            if itag == expected_tag:
                return idata
        return inner

    async def list_scenes(self, timeout: float = 10.0) -> bytes:
        """List all scene identifiers.

        Sends tag ``0x0703`` (ListScene) via the ``ci`` endpoint.

        Returns:
            Raw bytes from the device (``TLV(0x8703, ...)`` inner value).
            Each byte is a separate 1-byte scene handle.  Iterate with
            ``for b in list_scenes(): ...`` and call ``get_scene(bytes([b]))``
            for each handle.  Empty bytes if no scenes are stored.  Note that
            the device may include stale handles for already-deleted scenes;
            ``get_scene()`` will raise RuntimeError for those.

        Raises:
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        req = _create_tlv(0x0001, b"ci") + _create_tlv(0x0002, _create_tlv(0x0703, b""))
        resp = await self.send_ltpdu(req, timeout=timeout)
        status, inner = _parse_ci_response(resp)
        if status != 0x00:
            raise RuntimeError(f"list_scenes returned status 0x{status:02x}")
        return self._ci_inner(inner, 0x8703)

    async def get_scene(self, scene_id: bytes, timeout: float = 10.0) -> dict[str, Any] | None:
        """Get scene details for a given scene ID.

        Args:
            scene_id: 1-byte scene handle, e.g. ``bytes([0xfb])``.
                      Use a single byte from :meth:`list_scenes`.
            timeout:  Per-request timeout in seconds.

        Returns:
            Parsed scene dict with keys ``effect_type``, ``transition_time``,
            ``wait_time``, and ``palette`` (concatenated rrggbb hex string),
            or None if the raw payload is empty.

        Raises:
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        req = _create_tlv(0x0001, b"ci") + _create_tlv(
            0x0002, _create_tlv(0x0704, scene_id)
        )
        resp = await self.send_ltpdu(req, timeout=timeout)
        status, inner = _parse_ci_response(resp)
        if status != 0x00:
            raise RuntimeError(f"get_scene returned status 0x{status:02x}")
        return _parse_scene_data(self._ci_inner(inner, 0x8704))

    async def play_scene(self, scene_id: bytes, timeout: float = 10.0) -> None:
        """Execute (play) a scene.

        Args:
            scene_id: 1-byte scene handle, e.g. ``bytes([0xfb])``.
            timeout:  Per-request timeout in seconds.

        Raises:
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        req = _create_tlv(0x0001, b"ci") + _create_tlv(
            0x0002, _create_tlv(0x0706, scene_id)
        )
        resp = await self.send_ltpdu(req, timeout=timeout)
        status, _ = _parse_ci_response(resp)
        if status != 0x00:
            raise RuntimeError(f"play_scene returned status 0x{status:02x}")

    async def preview_scene(self, scene_data: bytes, timeout: float = 10.0) -> None:
        """Preview a scene without persisting it.

        Sends tag ``0x0701`` (DISPLAY_SCENE) with *scene_data*.  The scene
        data format is the same as returned by :meth:`get_scene`:
        ``TLV1(0x01, metadata) + TLV1(0x02, palette)``.

        Args:
            scene_data: Raw scene data bytes.
            timeout:    Per-request timeout in seconds.

        Raises:
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        req = _create_tlv(0x0001, b"ci") + _create_tlv(
            0x0002, _create_tlv(0x0701, scene_data)
        )
        resp = await self.send_ltpdu(req, timeout=timeout)
        status, _ = _parse_ci_response(resp)
        if status != 0x00:
            raise RuntimeError(f"preview_scene returned status 0x{status:02x}")

    async def add_scene(self, scene_data: bytes, timeout: float = 10.0) -> bytes:
        """Add (persist) a new scene.

        Sends tag ``0x0702`` (ADD_SCENE) with *scene_data*.  The scene data
        format is ``TLV1(0x01, metadata) + TLV1(0x02, palette)`` where TLV1
        uses 1-byte type and 1-byte length.  metadata bytes: [sceneId(1B),
        effectType(1B), transitTime(1B), waitTime(1B), optionalByte...].

        Args:
            scene_data: Raw scene data bytes.
            timeout:    Per-request timeout in seconds.

        Returns:
            Binary scene ID assigned by the device (``TLV(0x8702, ...)``), or
            raw inner bytes if the tag is unexpected.

        Raises:
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        req = _create_tlv(0x0001, b"ci") + _create_tlv(
            0x0002, _create_tlv(0x0702, scene_data)
        )
        resp = await self.send_ltpdu(req, timeout=timeout)
        status, inner = _parse_ci_response(resp)
        if status != 0x00:
            raise RuntimeError(f"add_scene returned status 0x{status:02x}")
        return self._ci_inner(inner, 0x8702)

    async def delete_scene(self, scene_id: bytes, timeout: float = 10.0) -> None:
        """Delete a scene.

        Args:
            scene_id: 1-byte scene handle, e.g. ``bytes([0xfb])``.
            timeout:  Per-request timeout in seconds.

        Raises:
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        req = _create_tlv(0x0001, b"ci") + _create_tlv(
            0x0002, _create_tlv(0x0705, scene_id)
        )
        resp = await self.send_ltpdu(req, timeout=timeout)
        status, _ = _parse_ci_response(resp)
        if status != 0x00:
            raise RuntimeError(f"delete_scene returned status 0x{status:02x}")

    async def get_current_scene(self, timeout: float = 10.0) -> bytes:
        """Get the currently executing scene ID.

        Returns:
            1-byte scene handle of the currently playing scene (``TLV(0x8707,
            ...)`` inner value), or empty bytes if no scene is active.

        Raises:
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        req = _create_tlv(0x0001, b"ci") + _create_tlv(0x0002, _create_tlv(0x0707, b""))
        resp = await self.send_ltpdu(req, timeout=timeout)
        status, inner = _parse_ci_response(resp)
        if status != 0x00:
            raise RuntimeError(f"get_current_scene returned status 0x{status:02x}")
        return self._ci_inner(inner, 0x8707)

    # -- Thread network info (0x0801 read wrapper)

    async def _read_0801(self, endpoint: bytes, timeout: float) -> tuple[int, bytes]:
        """Send a read request for an 0x0801-class endpoint.

        Uses the same CoAP GET + ``TLV(0x0001, ep) + TLV(0x0002, b"")`` format
        as lb/ reads.  The response is parsed the same way: skip the path echo
        TLV(0x0001) and extract status + value from TLV(0x0003).

        Note: despite being called "0x0801-class", reads use the standard GET
        format (NOT the 0x0801 POST wrapper). The 0x0801 wrapper is for writes
        only (RW=1). Hardware-verified on NL67.

        Returns (status, value_bytes). status=0x00 means OK.
        """
        req = _create_tlv(0x0001, endpoint) + _create_tlv(0x0002, b"")
        resp = await self.get_ltpdu(req, timeout=timeout)
        return _read_0801_response(resp, endpoint)

    async def query_thread_capabilities(self, timeout: float = 10.0) -> dict[str, Any]:
        """Read Thread node capabilities via ``th/nc`` (0x0801 read).

        Returns:
            Dict with keys:

            - ``raw`` (int): raw 1-byte bitfield value
            - ``minimal`` (bool): bit 0x01
            - ``sleepy`` (bool): bit 0x02
            - ``full`` (bool): bit 0x04
            - ``router_eligible`` (bool): bit 0x08
            - ``border_router_capable`` (bool): bit 0x10

        Raises:
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        status, val = await self._read_0801(b"th/nc", timeout)
        if status != 0x00:
            raise RuntimeError(
                f"query_thread_capabilities returned status 0x{status:02x}"
            )
        if not val:
            raise RuntimeError("query_thread_capabilities: empty value")
        caps = val[0]
        return {
            "raw": caps,
            "minimal": bool(caps & 0x01),
            "sleepy": bool(caps & 0x02),
            "full": bool(caps & 0x04),
            "router_eligible": bool(caps & 0x08),
            "border_router_capable": bool(caps & 0x10),
        }

    async def query_thread_role(self, timeout: float = 10.0) -> dict[str, Any]:
        """Read Thread role via ``th/tr`` (0x0801 read).

        Returns:
            Dict with keys:

            - ``raw`` (int): raw 1-byte bitfield value
            - ``disabled`` (bool): bit 0x01
            - ``detached`` (bool): bit 0x02
            - ``joining`` (bool): bit 0x04
            - ``child`` (bool): bit 0x08
            - ``router`` (bool): bit 0x10
            - ``leader`` (bool): bit 0x20
            - ``border_router`` (bool): bit 0x40

        Raises:
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        status, val = await self._read_0801(b"th/tr", timeout)
        if status != 0x00:
            raise RuntimeError(f"query_thread_role returned status 0x{status:02x}")
        if not val:
            raise RuntimeError("query_thread_role: empty value")
        role = val[0]
        return {
            "raw": role,
            "disabled": bool(role & 0x01),
            "detached": bool(role & 0x02),
            "joining": bool(role & 0x04),
            "child": bool(role & 0x08),
            "router": bool(role & 0x10),
            "leader": bool(role & 0x20),
            "border_router": bool(role & 0x40),
        }

    async def query_thread_network_info(self, timeout: float = 10.0) -> dict[str, Any]:
        """Read Thread Operational Dataset via ``th/ds``.

        On Matter devices (NL67) the Thread network info is exposed at
        ``th/ds`` (MatterThreadControl). Non-Matter devices use ``th/tc``
        which returns status 0x04 (INVALID_ENDPOINT) on NL67.

        ``th/ds`` returns the full OpenThread Active Operational Dataset
        encoded as MeshCoP TLV (1-byte type, 1-byte length, value):

        - 0x00: Channel params - 3 bytes: page(1B) + channel(2B big-endian)
        - 0x01: PAN ID - 2 bytes big-endian
        - 0x02: Extended PAN ID - 8 bytes
        - 0x03: Network Name - up to 16-byte ASCII
        - 0x04: PSKc - 16 bytes
        - 0x05: Network Key (master key) - 16 bytes
        - 0x06: Mesh Local Prefix - 8-byte IPv6 prefix (may be absent)
        - 0x07: Mesh Local Prefix - 8-byte IPv6 prefix (OpenThread tag variant)
        - 0x07: Steering Data
        - 0x0c: Security Policy - 4 bytes
        - 0x0e: Channel Mask - 8 bytes
        - 0x35: Active Timestamp - 8 bytes

        The response outer TLV contains a 2-byte big-endian status followed
        by the raw TDS bytes (no extra nesting).

        Returns:
            Dict with keys: ``status``, ``raw_tds``, ``network_name``,
            ``channel``, ``pan_id``, ``extended_pan_id``, ``network_key``,
            ``mesh_local_prefix``, ``pskc``.

        Raises:
            RuntimeError on CoAP error, timeout, or unparseable response.
        """
        status, val = await self._read_0801(b"th/ds", timeout)
        if status != 0x00:
            return {
                "status": status,
                "raw_tds": val,
                "network_name": None,
                "channel": None,
                "pan_id": None,
                "extended_pan_id": None,
                "network_key": None,
                "mesh_local_prefix": None,
                "pskc": None,
            }
        # val is the raw Thread Operational Dataset (MeshCoP TLV: 1B type + 1B len + value)
        fields = _parse_tlv8(val)
        channel = None
        if 0x00 in fields and len(fields[0x00]) >= 3:
            channel = int.from_bytes(fields[0x00][1:3], "big")
        return {
            "status": status,
            "raw_tds": val,
            "network_name": fields.get(0x03, b"").decode("ascii", errors="replace")
            or None,
            "channel": channel,
            "pan_id": fields.get(0x01, b"").hex() or None,
            "extended_pan_id": fields.get(0x02, b"").hex() or None,
            "network_key": fields.get(0x05, b"").hex() or None,
            "mesh_local_prefix": fields.get(0x07, b"").hex() or None,
            "pskc": fields.get(0x04, b"").hex() or None,
        }

    async def identify(self, timeout: float = 10.0) -> None:
        """Trigger the device to blink for physical identification.

        Sends endpoint ``lb/0/id`` via the authenticated ``/nlltpdu`` channel.
        Must be called after a successful ``auth()``.

        Args:
            timeout: Per-request timeout in seconds.

        Raises:
            RuntimeError on CoAP error, timeout, or non-zero device status.
        """
        payload = _create_tlv(0x0001, b"lb/0/id") + _create_tlv(0x0002, b"\x01")
        resp = await self.send_ltpdu(payload, timeout=timeout)
        self._check_write_status(resp, "identify")

    async def close(self) -> None:
        """Shut down the underlying CoAP context."""
        await self._coap.shutdown()
