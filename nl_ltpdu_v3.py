"""Local LTPDUv3 authentication, encryption, transport, and controls."""

# ruff: noqa: ASYNC109 -- timeout matches the legacy public API.

from __future__ import annotations

import asyncio
import binascii
import colorsys
import hashlib
import hmac
import json
import math
import secrets
import struct
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

if __package__:
    from .nl_ltpdu import AuthRejectedError, SessionExpiredError
else:
    from nl_ltpdu import AuthRejectedError, SessionExpiredError


# SRP and secure-session encryption.

N512 = int(
    "d66aafe8e245f9ac245a199f62ce61ab8fa90a4d80c71cd2adfd0b9da163b29f2a34"
    "afbdb3b1b5d0102559ce63d8b6e86b0aa59c14e79d4aa62d1748e4249df3",
    16,
)
N2048 = int(
    "ac6bdb41324a9a9bf166de5e1389582faf72b6651987ee07fc3192943db56050a37329"
    "cbb4a099ed8193e0757767a13dd52312ab4b03310dcd7f48a9da04fd50e8083969edb"
    "767b0cf6095179a163ab3661a05fbd5faaae82918a9962f0b93b855f97993ec975eeaa"
    "80d740adbf4ff747359d041d5c33ea71d281e446b14773bca97b43a23fb801676bd2"
    "07a436c6481f1d2b9078717461a5b9d32e688f87748544523b524b0d57d5ea77a277"
    "5d2ecfa032cfbdbf52fb3786160279004e57ae6af874e7303ce53299ccc041c7bc308"
    "d82a5698f3a8d0c38271ae35f8e9dbfbb694b5c803d89f7ae435de236d525f54759b"
    "65e372fcd68ef20fa7111f9e4aff73",
    16,
)


