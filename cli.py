"""
cli.py — Nanoleaf LTPDU command-line app.
"""

import argparse
import asyncio
from pathlib import Path
from typing import Any
import datetime
import json
import logging
import os
import socket
import sys

from nl_api import NanoleafCloudApi, NanoleafFirmwareApi
from nl_ltpdu import (
    LtpduDiscovery,
    LtpduSession,
    SceneLookup,
    SessionExpiredError,
)

DEFAULT_CONF = "bulb.json"

def save_credential(device_file: str, key: str, value: object) -> None:
    """Write a top-level credential field into the device JSON config."""
    with open(device_file) as f:
        config = json.load(f)
    config[key] = value
    with open(device_file, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def load_credential(device_file: str, key: str):
    """Read a top-level credential field from the device JSON config. Returns None if missing."""
    try:
        with open(device_file) as f:
            config = json.load(f)
        return config.get(key)
    except (OSError, json.JSONDecodeError):
        return None


def save_device_field(device_file: str, key: str, value: object) -> None:
    """Write a top-level field into the device JSON config."""
    with open(device_file) as f:
        config = json.load(f)
    config[key] = value
    with open(device_file, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def remove_token(device_file: str) -> None:
    """Remove the ``token`` key from a device JSON config (step 15.b)."""
    try:
        with open(device_file) as f:
            config = json.load(f)
        config.pop("token", None)
        with open(device_file, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
    except OSError:
        pass


def _find_existing_config(config_dir: str, eui64: str | None, ip_address: str | None) -> str | None:
    """Find an existing device config by EUI-64 in filename or IP address in content."""
    if eui64:
        for fname in os.listdir(config_dir):
            if fname.endswith(".json") and eui64.lower() in fname.lower():
                return os.path.join(config_dir, fname)
    if ip_address:
        for fname in os.listdir(config_dir):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(config_dir, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("ip_address") == ip_address:
                return fpath
    return None


def persist_device(record: dict[str, Any], config_dir: str = ".") -> str:
    """Persist a discovered mDNS record to a JSON config file.

    Stores only name, model, network. Updates an existing config without
    touching pin or token. Creates nanoleaf_{model}_{eui64}.json if new.
    Returns the path to the written config file.
    """
    ident = LtpduDiscovery.identify(record)
    model_str = ident["model"] or "unknown"
    eui64 = ident["eui64"]
    ip_address = record["addresses"][0] if record.get("addresses") else None
    friendly = ident["name"]

    path = _find_existing_config(config_dir, eui64, ip_address)

    if path is not None:
        with open(path) as f:
            config = json.load(f)
        config["ip_address"] = ip_address
        config["port"] = record.get("port")
        if friendly and not config.get("name"):
            config["name"] = friendly
        if ident["model"] and not config.get("model"):
            config["model"] = ident["model"]
    else:
        filename = f"nanoleaf_{model_str.lower()}_{(eui64 or 'unknown').lower()}.json"
        path = os.path.join(config_dir, filename)
        config: dict[str, Any] = {}
        if friendly:
            config["name"] = friendly
        if ident["model"]:
            config["model"] = ident["model"]
        config["ip_address"] = ip_address
        config["port"] = record.get("port")

    with open(path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    return path


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def print_info(rows: list[tuple[str, str]], prefix: str = "  ") -> None:
    """Print label/value pairs with aligned columns."""
    if not rows:
        return
    col = max(len(label) for label, _ in rows) + 1
    for label, value in rows:
        print(f"{prefix}{label:<{col}}: {value}")


def print_section(title: str) -> None:
    print(f"\n--- {title} {'-' * max(0, 48 - len(title))}")


# ---------------------------------------------------------------------------
# Device file helpers
# ---------------------------------------------------------------------------


def load_device(path: str) -> dict[str, Any]:
    """Load and minimally validate a device JSON config file."""
    try:
        with open(path) as f:
            config = json.load(f)
    except FileNotFoundError:
        sys.exit(f"Error: device file not found: {path}")
    except json.JSONDecodeError as e:
        sys.exit(f"Error: malformed JSON in {path}: {e}")

    if not config.get("ip_address"):
        sys.exit(f"Error: {path} is missing ip_address")
    if not config.get("port"):
        sys.exit(f"Error: {path} is missing port")

    return config


async def open_session(device: dict[str, Any], timeout: float = 10.0) -> LtpduSession:
    """KEX + auth using credentials stored in device config. Raises SystemExit if not paired."""
    ip_address = device["ip_address"]
    port = device["port"]
    model = device.get("model")

    token_hex: str | None = device.get("token")
    if not token_hex:
        sys.exit("Error: device not paired — run: cli.py pair --pin <PIN>")

    session = await LtpduSession.kex(ip_address, port, model=model, timeout=timeout)
    try:
        await session.auth(bytes.fromhex(token_hex))
    except SessionExpiredError:
        await session.reauth()
    return session


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


async def cmd_discover(args: argparse.Namespace) -> None:
    """Scan for all Nanoleaf LTPDU devices on the local network via mDNS."""
    print(f"Scanning for Nanoleaf LTPDU devices ({args.timeout}s) ...")

    records = await LtpduDiscovery.scan(timeout=args.timeout)

    if not records:
        print("No devices found.")
        return

    for i, record in enumerate(records):
        ident = LtpduDiscovery.identify(record)

        name = ident["name"] or "(unknown)"
        model = ident["model"] or "?"
        eui64 = ident["eui64"] or "?"
        firmware = ident["firmware"] or "?"
        ip_address = record["addresses"][0] if record.get("addresses") else "?"
        port = record.get("port", "?")

        print(f"\n  [{i + 1}] {name}")
        print_info(
            [
                ("model", model),
                ("eui64", eui64),
                ("firmware", firmware),
                ("ip", ip_address),
                ("port", str(port)),
            ],
            prefix="      ",
        )

        if args.save:
            if not record.get("addresses"):
                print("      → skipped (no address)")
                continue
            path = persist_device(record, config_dir=".")
            print(f"      → saved to {path}")

    print(f"\nFound {len(records)} device(s).")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def cmd_find(args: argparse.Namespace) -> None:
    """Resolve an address, probe the device via CoAP, and optionally save a config."""
    address = args.address.strip("[]")

    if ":" in address:
        ip = address
    else:
        try:
            results = socket.getaddrinfo(address, None, socket.AF_INET6)
            ip = str(results[0][4][0])
        except socket.gaierror:
            try:
                results = socket.getaddrinfo(address, None, socket.AF_UNSPEC)
                ipv6_results = [r for r in results if r[0] == socket.AF_INET6]
                if not ipv6_results:
                    sys.exit(f"Error: no IPv6 address found for {address!r}")
                ip = str(ipv6_results[0][4][0])
            except socket.gaierror:
                sys.exit(f"Error: cannot resolve address {address!r}")

    print(f"Probing {ip} port {args.port} ...")
    session = await LtpduSession.connect(ip, args.port, model=args.model, timeout=5.0)
    print(f"  resources: {', '.join(session.paths)}")

    print_info([
        ("address", ip),
        ("port", str(args.port)),
        ("model", args.model or "(unknown)"),
    ])

    if args.save:
        config: dict[str, Any] = {"ip_address": ip, "port": args.port}
        if args.model:
            config["model"] = args.model
        dest = args.conf
        with open(dest, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        print(f"→ saved to {dest}")


async def cmd_pair(args: argparse.Namespace) -> None:
    """Pair with a device using its PIN, storing the token in the config file."""
    device = load_device(args.conf)
    ip_address = device["ip_address"]
    port = device["port"]
    model = device.get("model")
    pin = args.pin or device.get("pin")
    if not pin:
        sys.exit("Error: no PIN supplied — use --pin or add 'pin' to the config file")

    existing_hex: str | None = device.get("token")
    if existing_hex:
        print("Existing token found — attempting to revoke before re-pairing ...")
        try:
            session = await LtpduSession.kex(ip_address, port, model=model)
            try:
                await session.auth(bytes.fromhex(existing_hex))
                await session.delete_token()
                print("  old token revoked")
            except RuntimeError as e:
                print(f"  revoke failed ({e}) — token likely stale, continuing")
            finally:
                await session.close()
        except RuntimeError as e:
            print(f"  KEX failed ({e}) — continuing anyway")

    print(f"Pairing with PIN {pin} ...")
    session = await LtpduSession.kex(ip_address, port, model=model)
    try:
        token = await session.pair(pin)
    finally:
        await session.close()

    save_credential(args.conf, "token", token.hex())
    print(f"  token : {token.hex()}")
    print(f"  saved to {args.conf}")


async def cmd_info(args: argparse.Namespace) -> None:
    """Show full device info and current light state."""
    device = load_device(args.conf)
    session = await open_session(device)
    try:
        dev_info, light, scene_handles = await asyncio.gather(
            session.query_device_info(),
            session.query_light_state(),
            session.list_scenes(),
        )
        try:
            current_scene_id = await session.get_current_scene()
        except RuntimeError:
            current_scene_id = b""

        # fetch scene detail for every handle (sequential — single cipher lock)
        scene_detail: dict[int, dict[str, Any] | None] = {}
        for byte in scene_handles:
            try:
                scene_detail[byte] = await session.get_scene(bytes([byte]))
            except RuntimeError:
                scene_detail[byte] = None

        # fetch Thread info; tolerate unsupported endpoints
        try:
            thread_caps, thread_role, thread_net = await asyncio.gather(
                session.query_thread_capabilities(),
                session.query_thread_role(),
                session.query_thread_network_info(),
            )
        except RuntimeError:
            thread_caps = thread_role = thread_net = None
    finally:
        await session.close()

    model = device.get("model") or dev_info.get("hardware_version") or "?"
    current_scene_label = (
        f"Scene 0x{current_scene_id[0]:02x}" if current_scene_id else "(none)"
    )

    scene_lookup = SceneLookup.from_path()

    # ---- Device ------------------------------------------------------------
    print_section("Device")
    print_info([
        ("name",     device.get("name") or "(unknown)"),
        ("model",    model),
        ("serial",   dev_info.get("serial_number") or "?"),
        ("eui64",    dev_info.get("eui64") or "?"),
        ("firmware", dev_info.get("firmware_version") or "?"),
        ("hardware", dev_info.get("hardware_version") or "?"),
        ("ip",       device["ip_address"]),
        ("port",     str(device["port"])),
    ])

    # ---- Light state -------------------------------------------------------
    print_section("Light state")
    print_info([
        ("power",          "on" if light["power"] else "off"),
        ("brightness",     str(light["brightness"])),
        ("hue",            str(light["hue"])),
        ("saturation",     str(light["saturation"])),
        ("color_temp",     f"{light['color_temp']} K"),
        ("scene",     current_scene_label),
    ])

    # ---- Scenes ------------------------------------------------------------
    print_section("Scenes")
    if scene_handles:
        for byte in scene_handles:
            handle = bytes([byte])
            active = " ◀ active" if handle == current_scene_id else ""

            detail = scene_detail.get(byte)
            device_pal = detail.get("palette", "") if detail else ""

            match_name = match_uuid = match_method = None
            if device_pal:
                result = scene_lookup.resolve(device_pal)
                if result:
                    match_name, match_uuid, match_method = result

            name = match_name or f"Scene 0x{byte:02x}"
            print(f"  0x{byte:02x}  {name}{active}")

            meta: list[tuple[str, str]] = []

            if match_name:
                meta.append(("matched", f"by palette ({match_method})"))
                meta.append(("uuid",    match_uuid or ""))

            if device_pal:
                meta.append(("palette", device_pal))

            if meta:
                col = max(len(k) for k, _ in meta) + 1
                for k, v in meta:
                    print(f"        {k:<{col}}: {v}")
    else:
        print("  (none)")

    # ---- Thread network ----------------------------------------------------
    if thread_net is not None:
        print_section("Thread network")
        rows: list[tuple[str, str]] = []
        if thread_net.get("network_name"):
            rows.append(("network",      thread_net["network_name"]))
        if thread_net.get("channel") is not None:
            rows.append(("channel",      str(thread_net["channel"])))
        if thread_net.get("pan_id"):
            rows.append(("pan_id",       thread_net["pan_id"]))
        if thread_net.get("extended_pan_id"):
            rows.append(("ext_pan_id",   thread_net["extended_pan_id"]))
        if thread_net.get("mesh_local_prefix"):
            rows.append(("mesh_prefix",  thread_net["mesh_local_prefix"]))
        if thread_role is not None:
            active_roles = [k for k, v in thread_role.items() if k != "raw" and v]
            rows.append(("role",         ", ".join(active_roles) or "unknown"))
        if thread_caps is not None:
            active_caps = [k for k, v in thread_caps.items() if k != "raw" and v]
            rows.append(("capabilities", ", ".join(active_caps) or "none"))
        if rows:
            print_info(rows)
        else:
            print("  (no Thread data)")


async def cmd_set(args: argparse.Namespace) -> None:
    """Set one or more light parameters atomically, and optionally activate a scene."""
    # Validate that at least one parameter was supplied
    has_light = any([
        args.power is not None,
        args.brightness is not None,
        args.hue is not None,
        args.saturation is not None,
        args.color_temp is not None,
    ])
    if not has_light and not args.scene and not args.identify:
        sys.exit("Error: at least one of --on/--off, --brightness, --hue, "
                 "--saturation, --color-temp, --scene, --identify is required")

    device = load_device(args.conf)
    session = await open_session(device)
    try:
        if has_light:
            await session.set_light_state(
                on=args.power if args.power is not None else None,
                brightness=args.brightness,
                hue=args.hue,
                saturation=args.saturation,
                color_temp=args.color_temp,
            )

        if args.scene:
            # Resolve scene by name (case-insensitive) or hex handle (e.g. "0xfb" / "251")
            scene_id: bytes | None = None
            handles = await session.list_scenes()
            query = args.scene.strip()

            # Try numeric / hex handle first
            try:
                if query.lower().startswith("0x"):
                    raw = int(query, 16)
                else:
                    raw = int(query)
                if raw in handles:
                    scene_id = bytes([raw])
            except ValueError:
                pass

            # Fall back to hex-label match e.g. "Scene 0xfb"
            if scene_id is None:
                for byte in handles:
                    if f"Scene 0x{byte:02x}".lower() == query.lower():
                        scene_id = bytes([byte])
                        break

            if scene_id is None:
                sys.exit(f"Error: scene {query!r} not found on device")

            await session.play_scene(scene_id)

        if args.identify:
            await session.identify()
    finally:
        await session.close()

    # Confirmation summary
    changes: list[str] = []
    if args.power is not None:
        changes.append("power=" + ("on" if args.power else "off"))
    if args.brightness is not None:
        changes.append(f"brightness={args.brightness}")
    if args.hue is not None:
        changes.append(f"hue={args.hue}")
    if args.saturation is not None:
        changes.append(f"saturation={args.saturation}")
    if args.color_temp is not None:
        changes.append(f"color_temp={args.color_temp}K")
    if args.scene:
        changes.append(f"scene={args.scene!r}")
    if args.identify:
        changes.append("identify")
    print("Set: " + ", ".join(changes))


async def cmd_unpair(args: argparse.Namespace) -> None:
    """Revoke the pairing token on the device and remove it from the config file."""
    device = load_device(args.conf)
    session = await open_session(device)
    try:
        await session.unpair()
    finally:
        await session.close()

    remove_token(args.conf)
    print(f"Unpaired — token revoked and removed from {args.conf}")

    if args.delete:
        os.remove(args.conf)
        print(f"Deleted {args.conf}")


# ---------------------------------------------------------------------------
# observe
# ---------------------------------------------------------------------------


async def cmd_observe(args: argparse.Namespace) -> None:
    """Poll the device for light state changes and print diffs.

    Keeps one LTPDU session open and calls query_light_state every
    --interval seconds.  Re-KEXes only when the session actually expires.
    """
    device = load_device(args.conf)
    print(f"Observing {device['ip_address']}:{device['port']} "
          f"(interval={args.interval}s, Ctrl+C to stop)\n")

    prev: dict[str, Any] | None = None
    KEYS = ["power", "brightness", "hue", "saturation", "color_temp"]

    try:
        while True:
            # Open session — re-KEX only here and on session expiry.
            try:
                session = await open_session(device, timeout=10.0)
            except RuntimeError as e:
                print(f"[session error] {e} — retrying in 5s")
                await asyncio.sleep(5.0)
                continue

            # Inner poll loop: keep the same session alive.
            try:
                while True:
                    try:
                        state = await session.query_light_state(timeout=10.0)
                    except SessionExpiredError:
                        try:
                            await session.reauth()
                        except RuntimeError as e:
                            raise RuntimeError(f"reauth failed: {e}") from e
                        state = await session.query_light_state(timeout=10.0)

                    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    if prev is None:
                        rows = [
                            ("power",      "on" if state["power"] else "off"),
                            ("brightness", str(state["brightness"])),
                            ("hue",        str(state["hue"])),
                            ("saturation", str(state["saturation"])),
                            ("color_temp", f"{state['color_temp']} K"),
                        ]
                        col = max(len(k) for k, _ in rows) + 1
                        print(f"[{ts}] (initial state)")
                        for k, v in rows:
                            print(f"  {k:<{col}}: {v}")
                        print()
                    else:
                        changed = {k: state[k] for k in KEYS if state[k] != prev[k]}
                        if changed:
                            print(f"[{ts}] state change:")
                            col = max(len(k) for k in changed) + 1
                            for k, v in changed.items():
                                old = prev[k]
                                if k == "power":
                                    v_str, old_str = ("on" if v else "off"), ("on" if old else "off")
                                elif k == "color_temp":
                                    v_str, old_str = f"{v} K", f"{old} K"
                                else:
                                    v_str, old_str = str(v), str(old)
                                print(f"  {k:<{col}}: {old_str} → {v_str}")
                            print()
                    prev = state
                    await asyncio.sleep(args.interval)

            except (RuntimeError, asyncio.CancelledError) as e:
                if isinstance(e, asyncio.CancelledError):
                    raise KeyboardInterrupt from None
                print(f"[error] {e} — reconnecting in 3s")
                await asyncio.sleep(3.0)
            finally:
                try:
                    await session.close()
                except Exception:
                    pass
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# fw
# ---------------------------------------------------------------------------

_FW_API_PAYLOAD: dict[str, Any] = {
    "app_platform": "Android",
    "app_version": "11.9.2",
    "firmware": "1.0.0",  # always 1.0.0 so server returns latest available version
    "serial": "N25180B0K50",
    "use_https": True,    # NL45/NL67 don't support WiFi OTA, so always BLE/HTTPS path
}


async def cmd_get_scenes(args: argparse.Namespace) -> None:
    """Download scene metadata from the Nanoleaf cloud and update scenes.json."""
    api = NanoleafCloudApi()
    api.build_scenes(Path(args.scenes), save_raw=args.save_raw, progress_cb=print)


async def cmd_fw(args: argparse.Namespace) -> None:
    """Show device firmware info; optionally check or download an update."""
    cfg = load_device(args.conf)
    session = await open_session(cfg)
    try:
        dev_info = await session.query_device_info()
    finally:
        await session.close()

    model    = cfg.get("model") or dev_info.get("hardware_version") or "?"
    fw_ver   = dev_info.get("firmware_version") or "?"
    hw_ver   = dev_info.get("hardware_version") or "?"

    print(f"  model    : {model}")
    print(f"  firmware : {fw_ver}")
    print(f"  hardware : {hw_ver}")

    if not args.check and not args.download:
        return

    payload = dict(_FW_API_PAYLOAD)
    payload["model"]    = str(model)
    payload["hardware"] = str(hw_ver)

    api = NanoleafFirmwareApi()
    print(f"\nFetching available firmware versions for {model} …")

    # Walk the version chain: start from 1.0.0, advance to each returned version
    # until the API reports no further update.
    versions: list[tuple[str, str]] = []   # (version, url)
    seen: set[str] = set()
    probe = payload["firmware"]            # "1.0.0"
    while True:
        payload["firmware"] = probe
        data = api.check_update(payload)
        status  = data.get("update_status", "none")
        new_ver = data.get("new_firmware_ver") or ""
        fw_url  = data.get("fw_location") or ""
        if status == "none" or not new_ver or new_ver in seen:
            break
        versions.append((new_ver, fw_url))
        seen.add(new_ver)
        probe = new_ver

    if versions:
        print(f"  {'version':<12}  url")
        for ver, url in versions:
            marker = "  ◀ installed" if ver == str(fw_ver) else ""
            print(f"  {ver:<12}  {url}{marker}")
        latest = versions[-1][0]
        if str(fw_ver) == latest:
            print(f"\nAlready on latest firmware ({latest}).")
        else:
            print(f"\nInstalled: {fw_ver}  →  latest: {latest}")
            if args.download:
                dl_url = versions[-1][1]
                if not dl_url:
                    sys.exit("Error: no fw_location in response")
                dest = f"{model}_{latest}.bin"
                print(f"Downloading → {dest}")
                api.download(dl_url, dest)
                print(f"Saved {dest}")
    else:
        print("  No firmware information returned by server.")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Nanoleaf LTPDU command-line controller",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # -- discover ------------------------------------------------------------
    p_disc = sub.add_parser(
        "discover", help="scan for all LTPDU devices on the network"
    )
    p_disc.add_argument(
        "--save",
        action="store_true",
        help="write a JSON config file for each discovered device",
    )
    p_disc.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        metavar="SECS",
        help="mDNS scan duration in seconds (default: 5.0)",
    )

    # -- find ----------------------------------------------------------------
    p_find = sub.add_parser("find", help="find a single device by IP or hostname")
    p_find.add_argument("address", help="IPv4, IPv6, or hostname of the device")
    p_find.add_argument(
        "--port", type=int, default=5683, help="CoAP port (default: 5683)"
    )
    p_find.add_argument("--model", help="device model hint (e.g. NL67)")
    p_find.add_argument("--save", action="store_true", help="write a JSON config file")
    p_find.add_argument(
        "--conf",
        default=DEFAULT_CONF,
        metavar="FILE",
        help=f"device config file (default: {DEFAULT_CONF})",
    )

    # -- pair ----------------------------------------------------------------
    p_pair = sub.add_parser("pair", help="pair with a device using its PIN")
    p_pair.add_argument("--pin", default=None, help="pairing PIN (e.g. 3394-532-2503); falls back to 'pin' in conf file")
    p_pair.add_argument(
        "--conf",
        default=DEFAULT_CONF,
        metavar="FILE",
        help=f"device config file (default: {DEFAULT_CONF})",
    )

    # -- info ----------------------------------------------------------------
    p_info = sub.add_parser("info", help="show full device and light state info")
    p_info.add_argument(
        "--conf",
        default=DEFAULT_CONF,
        metavar="FILE",
        help=f"device config file (default: {DEFAULT_CONF})",
    )

    # -- set -----------------------------------------------------------------
    p_set = sub.add_parser(
        "set", help="change light state (brightness, color, scene, …)"
    )
    p_set.add_argument(
        "--conf",
        default=DEFAULT_CONF,
        metavar="FILE",
        help=f"device config file (default: {DEFAULT_CONF})",
    )
    power = p_set.add_mutually_exclusive_group()
    power.add_argument(
        "--on",
        dest="power",
        action="store_true",
        default=None,
        help="turn the light on",
    )
    power.add_argument(
        "--off", dest="power", action="store_false", help="turn the light off"
    )
    p_set.add_argument("--brightness", type=int, metavar="N", help="0–100")
    p_set.add_argument("--hue", type=int, metavar="N", help="0–360")
    p_set.add_argument("--saturation", type=int, metavar="N", help="0–100")
    p_set.add_argument(
        "--color-temp",
        type=int,
        metavar="N",
        dest="color_temp",
        help="color temperature in Kelvin (e.g. 4000)",
    )
    p_set.add_argument("--scene", metavar="NAME", help="scene name or handle")
    p_set.add_argument("--identify", action="store_true", help="blink the light for physical identification")

    # -- unpair --------------------------------------------------------------
    p_unpair = sub.add_parser("unpair", help="revoke pairing token from the device")
    p_unpair.add_argument(
        "--conf",
        default=DEFAULT_CONF,
        metavar="FILE",
        help=f"device config file (default: {DEFAULT_CONF})",
    )
    p_unpair.add_argument(
        "--delete",
        action="store_true",
        help="also delete the JSON config file after unpairing",
    )

    # -- observe -------------------------------------------------------------
    p_obs = sub.add_parser(
        "observe", help="poll for light state changes and print diffs"
    )
    p_obs.add_argument(
        "--interval",
        type=float,
        default=2.0,
        metavar="SECS",
        help="polling interval in seconds (default: 2.0)",
    )
    p_obs.add_argument(
        "--conf",
        default=DEFAULT_CONF,
        metavar="FILE",
        help=f"device config file (default: {DEFAULT_CONF})",
    )

    p_fw = sub.add_parser("fw", help="check or download firmware updates")
    p_fw.add_argument(
        "--check",
        action="store_true",
        help="check Nanoleaf cloud for available firmware update",
    )
    p_fw.add_argument(
        "--download",
        action="store_true",
        help="download the firmware binary if an update is available",
    )
    p_fw.add_argument(
        "--conf",
        default=DEFAULT_CONF,
        metavar="FILE",
        help=f"device config file (default: {DEFAULT_CONF})",
    )

    # -- get-scenes ----------------------------------------------------------
    _default_scenes = str(Path(__file__).parent / "scenes.json")
    p_gs = sub.add_parser(
        "get-scenes", help="download scene metadata from the Nanoleaf cloud into scenes.json"
    )
    p_gs.add_argument(
        "--scenes",
        default=_default_scenes,
        metavar="FILE",
        help=f"scenes.json path (default: {_default_scenes})",
    )
    p_gs.add_argument(
        "--save-raw",
        action="store_true",
        dest="save_raw",
        help="also save raw API responses to <label>_raw.json files",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_COMMANDS = {
    "discover": cmd_discover,
    "find": cmd_find,
    "pair": cmd_pair,
    "info": cmd_info,
    "set": cmd_set,
    "unpair": cmd_unpair,
    "observe": cmd_observe,
    "fw": cmd_fw,
    "get-scenes": cmd_get_scenes,
}


async def main(args: argparse.Namespace) -> None:
    fn = _COMMANDS[args.command]
    try:
        await fn(args)
    except NotImplementedError as e:
        sys.exit(f"Error: {e}")
    except RuntimeError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    parser = build_parser()
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)
    args = parser.parse_args()
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    asyncio.run(main(args))
