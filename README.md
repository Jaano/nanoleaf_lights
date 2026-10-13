# Enhanced Nanoleaf Lights

Home Assistant integration for Nanoleaf Essentials bulbs with scene activation, circadian lighting, device settings, and diagnostics alongside power, brightness, and color controls.

Communicates directly over Thread using Nanoleaf's proprietary LTPDU protocol.
Nanoleaf cloud is only used to resolve scene names from palette data (if needed), but the integration works without internet access and does not require a cloud account.

---

## Device Compatibility

| Model | Name | Status |
| --- | --- | --- |
| NL67 | Essentials A19 / A60 (Matter) | Tested |
| NL45 | Essentials A19 (legacy) | Tested |
| NL55 | Essentials Bulb (legacy) | Should work |
| NL58 | Essentials Candle (legacy) | Should work |
| NL62 | Essentials BR30 (legacy) | Should work |

NL55/NL58/NL62 use the same protocol variant as NL45 and are expected to work but have not been verified on hardware.

---

## Supported Features

| Feature | HA Integration |
| --- | --- |
| **Scenes / effects** | **yes — primary feature** |
| Power on/off | yes |
| Brightness | yes |
| Hue / saturation | yes |
| Color temperature | yes |
| Auto Circadian Lighting | switch and schedule action |
| Clock, timezone, and location | actions and synchronization button |
| Power Loss Recovery | configuration switch |
| Motion capabilities | diagnostic sensor and response action |
| Identify (blink) | yes |
| Thread diagnostics | yes (sensor entities) |
| Device info | yes (device page) |

New firmware reports scene names directly. Older firmware stores palette data that is resolved through `scenes.json`. See [SCENES.md](SCENES.md).

---

## Installation

### Option 1: HACS (recommended)