def _integer_bytes(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def _hash(*parts: bytes) -> bytes:
    return hashlib.sha256(b"".join(parts)).digest()


class SRPClient:
    """SHA256 SRP-6a client using the bulb's group and integer encoding."""

    def __init__(
        self,
        username: str,
        password: bytes,
        group: int = 0,
        private_key: bytes | None = None,
    ) -> None:
        if group not in (0, 2):
            raise ValueError("Unsupported LTPDUv3 SRP group")
        self.username = username.encode("utf-8")
        self.password = password
        self.modulus = N512 if group == 0 else N2048
        size = 64 if group == 0 else 256
        self._private = int.from_bytes(
            secrets.token_bytes(size) if private_key is None else private_key, "big"
        )
        if self._private == 0:
            raise ValueError("SRP private key must not be zero")
        self.public_key = _integer_bytes(pow(2, self._private, self.modulus))
        self.shared_secret: bytes | None = None
        self.server_proof: bytes | None = None

    def process_challenge(self, salt: bytes, server_public: bytes) -> bytes:
        """Return M and retain K and H(A,M,K) for server authentication."""
        modulus = self.modulus
        server_value = int.from_bytes(server_public, "big")
        if server_value % modulus == 0:
            raise ValueError("Invalid SRP server public key")
        server_public = _integer_bytes(server_value)
        multiplier = int.from_bytes(_hash(_integer_bytes(modulus), b"\x02"), "big")
        scrambling = int.from_bytes(_hash(self.public_key, server_public), "big")
        if scrambling == 0:
            raise ValueError("Invalid SRP scrambling parameter")
        password_key = int.from_bytes(_hash(salt, _hash(self.username, b":", self.password)), "big")
        base = (server_value - multiplier * pow(2, password_key, modulus)) % modulus
        secret = pow(base, self._private + scrambling * password_key, modulus)
        self.shared_secret = _hash(_integer_bytes(secret))
        xor_hash = bytes(
            a ^ b for a, b in zip(_hash(_integer_bytes(modulus)), _hash(b"\x02"), strict=True)
        )
        proof = _hash(
            xor_hash,
            _hash(self.username),
            salt,
            self.public_key,
            server_public,
            self.shared_secret,
        )
        self.server_proof = _hash(self.public_key, proof, self.shared_secret)
        return proof

    def verify_server(self, proof: bytes) -> None:
        if self.server_proof is None or not hmac.compare_digest(self.server_proof, proof):
            raise ValueError("LTPDUv3 SRP server proof mismatch")


class SecureCipher:
    """One ordered, bidirectional secure-session cipher.

    A request and its response use successive counters. Retransmit the same
    encrypted datagram; do not encrypt retries again. CTR also retains its
    byte stream across request/response boundaries.
    """

    def __init__(self, version: int, shared_secret: bytes, counter: int = 0) -> None:
        if version not in (0x0300, 0x0301, 0x0302):
            raise ValueError("Unsupported LTPDUv3 secure version")
        if len(shared_secret) != 32:
            raise ValueError("LTPDUv3 shared secret must be 32 bytes")
        if not 0 <= counter < 2**64:
            raise ValueError("Invalid secure-session counter")
        self.version = version
        self.counter = counter
        material = _hash(shared_secret)
        key = material[:16]
        self._nonce_tail = material[16:24]
        self._stream = None
        self._ccm = None
        if version == 0x0300:
            self._stream = Cipher(
                algorithms.AES(key), modes.CTR(counter.to_bytes(8, "big") + self._nonce_tail)
            ).encryptor()
        else:
            self._ccm = AESCCM(key, tag_length=4)

    def _nonce(self) -> bytes:
        size = 5 if self.version == 0x0301 else 4
        if self.counter >= 1 << (size * 8):
            raise ValueError("LTPDUv3 secure counter exhausted; reconnect required")
        return self.counter.to_bytes(size, "big") + self._nonce_tail

    def encrypt(self, plain: bytes) -> bytes:
        if self._stream is not None:
            crc = binascii.crc_hqx(plain, 0).to_bytes(2, "big")
            wire = self._stream.update(plain + crc)
        else:
            wire = self._ccm.encrypt(self._nonce(), plain, None)
        self.counter += 1
        return wire

    def decrypt(self, wire: bytes) -> bytes:
        if self._stream is not None:
            if len(wire) < 2:
                raise ValueError("Truncated LTPDUv3 CTR payload")
            payload = self._stream.update(wire)
            plain, crc = payload[:-2], payload[-2:]
            if not hmac.compare_digest(binascii.crc_hqx(plain, 0).to_bytes(2, "big"), crc):
                raise ValueError("LTPDUv3 CRC mismatch; reconnect required")
        else:
            plain = self._ccm.decrypt(self._nonce(), wire, None)
        self.counter += 1
        return plain


# Light controls and effect decoding.


def _tlv(tag: int, data: bytes) -> bytes:
    return bytes([tag]) + len(data).to_bytes(2, "big") + data


def _fields(data: bytes, length_size: int = 2) -> list[tuple[int, bytes]]:
    """Decode bounded TLVs; reject truncated values rather than partial scenes."""
    result = []
    offset = 0
    header = 1 + length_size
    while offset < len(data):
        if len(data) - offset < header:
            raise RuntimeError("Truncated v3 TLV header")
        tag = data[offset]
        size = int.from_bytes(data[offset + 1 : offset + header], "big")
        start = offset + header
        end = start + size
        if end > len(data):
            raise RuntimeError("Truncated v3 TLV value")
        result.append((tag, data[start:end]))
        offset = end
    return result


def _uint(data: bytes, label: str, width: int | None = None) -> int:
    if not data or (width is not None and len(data) != width):
        raise RuntimeError(f"Invalid {label} value length: {len(data)}")
    return int.from_bytes(data, "big")


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").rstrip("\x00")


# Motion option names, wire types, and value formats.
_OPTIONS = {
    1: ("transTime", "u16"),
    2: ("loop", "bool"),
    3: ("delayTime", "u16"),
    4: ("linDirection", "str"),
    5: ("mainColorProb", "u8"),
    6: ("nColorsPerFrame", "u8"),
    7: ("fadeOut", "u8"),
    8: ("enableBackgroundColor", "bool"),
    9: ("blockSize", "u8"),
    10: ("streamControlVersion", "u8"),
    11: ("streamControlClientId", "u16"),
    12: ("streamControlGroupIndexing", "tlv"),
    13: ("streamControlClientIp", "str"),
    14: ("streamControlClientPort", "u16"),
    15: ("streamControlInactiveTimeout", "u16"),
}

_MOTIONS = {
    65536: "fade",
    131072: "flow",
    196608: "burst",
    262144: "random",
    327680: "highlight",
    393216: "wheel",
    458752: "explodewheel",
    524288: "juggle",
    589824: "organic",
    655360: "soundbar",
    720896: "radialsoundbar",
    786432: "checkerboard",
    851968: "fireworks",
    917504: "bounce",
    983040: "melt",
    1048576: "radiate",
    1114112: "intertwine",
    1179648: "streakingnotes",
    1245184: "rollercoaster",
    1310720: "weave",
    1376256: "heartbeat",
    1441792: "radialpulse",
    4278386688: "static",
    4278321152: "streamControl",
}


def _parse_palette(data: bytes) -> dict[str, Any]:
    fields = dict(_fields(data))
    kind = _uint(fields.get(1, b""), "palette type", 1)
    raw = fields.get(2, b"")
    width = 4 if kind == 0 else 3
    if kind not in (0, 1, 2):
        return {"type": kind, "colors": [], "raw": raw}
    if len(raw) % width:
        raise RuntimeError("Truncated v3 palette color")
    colors = []
    for offset in range(0, len(raw), width):
        part = raw[offset : offset + width]
        if kind == 0:
            colors.append({"h": int.from_bytes(part[:2], "big"), "s": part[2], "v": part[3]})
        elif kind == 1:
            colors.append({"ct": int.from_bytes(part[:2], "big"), "brightness": part[2]})
        else:
            colors.append({"r": part[0], "g": part[1], "b": part[2]})
    return {"type": ("HSV", "CT", "RGB")[kind], "colors": colors}


def _palette_hex(palette: dict[str, Any]) -> str:
    parts = []
    for color in palette["colors"]:
        if palette["type"] == "HSV":
            rgb = colorsys.hsv_to_rgb(color["h"] / 360, color["s"] / 100, color["v"] / 100)
            # Match legacy palette_to_hex for optional catalogue matching.
            parts.append("".join(f"{int(channel * 255):02x}" for channel in rgb))
        elif palette["type"] == "RGB":
            parts.append("".join(f"{color[channel]:02x}" for channel in ("r", "g", "b")))
    # CT has no exact RGB representation: preserve CT in palette_data.
    return "".join(parts)


def _parse_options(data: bytes) -> dict[str, Any]:
    # Preserve structured and unknown option bytes.
    result = {}
    for tag, element in _fields(data):
        if tag != 1:
            continue
        fields = dict(_fields(element))
        code = _uint(fields.get(2, b""), "motion option type", 1)
        name, fmt = _OPTIONS.get(code, (f"opt{code}", "raw"))
        value = fields.get(3, b"")
        if fmt == "u16":
            decoded = _uint(value, name, 2)
        elif fmt == "u8":
            decoded = _uint(value, name, 1)
        elif fmt == "bool":
            decoded = bool(_uint(value, name, 1))
        elif fmt == "str":
            decoded = _text(value)
        else:
            decoded = value
        result[name] = decoded
    return result


def _parse_static(data: bytes) -> dict[str, Any]:
    fields = dict(_fields(data))
    raw = fields.get(2, b"")
    pixels = []
    offset = index = 0
    while offset < len(raw):
        if raw[offset] == 255:
            offset += 1
        else:
            if len(raw) - offset < 3:
                raise RuntimeError("Truncated v3 static pixel")
            pixels.append(
                {"id": index, "r": raw[offset], "g": raw[offset + 1], "b": raw[offset + 2]}
            )
            offset += 3
        index += 1
    result: dict[str, Any] = {"static_data": pixels}
    if 1 in fields:
        result["static_addressing_type"] = _uint(fields[1], "static addressing type", 1)
    if 3 in fields:
        result["leds_per_zone"] = _uint(fields[3], "LEDs per zone")
    return result


def _parse_effect(data: bytes) -> dict[str, Any]:
    fields = dict(_fields(data))
    effect_id = _uint(fields.get(3, b""), "effect ID", 4)
    result: dict[str, Any] = {
        "id": effect_id.to_bytes(4, "big"),
        "effect_id": effect_id,
        "name": _text(fields.get(5, b"")),
        "fixture_ids": list(fields.get(11, b"")),
        "motion_options": _parse_options(fields.get(7, b"")),
        "raw": data,
    }
    if 4 in fields:
        motion = _uint(fields[4], "motion ID", 4)
        result["motion_id"] = motion
        result["motion_name"] = _MOTIONS.get(motion, "")
    palette = _parse_palette(fields[6]) if 6 in fields else {"type": None, "colors": []}
    result["palette_data"] = palette
    result["palette"] = _palette_hex(palette)
    if 9 in fields:
        result.update(_parse_static(fields[9]))
    if 12 in fields:
        result["duration"] = _uint(fields[12], "effect duration", 4)
    return result


def parse_effects(data: bytes) -> list[dict[str, Any]]:
    """Parse complete effectV2 effectList/effectData responses."""
    result = []
    for tag, value in _fields(data):
        if tag == 1:
            result.extend(parse_effects(value))
        elif tag == 2:
            result.append(_parse_effect(value))
    return result


_HOMEKIT_MODELS = {"NL45", "NL55", "NL58", "NL62"}
_THREAD_MODELS = {"NL53", "NL54", "NL65", "NL67", "NL68", "SQUB01", "SQUB03"}
_WIFI_MODELS = {
    "NL71K1",
    "NL71K2",
    "NL72K1",
    "NL72K3",
    "NL72K4",
    "NL72K6",
    "NL72S1",
    "NL72S2",
    "NL73K1",
    "NL73K2",
    "NL73K3",
    "NL75K1",
    "NL75K2",
    "NL75K3",
    "NL75K4",
    "NL75K5",
    "NL75K6",
    "NL77K1",
    "SQCM01",
    "SQCM02",
    "SQFX03",
    "SQFX04",
    "SQSL01",
}


def decode_thread_role(value: int, model: str | None) -> dict[str, Any]:
    """Decode Matter role enums and HomeKit role bitfields."""
    flags = ("disabled", "detached", "joining", "child", "router", "leader", "border_router")
    if (model or "").upper() in _THREAD_MODELS | _WIFI_MODELS:
        role = {0: "disabled", 1: "detached", 2: "child", 3: "router", 4: "leader"}.get(value)
        return {"raw": value, **{name: name == role for name in flags}}
    return {"raw": value, **{name: bool(value & (1 << bit)) for bit, name in enumerate(flags)}}


def _version_tuple(firmware: str | None) -> tuple[int, int, int] | None:
    if not isinstance(firmware, str):
        return None
    pieces = firmware.split(".")
    if len(pieces) != 3 or any(not part.isdigit() for part in pieces):
        return None
    return tuple(int(part) for part in pieces)


def color_temperature_range(model: str | None, firmware: str | None) -> tuple[int, int]:
    """Return known Kelvin bounds or a conservative range for unknown models."""
    model = (model or "").upper()
    special = {
        "NL72K4": 2200,
        "NL73K1": 2100,
        "NL73K3": 2200,
        "NL75K1": 2100,
        "NL75K2": 2100,
        "NL75K3": 2100,
        "NL75K4": 2100,
        "NL75K5": 2100,
    }
    if model in special:
        return special[model], 6500
    if model in _WIFI_MODELS:
        return 2700, 6500
    version = _version_tuple(firmware)
    if model in _THREAD_MODELS and version is not None and version >= (5, 0, 0):
        return 2100, 6500
    if model in _HOMEKIT_MODELS | _THREAD_MODELS:
        return 2127, 6535
    return 2700, 6500


def device_capabilities(model: str | None, firmware: str | None) -> dict[str, Any]:
    """Describe known optional endpoints without enabling features for unknown models."""
    model = (model or "").upper()
    version = _version_tuple(firmware)
    known = model in _HOMEKIT_MODELS | _THREAD_MODELS | _WIFI_MODELS
    valid = known and version is not None
    effect_v2 = valid and model in _THREAD_MODELS | _WIFI_MODELS and version >= (5, 0, 0)
    threshold = (1, 3, 17) if model in _HOMEKIT_MODELS else (3, 5, 0)
    if model in _WIFI_MODELS:
        threshold = (2, 0, 10)
    circadian = (
        valid and model not in {"NL62", "NL72S1", "NL72S2", "SQSL01"} and version >= threshold
    )
    return {
        "circadian": circadian,
        "time": valid,
        "location": valid,
        "power_loss_recovery": valid,
        "motions": valid,
        "scene_protocol": "effectV2"
        if effect_v2
        else "ciOneD"
        if model in _WIFI_MODELS
        else "ciEssentials",
        "palette_types": ["HSV"] if valid else [],
        "max_palette_size": (10 if model in _WIFI_MODELS else 7) if valid else 0,
        "max_stored_scenes": (32 if effect_v2 else 20)
        if valid and model in _WIFI_MODELS
        else 12
        if valid
        else 0,
        "supports_static_scenes": effect_v2 and model in _WIFI_MODELS,
    }


async def _settings_capabilities(session: Any, timeout: float, feature: str) -> dict[str, Any]:
    firmware = getattr(session, "_firmware_version", None)
    model = getattr(session, "_model", None)
    if firmware is None:
        info = await session.query_device_info(timeout=timeout)
        firmware = info.get("firmware_version")
        model = info.get("model_number") or model
        session._firmware_version = firmware
        session._model = model
    capabilities = device_capabilities(model, firmware)
    if not capabilities[feature]:
        raise NotImplementedError(f"{feature} is unavailable for this model and firmware")
    return capabilities


def _tlv8(tag: int, data: bytes) -> bytes:
    if len(data) > 255:
        raise ValueError("TLV8 value exceeds 255 bytes")
    return bytes([tag, len(data)]) + data


def _checked_number(value: Any, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer in {minimum}..{maximum}")
    return value


def _circadian_status(data: bytes, configuring: bool) -> None:
    if not data or data[0] == 0:
        return
    if data[0] == 2:
        message = (
            "Set the device time, timezone and location before using sunrise/sunset settings"
            if configuring
            else "Set the device clock and circadian configuration before enabling"
        )
    elif data[0] == 3:
        message = "Device rejected a circadian configuration value"
    elif data[0] == 5:
        message = "Device could not read its circadian configuration"
    else:
        message = f"Device returned circadian status {data[0]}"
    raise RuntimeError(message)


def _parse_circadian(data: bytes) -> dict[str, Any] | None:
    if not data or data == b"\x00":
        return None
    if data[0] == 0:
        data = data[1:]
    if len(data) == 1:
        raise RuntimeError(f"Circadian configuration read returned status {data[0]}")
    fields = _fields(data, length_size=1)
    config: dict[str, Any] = {"version": None}
    for tag, value in fields:
        if tag == 1:
            config["version"] = _uint(value, "circadian version", 1)
        elif tag in (2, 3):
            config["mode"] = "explicit" if tag == 3 else "sunriseSunset"
            names = (
                ("riseTimeOfDayMinutes", "fallTimeOfDayMinutes")
                if tag == 3
                else ("sunriseOffsetMinutes", "sunsetOffsetMinutes")
            ) + (
                "riseRampMinutes",
                "fallRampMinutes",
                "morningRampMinutes",
                "daylightTargetK",
                "eveningTargetK",
                "morningTargetK",
            )
            for field, raw in _fields(value, length_size=1):
                if 1 <= field <= 8:
                    number = _uint(raw, names[field - 1], 2)
                    if field <= 2 and tag == 2:
                        number = int.from_bytes(raw, "big", signed=True)
                    if field >= 6:
                        number = 1_000_000 // number if number else 0
                    config[names[field - 1]] = number
    if "mode" not in config:
        raise RuntimeError("Device returned no circadian mode")
    return config


def _encode_circadian(config: dict[str, Any], model: str | None, firmware: str | None) -> bytes:
    if not isinstance(config, dict) or config.get("mode") not in {"explicit", "sunriseSunset"}:
        raise ValueError("Circadian mode must be explicit or sunriseSunset")
    explicit = config["mode"] == "explicit"
    timing = (
        ("riseTimeOfDayMinutes", "fallTimeOfDayMinutes")
        if explicit
        else ("sunriseOffsetMinutes", "sunsetOffsetMinutes")
    )
    names = (
        *timing,
        "riseRampMinutes",
        "fallRampMinutes",
        "morningRampMinutes",
        "daylightTargetK",
        "eveningTargetK",
        "morningTargetK",
    )
    if any(name not in config for name in names):
        raise ValueError("Circadian config requires all timing, ramp and white-point fields")
    extras = set(config) - {*names, "mode", "version"}
    if extras:
        raise ValueError("Circadian config contains fields for another mode or unknown fields")
    minimum_k, maximum_k = color_temperature_range(model, firmware)
    pieces = []
    for tag, name in enumerate(names, 1):
        if tag <= 2:
            number = _checked_number(config[name], 0 if explicit else -1439, 1439, name)
        elif tag <= 5:
            number = _checked_number(config[name], 0, 65535, name)
        else:
            kelvin = _checked_number(config[name], 1200, 6535, name)
            kelvin = max(minimum_k, min(maximum_k, kelvin))
            number = (1_000_000 + kelvin // 2) // kelvin
        pieces.append(_tlv8(tag, number.to_bytes(2, "big", signed=tag <= 2 and not explicit)))
    prefix = b"" if (model or "").upper() in _HOMEKIT_MODELS else _tlv8(1, b"\x00")
    return prefix + _tlv8(3 if explicit else 2, b"".join(pieces))


def _inner_ci(data: bytes, command: int) -> bytes:
    if len(data) < 4:
        raise RuntimeError("Truncated command response")
    tag = int.from_bytes(data[:2], "big")
    size = int.from_bytes(data[2:4], "big")
    if tag not in {command, command | 0x8000} or size != len(data) - 4:
        raise RuntimeError("Invalid command response")
    return data[4:]


_OPTION_SPECS = {
    "transTime": ("int", 1, 600),
    "delayTime": ("int", 0, 600),
    "loop": ("bool", None, None),
    "linDirection": ("string", None, None),
    "mainColorProb": ("int", 0, 100),
    "nColorsPerFrame": ("int", 2, 20),
    "fadeOut": ("int", 0, 10),
    "enableBackgroundColor": ("bool", None, None),
    "blockSize": ("int", 3, 50),
}
_MOTION_DEFAULTS = {
    "bounce": {"blockSize": 5, "transTime": 400},
    "burst": {"delayTime": 24, "linDirection": "left", "transTime": 24},
    "checkerboard": {"delayTime": 24, "loop": True, "transTime": 24},
    "explodewheel": {"linDirection": "left", "nColorsPerFrame": 4, "transTime": 24},
    "fade": {"transTime": 24},
    "fireworks": {"enableBackgroundColor": False, "fadeOut": 6},
    "flow": {"delayTime": 24, "linDirection": "left", "transTime": 24},
    "heartbeat": {"delayTime": 15, "loop": False, "transTime": 24},
    "highlight": {"mainColorProb": 40, "transTime": 24},
    "intertwine": {"blockSize": 1, "fadeOut": 1, "transTime": 10},
    "juggle": {"transTime": 450},
    "melt": {"delayTime": 24, "loop": False, "transTime": 24},
    "organic": {"linDirection": "left", "transTime": 24},
    "radialpulse": {"delayTime": 24, "loop": True, "transTime": 24},
    "radialsoundbar": {},
    "radiate": {"delayTime": 24, "transTime": 24},
    "random": {"transTime": 24},
    "rollercoaster": {"enableBackgroundColor": False, "transTime": 24},
    "soundbar": {"linDirection": "left"},
    "streakingnotes": {},
    "weave": {"delayTime": 24, "loop": False, "transTime": 24},
    "wheel": {"linDirection": "left", "nColorsPerFrame": 4, "transTime": 24},
    "stripes": {"linDirection": "right", "segment": 50, "transTime": 24},
    "pulse": {"delayTime": 0, "loop": True, "transTime": 24},
    "colorpop": {"enableBackgroundColor": False, "fadeOut": 6},
}
_MOTION_UUIDS = {
    "bounce": "db8ce916-88de-484f-bdbb-afd732053105",
    "burst": "713518c1-d560-47db-8991-de780af71d1e",
    "checkerboard": "b75f1438-c37b-49c2-aa39-273bceb3d94b",
    "colorpop": "e8b69f65-df13-480b-a949-db1bbc9a1cde",
    "fade": "b3fd723a-aae8-4c99-bf2b-087159e0ef53",
    "flow": "027842e4-e1d6-4a4c-a731-be74a1ebd4cf",
    "highlight": "70b7c636-6bf8-491f-89c1-f4103508d642",
    "random": "ba632d3e-9c2b-4413-a965-510c839b3f71",
    "stripes": "7cdc7692-381a-4405-b25b-b477f116f7ca",
    "fireworks": "5cec763a-6f32-45cf-a691-b81e176b6b4a",
    "intertwine": "5c4d343f-8eca-47e9-8c01-92662dff4eb6",
    "juggle": "29929fc2-6905-41c0-a7d7-1582a8c11f17",
    "melt": "30a017d4-e6a5-4647-8c41-5a7cd38ff907",
    "organic": "1dab05d3-07bf-4648-9d48-db8dd196ed28",
    "pulse": "0794a6b3-f6cc-452d-a900-30081604b7ec",
    "radialsoundbar": "0349fc36-7727-4ebc-bc1b-a0021c55038e",
    "radiate": "e0ce975f-59e0-4d2e-872c-4856b79a36e9",
    "rollercoaster": "e2eb1818-07ac-495f-a10d-7c192d7df705",
    "soundbar": "fe0d0ba8-741f-4210-940c-454eceed010f",
    "streakingnotes": "337784c0-6c98-447a-8a9f-fc6ac44a8024",
    "weave": "ac8fb29a-333d-4de8-a95e-211cecc31ae1",
    "wheel": "6970681a-20b5-4c5e-8813-bdaebc4ee4fa",
}


def _motion_info(name: str, motion_id: int | None = None, legacy: bool = False) -> dict[str, Any]:
    defaults = dict(_MOTION_DEFAULTS.get(name, {}))
    if legacy:
        defaults.update(
            {
                "fade": {"delayTime": 0, "loop": True},
                "flow": {"loop": True},
                "highlight": {"delayTime": 15},
                "random": {"delayTime": 0},
            }.get(name, {})
        )
    options = []
    for option, default in defaults.items():
        kind, minimum, maximum = _OPTION_SPECS.get(option, ("int", 0, 100))
        if name == "intertwine" and option in {"blockSize", "fadeOut"}:
            minimum, maximum = (1, 20) if option == "blockSize" else (0, 20)
        item = {"name": option, "type": kind, "default": default}
        if minimum is not None:
            item.update({"min": minimum, "max": maximum})
        if option == "linDirection":
            item["values"] = (
                ["left", "right"] if name == "stripes" else ["left", "right", "up", "down", "none"]
            )
        options.append(item)
    result: dict[str, Any] = {
        "name": name,
        "type": "rhythm"
        if name in {"colorpop", "fireworks", "radialsoundbar", "soundbar", "streakingnotes"}
        else "color",
        "options": options,
    }
    if motion_id is not None:
        result["id"] = motion_id
    if name in _MOTION_UUIDS:
        result["uuid"] = _MOTION_UUIDS[name]
    return result


class V3Controls:
    """Legacy-compatible public control methods using the secure v3 channel."""

    def _scene_cache(self) -> dict[bytes, dict[str, Any]]:
        if not hasattr(self, "_v3_scenes"):
            self._v3_scenes: dict[bytes, dict[str, Any]] = {}
        return self._v3_scenes

    async def query_device_info(self, timeout: float = 10.0) -> dict[str, Any]:
        # Tag 6 contains the EUI-64 address.
        fields = dict(_fields(await self._request("di", timeout=timeout)))
        firmware = fields.get(254, b"")
        model = _text(fields.get(11, b"")).upper()
        info = {
            "hardware_version": _text(fields.get(3, b"")),
            "firmware_version": ".".join(map(str, firmware))
            if len(firmware) == 3
            else _text(firmware),
            "serial_number": _text(fields.get(4, b"")),
            "eui64": fields.get(6, b"").hex().upper(),
            "device_code": _text(fields.get(10, b"")),
            "model_number": model if model.startswith("NL") else "NL" + model if model else "",
        }
        self._firmware_version = info["firmware_version"]
        if info["model_number"]:
            self._model = info["model_number"]
        return info

    async def query_light_state(self, timeout: float = 10.0) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, suffix in (
            ("power", "oo"),
            ("brightness", "pb"),
            ("hue", "hu"),
            ("saturation", "sa"),
            ("color_temp", "ct"),
        ):
            value = _uint(await self._request(f"lb/0/{suffix}", timeout=timeout), suffix)
            result[key] = bool(value) if key == "power" else value
        mode = await self._request("lb/0/cm", timeout=timeout)
        if len(mode) == 1:
            result["color_mode"] = {0: "hs", 1: "xy", 2: "ct", 4: "effect"}.get(mode[0], "hs")
        else:
            result["color_mode"] = _text(mode)
        return result

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
        # POST encodes power as uint8 and numeric controls as uint16.
        writes = []
        if on is not None:
            writes.append(("oo", bytes([1 if on else 0])))
        minimum_k, maximum_k = color_temperature_range(
            getattr(self, "_model", None), getattr(self, "_firmware_version", None)
        )
        for suffix, value, maximum, minimum in (
            ("pb", brightness, 100, 0),
            ("hu", hue, 360, 0),
            ("sa", saturation, 100, 0),
            ("ct", color_temp, maximum_k, minimum_k),
        ):
            if value is not None:
                if not minimum <= value <= maximum:
                    raise ValueError(f"{suffix} must be {minimum}-{maximum}, got {value}")
                writes.append((suffix, value.to_bytes(2, "big")))
        if not writes:
            raise ValueError("set_light_state called with no parameters to set")
        for suffix, value in writes:
            await self._request(f"lb/0/{suffix}", value, method=1, timeout=timeout)

    async def set_power(self, on: bool, timeout: float = 10.0) -> None:
        await self.set_light_state(on=on, timeout=timeout)

    async def set_brightness(self, brightness: int, timeout: float = 10.0) -> None:
        await self.set_light_state(brightness=brightness, timeout=timeout)

    async def set_color(self, hue: int, saturation: int, timeout: float = 10.0) -> None:
        await self.set_light_state(hue=hue, saturation=saturation, timeout=timeout)

    async def set_hue_sat(self, hue: int, saturation: int, timeout: float = 10.0) -> None:
        await self.set_color(hue, saturation, timeout=timeout)

    async def set_color_temp(self, kelvin: int, timeout: float = 10.0) -> None:
        await self.set_light_state(color_temp=kelvin, timeout=timeout)

    async def identify(self, timeout: float = 10.0) -> None:
        await self._request("lb/0/id", b"\x01", method=1, timeout=timeout)

    async def list_scenes(self, timeout: float = 10.0) -> list[bytes]:
        raw = await self._request("effect/ids", timeout=timeout)
        fields = _fields(raw)
        if not fields:
            self._scene_cache().clear()
            return []
        tag, ids = fields[0]
        if tag != 8 or len(ids) % 4:
            raise RuntimeError("Invalid v3 effect/ids response")
        result = [ids[offset : offset + 4] for offset in range(0, len(ids), 4)]
        cache = self._scene_cache()
        for cached in list(cache):
            if cached not in result:
                del cache[cached]
        return result

    async def get_scene(self, scene_id: bytes, timeout: float = 10.0) -> dict[str, Any] | None:
        # GET effect expects tag 8 with uint32 IDs.
        if len(scene_id) != 4:
            raise ValueError("v3 scene ID must contain four bytes")
        raw = await self._request("effect", _tlv(8, scene_id), timeout=timeout)
        scenes = parse_effects(raw)
        cache = self._scene_cache()
        cache.update((scene["id"], scene) for scene in scenes)
        return next((scene for scene in scenes if scene["id"] == scene_id), None)

    async def get_scene_name(self, scene_id: bytes, timeout: float = 10.0) -> str | None:
        scene = self._scene_cache().get(scene_id)
        if scene is None:
            scene = await self.get_scene(scene_id, timeout=timeout)
        return scene["name"] if scene else None

    async def play_scene(self, scene_id: bytes, timeout: float = 10.0) -> None:
        # Scene activation uses the UTF-8 name.
        name = await self.get_scene_name(scene_id, timeout=timeout)
        if not name:
            raise RuntimeError("v3 scene not found or has no name")
        await self._request("lb/0/effect/select", name.encode("utf-8"), method=1, timeout=timeout)

    async def get_current_scene(self, timeout: float = 10.0) -> bytes:
        # Ignore NUL padding in the selected scene name.
        name = _text(await self._request("lb/0/effect/select", timeout=timeout)).replace("\x00", "")
        if not name:
            return b""
        for scene_id, scene in self._scene_cache().items():
            if scene["name"] == name:
                return scene_id
        for scene_id in await self.list_scenes(timeout=timeout):
            if await self.get_scene_name(scene_id, timeout=timeout) == name:
                return scene_id
        return b""

    async def query_capabilities(self, timeout: float = 10.0) -> dict[str, Any]:
        if getattr(self, "_firmware_version", None) is None:
            info = await self.query_device_info(timeout=timeout)
            self._firmware_version = info.get("firmware_version")
            self._model = info.get("model_number") or getattr(self, "_model", None)
        return device_capabilities(getattr(self, "_model", None), self._firmware_version)

    async def query_circadian_settings(self, timeout: float = 10.0) -> dict[str, Any]:
        capabilities = await _settings_capabilities(self, timeout, "circadian")
        endpoint = "lb/0/ac/en" if capabilities["scene_protocol"] == "effectV2" else "ac/en"
        enabled = await self._request(endpoint, timeout=timeout)
        if len(enabled) == 2 and enabled[0] == 0:
            enabled = enabled[1:]
        flag = _uint(enabled, "circadian enabled", 1)
        if flag not in (0, 1):
            _circadian_status(enabled, False)
        try:
            raw = await self._request("ac", timeout=timeout)
            config = _parse_circadian(raw)
        except (SessionExpiredError, TimeoutError, OSError):
            raise
        except (RuntimeError, ValueError) as exc:
            if getattr(self, "_cipher_corrupted", False) or getattr(self, "_broken", False):
                raise
            return {"config": None, "enabled": bool(flag), "config_error": str(exc)}
        return {"config": config, "enabled": bool(flag)}

    async def set_circadian_config(self, config: dict[str, Any], timeout: float = 10.0) -> None:
        await _settings_capabilities(self, timeout, "circadian")
        payload = _encode_circadian(config, self._model, self._firmware_version)
        response = await self._request("ac", payload, method=1, timeout=timeout)
        _circadian_status(response, True)

    async def set_circadian_enabled(self, enabled: bool, timeout: float = 10.0) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("Circadian enabled must be a boolean")
        capabilities = await _settings_capabilities(self, timeout, "circadian")
        endpoint = "lb/0/ac/en" if capabilities["scene_protocol"] == "effectV2" else "ac/en"
        response = await self._request(endpoint, bytes([int(enabled)]), method=1, timeout=timeout)
        _circadian_status(response, False)
        if enabled:
            await self.set_power(True, timeout=timeout)

    async def query_device_time(self, timeout: float = 10.0) -> dict[str, int]:
        await _settings_capabilities(self, timeout, "time")
        epoch = await self._request("tm", timeout=timeout)
        zone = await self._request("tz", timeout=timeout)
        return {
            "epoch_seconds": _uint(epoch, "device time", 8),
            "timezone_offset_seconds": int.from_bytes(zone, "big", signed=True)
            if len(zone) == 8
            else _uint(zone, "device timezone", 8),
        }

    async def set_device_time(
        self, epoch_seconds: int, timezone_offset_seconds: int | None = None, timeout: float = 10.0
    ) -> None:
        epoch = _checked_number(epoch_seconds, 0, (1 << 64) - 1, "epoch_seconds")
        zone = None
        if timezone_offset_seconds is not None:
            zone = _checked_number(
                timezone_offset_seconds, -(1 << 63), (1 << 63) - 1, "timezone_offset_seconds"
            )
        await _settings_capabilities(self, timeout, "time")
        await self._request("tm", epoch.to_bytes(8, "big"), method=1, timeout=timeout)
        if zone is not None:
            await self._request(
                "tz", zone.to_bytes(8, "big", signed=True), method=1, timeout=timeout
            )

    async def query_device_location(self, timeout: float = 10.0) -> dict[str, float]:
        await _settings_capabilities(self, timeout, "location")
        raw = await self._request("lglt", timeout=timeout)
        if len(raw) != 8:
            raise RuntimeError("Invalid device location length")
        longitude, latitude = struct.unpack(">ii", raw)
        return {"latitude": latitude / 1000, "longitude": longitude / 1000}

    async def set_device_location(
        self, latitude: float, longitude: float, timeout: float = 10.0
    ) -> None:
        for value, label, maximum in ((latitude, "latitude", 90), (longitude, "longitude", 180)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not -maximum <= value <= maximum
            ):
                raise ValueError(f"{label} must be finite and in {-maximum}..{maximum}")
        await _settings_capabilities(self, timeout, "location")
        payload = struct.pack(
            ">ii", math.floor(longitude * 1000 + 0.5), math.floor(latitude * 1000 + 0.5)
        )
        await self._request("lglt", payload, method=1, timeout=timeout)

    async def query_power_loss_recovery(self, timeout: float = 10.0) -> bool:
        capabilities = await _settings_capabilities(self, timeout, "power_loss_recovery")
        if capabilities["scene_protocol"] == "effectV2":
            raw = await self._request("lb/0/plr", timeout=timeout)
            if not raw or raw[0] not in (1, 2):
                raise RuntimeError("Invalid power-loss recovery state")
            return raw[0] == 2
        request = b"\x09\x02\x00\x00"
        raw = _inner_ci(await self._request("ci", request, method=1, timeout=timeout), 0x0902)
        if len(raw) != 1 or raw[0] not in (0, 1):
            raise RuntimeError("Invalid power-loss recovery state")
        return raw[0] == 1

    async def set_power_loss_recovery(self, enabled: bool, timeout: float = 10.0) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("Power-loss recovery enabled must be a boolean")
        capabilities = await _settings_capabilities(self, timeout, "power_loss_recovery")
        if capabilities["scene_protocol"] == "effectV2":
            await self._request(
                "lb/0/plr", bytes([2 if enabled else 1, 0]), method=1, timeout=timeout
            )
        else:
            raw = await self._request(
                "ci", b"\x09\x01\x00\x01" + bytes([int(enabled)]), method=1, timeout=timeout
            )
            if raw:
                value = _inner_ci(raw, 0x0901)
                if value and any(value):
                    raise RuntimeError("Device rejected power-loss recovery setting")

    async def query_motion_capabilities(self, timeout: float = 10.0) -> dict[str, Any]:
        capabilities = await _settings_capabilities(self, timeout, "motions")
        motions = []
        if capabilities["scene_protocol"] == "effectV2":
            data = _fields(await self._request("effect/motions", timeout=timeout))
            if data:
                tag, identifiers = data[0]
                if tag != 10 or len(identifiers) % 4:
                    raise RuntimeError("Invalid motion list")
                for offset in range(0, len(identifiers), 4):
                    motion_id = int.from_bytes(identifiers[offset : offset + 4], "big")
                    name = _MOTIONS.get(motion_id)
                    if name in _MOTION_DEFAULTS:
                        motions.append(_motion_info(name, motion_id))
                    else:
                        motions.append(
                            {
                                "id": motion_id,
                                "name": name or f"Motion 0x{motion_id:08x}",
                                "type": "unknown",
                                "options": [],
                            }
                        )
        elif capabilities["scene_protocol"] == "ciOneD":
            response = await self._request("ci", b"\x01\x07\x00\x00", method=1, timeout=timeout)
            plugins = json.loads(_inner_ci(response, 0x0107))
            if not isinstance(plugins, list) or any(
                not isinstance(plugin, str) for plugin in plugins
            ):
                raise RuntimeError("Invalid motion plugin list")
            uuids = {uuid: name for name, uuid in _MOTION_UUIDS.items()}
            for plugin in plugins:
                name = uuids.get(plugin.lower())
                motions.append(
                    _motion_info(name, legacy=True)
                    if name
                    else {"uuid": plugin, "name": plugin, "type": "unknown", "options": []}
                )
        else:
            baseline = (
                ["fade", "flow", "highlight", "random", "stripes"]
                if self._model == "NL62"
                else ["fade", "highlight", "random"]
            )
            motions = [_motion_info(name, legacy=True) for name in baseline]
        return {
            "motions": motions,
            "motion_source": "profile"
            if capabilities["scene_protocol"] == "ciEssentials"
            else "device",
            **{
                key: capabilities[key]
                for key in (
                    "palette_types",
                    "max_palette_size",
                    "max_stored_scenes",
                    "supports_static_scenes",
                    "scene_protocol",
                )
            },
        }

    async def _thread_flags(
        self, endpoint: str, flags: tuple[str, ...], timeout: float
    ) -> dict[str, Any]:
        value = _uint(await self._request(endpoint, timeout=timeout), endpoint, 1)
        return {"raw": value, **{name: bool(value & (1 << bit)) for bit, name in enumerate(flags)}}

    async def query_thread_capabilities(self, timeout: float = 10.0) -> dict[str, Any]:
        return await self._thread_flags(
            "th/nc",
            ("minimal", "sleepy", "full", "router_eligible", "border_router_capable"),
            timeout,
        )

    async def query_thread_role(self, timeout: float = 10.0) -> dict[str, Any]:
        value = _uint(await self._request("th/tr", timeout=timeout), "th/tr", 1)
        return decode_thread_role(value, getattr(self, "_model", None))

    async def query_thread_network_info(self, timeout: float = 10.0) -> dict[str, Any]:
        raw = await self._request("th/ds", timeout=timeout)
        fields = dict(_fields(raw, length_size=1))
        channel = fields.get(0, b"")
        return {
            "status": 0,
            "raw_tds": raw,
            "network_name": _text(fields.get(3, b"")) or None,
            "channel": int.from_bytes(channel[1:3], "big") if len(channel) >= 3 else None,
            "pan_id": fields.get(1, b"").hex() or None,
            "extended_pan_id": fields.get(2, b"").hex() or None,
            "network_key": fields.get(5, b"").hex() or None,
            "mesh_local_prefix": fields.get(7, fields.get(6, b"")).hex() or None,
            "pskc": fields.get(4, b"").hex() or None,
        }


# UDP transport and authenticated session.

_ENDPOINTS = {"nlsecure": 1, "nlltpdu": 2, "nlevents": 3, "nlidentify": 4, "nlinfo": 5}
_CREDENTIAL_PREFIX = b"NLV3\x01"


def _operation_tlv(tag: int, value: bytes) -> bytes:
    return struct.pack("!HH", tag, len(value)) + value


def _operation_response(data: bytes, endpoint: str) -> bytes:
    offset = 0
    echoed = None
    response = None
    while offset < len(data):
        if len(data) - offset < 4:
            raise RuntimeError("Truncated v3 operation header")
        tag, size = struct.unpack_from("!HH", data, offset)
        offset += 4
        if offset + size > len(data):
            raise RuntimeError("Truncated v3 operation value")
        value = data[offset : offset + size]
        offset += size
        if tag == 1:
            echoed = value.decode("utf-8")
        elif tag == 3:
            if response is not None or echoed != endpoint or not value:
                raise RuntimeError("Invalid v3 operation response")
            if value[0] != 0:
                raise RuntimeError(f"v3 endpoint {endpoint} returned status {value[0]}")
            response = value[1:]
    if response is None:
        raise RuntimeError("Missing v3 operation response")
    return response


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.pending: dict[int, asyncio.Future[tuple[int, bytes, int]]] = {}

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if len(data) < 12:
            return
        version, kind, message_id, client_id, status, size = struct.unpack_from("!BBHHHI", data)
        if version != 1 or kind != 2 or len(data) != 12 + size:
            return
        future = self.pending.get(message_id)
        if future is not None and not future.done():
            future.set_result((status, data[12:], client_id))

    def error_received(self, exc: Exception) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self.error_received(exc or ConnectionError("LTPDUv3 transport closed"))


class LtpduV3Session(V3Controls):
    """Serialized UDP/SRP session; a failed encrypted exchange requires reauth."""

    def __init__(
        self,
        transport: asyncio.DatagramTransport,
        protocol: _Datagrams,
        *,
        name: str | None = None,
        model: str | None = None,
    ) -> None:
        self._transport = transport
        self._protocol = protocol
        self._name = name
        self._model = model
        self._firmware_version: str | None = None
        self._group = 2 if model in {"NL45", "NL55", "NL58", "NL62"} else 0
        self._client_id = 0xFFFF
        self._cipher: SecureCipher | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._broken = False
        self.token: bytes | None = None
        self.paths = ["nlsecure", "nlltpdu", "nlinfo", "nlidentify"]

    @classmethod
    async def connect(
        cls,
        ip_address: str,
        port: int = 12566,
        *,
        model: str | None = None,
        name: str | None = None,
        timeout: float = 10.0,
    ) -> LtpduV3Session:
        """Probe custom UDP framing before committing to a secure handshake."""
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            _Datagrams, remote_addr=(ip_address, port)
        )
        session = cls(transport, protocol, name=name, model=model)
        try:
            # A valid error response also identifies this protocol. GET is read-only.
            await session._exchange("nlinfo", b"", method=0, timeout=min(timeout, 2), attempts=1)
            return session
        except BaseException:
            await session.close()
            raise

    async def _exchange(
        self,
        endpoint: str,
        payload: bytes,
        *,
        method: int = 1,
        timeout: float = 10.0,
        attempts: int = 4,
    ) -> tuple[int, bytes, int]:
        if self._closed:
            raise ConnectionError("LTPDUv3 session closed")
        message_id = secrets.randbelow(65536)
        while message_id in self._protocol.pending:
            message_id = (message_id + 1) & 0xFFFF
        packet = (
            struct.pack(
                "!BBHHBBI",
                1,
                1,
                message_id,
                self._client_id,
                _ENDPOINTS[endpoint],
                method,
                len(payload),
            )
            + payload
        )
        future = asyncio.get_running_loop().create_future()
        self._protocol.pending[message_id] = future
        try:
            for attempt in range(attempts):
                # Resend the identical ciphertext and ID, never advance cipher on retry.
                self._transport.sendto(packet)
                try:
                    return await asyncio.wait_for(asyncio.shield(future), timeout)
                except TimeoutError:
                    if attempt == attempts - 1:
                        raise TimeoutError(f"LTPDUv3 {endpoint} timed out") from None
            raise RuntimeError("No UDP attempts configured")
        finally:
            self._protocol.pending.pop(message_id, None)
            if not future.done():
                future.cancel()

    async def _checked_exchange(self, endpoint: str, payload: bytes, timeout: float) -> bytes:
        status, data, client_id = await self._exchange(endpoint, payload, timeout=timeout)
        if status == 3:
            raise AuthRejectedError("LTPDUv3 credentials rejected")
        if status not in (0, 1):
            raise RuntimeError(f"LTPDUv3 {endpoint} returned status {status}")
        self._client_id = client_id
        return data

    async def _handshake(
        self,
        username: str,
        password: bytes,
        auth_type: int,
        timeout: float,
    ) -> None:
        self._cipher = None
        self._broken = True
        srp = SRPClient(username, password, self._group)
        first = _tlv(1, bytes([auth_type])) + _tlv(3, srp.public_key) + _tlv(7, b"\x02")
        fields = dict(_fields(await self._checked_exchange("nlsecure", first, timeout)))
        version_bytes = fields.get(17, b"\x03\x00")
        if len(version_bytes) != 2 or 2 not in fields or 3 not in fields:
            raise RuntimeError("Invalid LTPDUv3 SRP challenge")
        version = int.from_bytes(version_bytes, "big")
        proof = srp.process_challenge(fields[2], fields[3])
        fields = dict(_fields(await self._checked_exchange("nlsecure", _tlv(4, proof), timeout)))
        try:
            srp.verify_server(fields.get(4, b""))
        except ValueError as exc:
            raise AuthRejectedError("LTPDUv3 server authentication failed") from exc
        self._cipher = SecureCipher(version, srp.shared_secret)
        self._broken = False

    async def _encrypted(self, endpoint: str, payload: bytes, timeout: float) -> bytes:
        if self._cipher is None or self._broken:
            raise SessionExpiredError("LTPDUv3 secure session needs authentication")
        try:
            wire = self._cipher.encrypt(payload)
            response = await self._checked_exchange(endpoint, wire, timeout)
            return self._cipher.decrypt(response)
        except BaseException:
            # Timeout/cancellation/bad ciphertext makes shared counter position uncertain.
            self._broken = True
            raise

    async def pair(self, pin: str, timeout: float = 10.0) -> bytes:
        if not self._name:
            raise RuntimeError("LTPDUv3 pairing requires the bulb's advertised name")
        username = self._name.split("-", 1)[0]
        digits = pin.replace("-", "").strip()
        if not digits or any(char not in "0123456789" for char in digits):
            raise ValueError("Pairing code must contain decimal digits")
        async with self._lock:
            await self._handshake(username, bytes(int(char) for char in digits), 1, timeout)
            fields = dict(_fields(await self._encrypted("nlsecure", _tlv(5, b""), timeout)))
            native_token = fields.get(6)
            if not native_token:
                raise RuntimeError("Missing LTPDUv3 access token")
            credential = {
                "username": username,
                "client_id": self._client_id,
                "auth_token": native_token.hex(),
                "group": self._group,
            }
            self.token = _CREDENTIAL_PREFIX + json.dumps(
                credential,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            return self.token

    async def auth(self, token: bytes, timeout: float = 10.0) -> None:
        if not token.startswith(_CREDENTIAL_PREFIX):
            raise AuthRejectedError(
                "Stored token belongs to the legacy handshake; pair again for v3"
            )
        try:
            data = json.loads(token[len(_CREDENTIAL_PREFIX) :])
            username = data["username"]
            client_id = data["client_id"]
            group = data["group"]
            native_token = bytes.fromhex(data["auth_token"])
            if (
                not isinstance(username, str)
                or not username
                or type(client_id) is not int
                or not 0 <= client_id <= 65535
                or group not in (0, 2)
                or not native_token
            ):
                raise ValueError("Invalid credential fields")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AuthRejectedError("Invalid stored LTPDUv3 credential") from exc
        async with self._lock:
            self._client_id = client_id
            self._group = group
            await self._handshake(username, native_token, 2, timeout)
            self.token = token

    async def reauth(self, timeout: float = 10.0) -> None:
        if self.token is None:
            raise AuthRejectedError("No stored LTPDUv3 credentials")
        await self.auth(self.token, timeout=timeout)

    async def _request(
        self,
        endpoint: str,
        payload: bytes = b"",
        method: int = 0,
        timeout: float = 10.0,
    ) -> bytes:
        request = (
            _operation_tlv(1, endpoint.encode("utf-8"))
            + _operation_tlv(2, payload)
            + _operation_tlv(4, bytes([method]))
        )
        async with self._lock:
            response = await self._encrypted("nlltpdu", request, timeout)
            return _operation_response(response, endpoint)

    async def unpair(self, timeout: float = 10.0) -> None:
        async with self._lock:
            # Unpair accepts an empty success response.
            if self._cipher is None or self._broken:
                raise SessionExpiredError("LTPDUv3 session needs authentication")
            try:
                response = await self._checked_exchange(
                    "nlsecure", self._cipher.encrypt(_tlv(9, b"")), timeout
                )
                if response:
                    self._cipher.decrypt(response)
            finally:
                self._broken = True
            self.token = None
            self._cipher = None

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._protocol.connection_lost(None)
            self._transport.close()
