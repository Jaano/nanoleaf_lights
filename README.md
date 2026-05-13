# Enhanced Nanoleaf Lights

Home Assistant integration for Nanoleaf Essentials bulbs that adds **scene activation** support — the one thing the standard Matter integration cannot do. Basic controls (power, brightness, color) are available too, but if you only need those, the built-in Matter integration is sufficient.

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
| Identify (blink) | yes |
| Thread diagnostics | yes (sensor entities) |
| Device info | yes (device page) |

Scenes are stored on the device as palette data and resolved to cloud scene names via `scenes.json`. See [SCENES.md](SCENES.md).

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

1. Copy (or symlink) the `custom_components/nanoleaf_lights/` directory from this repository into your Home Assistant `config/custom_components/` directory. The final path should be `config/custom_components/nanoleaf_lights/`.
2. Restart Home Assistant.
3. Continue with [Adding a Device](#adding-a-device).

The integration requires `aiocoap` and `cryptography`; Home Assistant installs them automatically on first load.

### Adding a Device

> **Prerequisite:** The bulb must already be joined to your Thread network via Matter (e.g. using the Nanoleaf app or Apple Home). Matter is not used by this integration — only for the initial network join.

1. Go to **Settings -> Devices & Services -> Add Integration** and search for **Enhanced Nanoleaf Lights**.
2. Devices on the local Thread network are auto-discovered via mDNS. Alternatively enter the address manually (IPv6, IPv4, or hostname).
3. Enter the **Pairing Code** printed on the bulb.

### Reconfiguring a Device

If a bulb is unpaired and re-paired in your Matter environment (e.g. re-added in the Nanoleaf or Apple Home app, or after a factory reset), it will rejoin Thread with a **new IPv6 address** and a new pairing code. The existing Home Assistant device entry will stop working until it is pointed at the new address.

To recover without removing and re-adding the integration:

1. Go to **Settings -> Devices & Services -> Enhanced Nanoleaf Lights** and open the affected device.
2. Click the **⋮** menu -> **Reconfigure**.
3. Confirm the new address (auto-discovered or entered manually) and, if the bulb was factory-reset, enter the new **Pairing Code**.

The device's existing entities, history, and automations are preserved.

### Scene Support

Scenes are automatically resolved to their cloud names. Use the **Refresh Scene Database** button entity to download the latest scene data from the Nanoleaf cloud. This download can take several minutes to complete. See [SCENES.md](SCENES.md) for how palette matching works.

### Polling

The integration polls every 5 seconds. Device info, scene list, and scene names are fetched once on first connect and cached until the integration is reloaded. Session expiry triggers automatic re-authentication.

---

## How It Works

Devices are controlled via LTPDU — a CoAP/UDP protocol with X25519 key exchange and AES-128-CTR encryption. Separate from Matter and no cloud required.

- **Transport:** CoAP over UDP via Thread border router
- **Discovery:** mDNS `_ltpdu._udp.local.`
- **Addressing:** IPv6 natively; IPv4 and hostname also work when NAT64 is enabled on the border router

See [LTPDU.md](LTPDU.md) for the full protocol reference.

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