[HACS](https://hacs.xyz/) is the Home Assistant Community Store. If you don't have it installed yet, follow the [HACS installation guide](https://hacs.xyz/docs/use/) first.

This integration is not (yet) in the default HACS repository, so it must be added as a **custom repository**:

1. Open HACS in Home Assistant.
2. Click the **⋮** menu (top right) -> **Custom repositories**.
3. Add the repository:
   - **Repository:** `https://github.com/Jaano/nanoleaf_lights`
   - **Type:** `Integration`
4. Click **Add**, then find **Enhanced Nanoleaf Lights** in the HACS integrations list and click **Download**.
5. **Restart Home Assistant** (Settings -> System -> Restart).
6. Continue with [Adding a Device](#adding-a-device).

Updates will appear in HACS like any other integration; click **Update** and restart HA.

### Option 2: Manual installation

1. Copy the `custom_components/nanoleaf_lights/` directory into your Home Assistant `config/custom_components/` directory, following its symlinks so the protocol modules are copied as files. The final path should be `config/custom_components/nanoleaf_lights/`. If the entire checkout is accessible to HA, symlinking the integration directory also works.
2. Restart Home Assistant.
3. Continue with [Adding a Device](#adding-a-device).

The integration requires `aiocoap` and `cryptography`; Home Assistant installs them automatically on first load.

### Adding a Device

> **Prerequisite:** The bulb must already be joined to your Thread network via Matter (e.g. using the Nanoleaf app or Apple Home). Matter is not used by this integration — only for the initial network join.

1. Go to **Settings -> Devices & Services -> Add Integration** and search for **Enhanced Nanoleaf Lights**.
2. Devices on the local Thread network are auto-discovered via mDNS. For manual setup, enter the address and advertised port (`12566` for NL67 firmware 5.1.45; older devices commonly use `5653` or `5683`). New-protocol pairing also needs the bulb's advertised Nanoleaf name, such as `Nanoleaf A19 E20-78E2`; enter that in **Discovered Name**.
3. Enter the **Pairing Code** printed on the bulb.

### Reconfiguring a Device

If a bulb is unpaired and re-paired in your Matter environment (e.g. re-added in the Nanoleaf or Apple Home app, or after a factory reset), it will rejoin Thread with a **new IPv6 address** and a new pairing code. The existing Home Assistant device entry will stop working until it is pointed at the new address.

To recover without removing and re-adding the integration:

1. Go to **Settings -> Devices & Services -> Enhanced Nanoleaf Lights** and open the affected device.
2. Click the **⋮** menu -> **Reconfigure**.
3. Confirm the new address and advertised port. If the bulb was factory-reset or upgraded from the legacy protocol, enter its **Pairing Code** to obtain new credentials. For manual new-protocol pairing, also supply the advertised **Discovered Name**.

The device's existing entities, history, and automations are preserved.

### Scene Support

New firmware supplies scene names directly. For older firmware, names are resolved from the local scene database. Use the **Refresh Scene Database** button entity to download the latest scene data from the Nanoleaf cloud. This download can take several minutes to complete. See [SCENES.md](SCENES.md) for how palette matching works.

### Polling

The integration polls light state every 5 seconds and optional settings every 60 seconds. Device info, scene lists, and motion capabilities are fetched after connecting. Session expiry triggers automatic re-authentication.

### Circadian lighting and device settings

Supported bulbs expose **Auto Circadian Lighting** and **Power Loss Recovery** configuration switches, a **Sync Circadian clock and location** button, and a **Motion capabilities** diagnostic sensor. Enable the diagnostic sensor in the entity settings to inspect its animations and parameter attributes. Availability depends on the bulb's model, firmware, and successful settings readback.

Configure the schedule through **Developer Tools → Actions → Enhanced Nanoleaf Lights: Configure circadian lighting** (`nanoleaf_lights.set_circadian_config`). Supply all three white points, all three ramp durations, and the two timing fields for the selected mode. Fixed times are minutes after midnight; sunrise/sunset offsets are signed minutes. White points are clamped to the bulb's supported range.

```yaml
action: nanoleaf_lights.set_circadian_config
target:
  entity_id: light.nanoleaf_bulb
data:
  mode: sunrise_sunset
  daylight_target_kelvin: 6500
  evening_target_kelvin: 2100
  morning_target_kelvin: 4000
  rise_ramp_minutes: 30
  fall_ramp_minutes: 60
  morning_ramp_minutes: 15
  sunrise_offset_minutes: -15
  sunset_offset_minutes: 30
```

For `explicit` mode, replace the two offsets with `rise_time_minutes` and `fall_time_minutes` (0–1439). Saving a sunrise/sunset schedule and enabling Auto Circadian Lighting synchronize the bulb's clock, timezone offset, and location from Home Assistant. Enabling Auto Circadian Lighting also turns the bulb on. These writes happen only when you invoke a control; discovery and polling do not change settings. The switch attributes show the stored schedule, clock, and location.

`nanoleaf_lights.set_device_time` accepts optional `epoch_seconds` and `timezone_offset_seconds`; omitted values use the current time and Home Assistant's timezone at the selected date. `nanoleaf_lights.set_device_location` accepts `latitude` and `longitude`. Use the synchronization button after timezone or daylight-saving changes; the bulb stores a UTC offset rather than a timezone name. The synchronization button is available only when Auto Circadian Lighting is supported and its enabled state has been read successfully; it remains available when the feature is switched off.

`nanoleaf_lights.get_motion_capabilities` returns supported animations, their known parameter types/ranges/defaults, and palette/storage limits. Older firmware may expose a fixed model profile; the response's `motion_source` distinguishes this from a device-reported list. This action queries capabilities without playing an animation.

NL67 firmware 5.0.0 and newer exposes **2100–6500 K** through the light entity. Temperature limits also apply when sending commands and saving circadian white points.

---

## How It Works

The integration tries LTPDUv3 first. If connection, pairing, or authentication fails, it closes that session and tries the existing CoAP/X25519 handshake at the same advertised address and port. Existing legacy credentials remain supported; upgrading to v3 requires pairing again.

- **LTPDUv3:** Custom UDP framing, SRP pairing, and negotiated AES-CTR/CRC or AES-CCM encryption. New NL67 scene IDs and activation are supported.
- **Legacy:** Existing CoAP/X25519 and AES-128-CTR protocol.
- **Transport:** UDP via the Thread border router; independent of Matter, with no cloud required for control.
- **Discovery:** mDNS `_ltpdu._udp.local.`
- **Addressing:** IPv6 natively; IPv4 and hostname also work when NAT64 is enabled on the border router

V3 support has passed offline protocol tests and local simulated-device tests. Hardware validation on firmware 5.1.45 remains pending. See [LTPDU.md](LTPDU.md) for the protocol reference.

---

## Command-Line Interface

`cli.py` provides direct device control without Home Assistant.

### Setup

Discover devices on your Thread network and write a config file for each one:

```sh
python cli.py discover --save
# writes e.g. bulb.json, bulb_nl45.json — one file per discovered device
```

### Scene database

Download the Nanoleaf cloud scene database before using scene commands:

```sh
python cli.py scene --download
```

This writes `scenes.json` (~6000 scenes). Re-run any time to update.

### Scene commands

```sh
# Browse device scenes
python cli.py scene --list --conf bulb.json

# Identify currently active scene
python cli.py scene --current --conf bulb.json

# Preview a scene (temporary, not stored)
python cli.py scene --preview "Police 1" --conf bulb.json

# Add a scene to the device
python cli.py scene --add "Police 1" --conf bulb.json
python cli.py scene --add "Police 1" --effect flow --conf bulb.json
python cli.py scene --add "Halloween 2026" --effect fade --transition 10 --conf bulb.json

# Activate a stored scene by name or slot ID
python cli.py scene --play "Police 1" --conf bulb.json
python cli.py scene --play 12 --conf bulb.json

# Replace a stored scene
python cli.py scene --replace 12 "Halloween 2026" --conf bulb.json

# Delete a scene
python cli.py scene --delete "Police 1" --conf bulb.json

# Dry-run: show payload info without sending to device
python cli.py scene --add "Police 1" --dry-run --conf bulb.json
```

### Effect options

| Option | Default | Applies to |
|---|---|---|
| `--effect TYPE` | `fade` | all add/preview |
| `--transition N` | 24 | all effects except stream |
| `--wait N` | 0 | fade, random, highlight, flow |
| `--loop` / `--no-loop` | loop on | fade, flow |
| `--main-probability N` | 80 | highlight |
| `--direction N` | 0 | flow, stripes |
| `--segment N` | 50 | stripes |
| `--max-colors N` | 7 | all |
| `--compact-repeats` | off | all (experimental) |

Effect types: `fade`, `random`, `highlight`, `stream`, `flow`, `stripes`.

See [LTPDU.md](LTPDU.md) and [SCENES.md](SCENES.md) for protocol and palette details.

### Running tests

```sh
pip install pytest   # or: pip install -e ".[dev]"
python -m pytest
```

---

## License

MIT License — Copyright (c) 2026 [@Jaano](https://github.com/Jaano). See [LICENSE](LICENSE) for full text.
