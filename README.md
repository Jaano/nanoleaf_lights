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

Copy or symlink `custom_components/nanoleaf_ltpdu/` into your HA `config/custom_components/` directory. Restart Home Assistant.

No extra Python dependencies beyond what ships with Home Assistant.

### Adding a Device

> **Prerequisite:** The bulb must already be joined to your Thread network via Matter (e.g. using the Nanoleaf app or Apple Home). Matter is not used by this integration — only for the initial network join.

1. Go to **Settings -> Devices & Services -> Add Integration** and search for **Nanoleaf LTPDU**.
2. Devices on the local Thread network are auto-discovered via mDNS. Alternatively enter the address manually (IPv6, IPv4, or hostname).
3. Enter the **Pairing Code** printed on the bulb.

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

## License

MIT License — Copyright (c) 2026 Jaano. See [LICENSE](LICENSE) for full text.
