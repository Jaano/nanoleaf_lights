# Nanoleaf Lights (++)

Home Assistant integration for Nanoleaf Essentials bulbs with scene activation, circadian lighting, device settings, and diagnostics alongside power, brightness, and color controls.

Communicates directly over Thread using Nanoleaf's proprietary LTPDU protocol.
Nanoleaf cloud is only used to resolve scene names from palette data (if needed), but the integration works without internet access and does not require a cloud account.

---

## Device Compatibility

| Model | Name | Status |
| --- | --- | --- |
| NL67 | Essentials A19 / A60 (Matter) | Tested |
| NL45 | Essentials A19 (legacy) | Tested |
| NL55 | Essentials Light Strip (legacy) | Should work |
| NL58 | Essentials Candle (legacy) | Should work |
| NL62 | Secretlab MAGRGB Smart LED Strip (legacy) | Should work |

NL55/NL58/NL62 use the same protocol variant as NL45 and are expected to work but have not been verified on hardware.

---

## Supported Features

| Feature | HA Integration |
| --- | --- |
| **Scenes / effects** | **yes — primary feature** |
| Power on/off | yes |
| Brightness | yes |
| Hue / saturation | yes |
| Color temperature | yes (not NL62) |
| Auto Circadian Lighting | switch and schedule action |
| Clock, timezone, and location | actions and synchronization button |
| Power Loss Recovery | configuration switch |
| Supported animations | diagnostic sensor and response action |
| Circadian clock, timezone offset, latitude, longitude | diagnostic sensors (disabled by default) |
| Identify (blink) | yes |
| Thread diagnostics | yes (sensor entities) |
| Device info | yes (device page) |

New firmware reports scene names directly. Older firmware stores palette data that is resolved through `scenes.json`. See [SCENES.md](SCENES.md).

---

## Installation

### Prerequisites

- Home Assistant 2026.1 or newer.
- A Thread border router (for example Apple TV, HomePod, Google Nest Hub or the Home Assistant Connect ZBT-1/ZBT-2) that Home Assistant can reach over IPv6.
- The device already joined to that Thread network, for example through the Nanoleaf app or Apple Home (see [Adding a Device](#adding-a-device)).
- The pairing code printed on the device label.
- For HACS installs, [HACS](https://hacs.xyz/) itself.

### Option 1: HACS (recommended)

[HACS](https://hacs.xyz/) is the Home Assistant Community Store. If you don't have it installed yet, follow the [HACS installation guide](https://hacs.xyz/docs/use/) first.

This integration is not (yet) in the default HACS repository, so it must be added as a **custom repository**:

1. Open HACS in Home Assistant.
2. Click the **⋮** menu (top right) -> **Custom repositories**.
3. Add the repository:
   - **Repository:** `https://github.com/Jaano/nanoleaf_lights`
   - **Type:** `Integration`
4. Click **Add**, then find **Nanoleaf Lights (++)** in the HACS integrations list and click **Download**.
5. **Restart Home Assistant** (Settings -> System -> Restart).
6. Continue with [Adding a Device](#adding-a-device).

Updates will appear in HACS like any other integration; click **Update** and restart HA.

### Option 2: Manual installation

1. Copy the `custom_components/nanoleaf_lights/` directory into your Home Assistant `config/custom_components/` directory. The final path should be `config/custom_components/nanoleaf_lights/`. The directory includes all protocol modules needed by the integration.
2. Restart Home Assistant.
3. Continue with [Adding a Device](#adding-a-device).

The integration requires `aiocoap` and `cryptography`; Home Assistant installs them automatically on first load.

### Adding a Device

> **Prerequisite:** The bulb must already be joined to your Thread network via Matter (e.g. using the Nanoleaf app or Apple Home). Matter is not used by this integration — only for the initial network join.

1. Go to **Settings -> Devices & Services -> Add Integration** and search for **Nanoleaf Lights (++)**.
2. Devices on the local Thread network are auto-discovered via mDNS. For manual setup, enter the address and advertised port (`12566` for NL67 firmware 5.1.45; older devices commonly use `5653` or `5683`). New-protocol pairing also needs the bulb's advertised Nanoleaf name, such as `Nanoleaf A19 E20-78E2`; enter that in **Discovered Name**.
3. Enter the **Pairing Code** printed on the bulb.

### Reconfiguring a Device

If a bulb is unpaired and re-paired in your Matter environment (e.g. re-added in the Nanoleaf or Apple Home app, or after a factory reset), it will rejoin Thread with a **new IPv6 address** and a new pairing code. The existing Home Assistant device entry will stop working until it is pointed at the new address.

To recover without removing and re-adding the integration:

1. Go to **Settings -> Devices & Services -> Nanoleaf Lights (++)** and open the affected device.
2. Click the **⋮** menu -> **Reconfigure**.
3. Confirm the new address and advertised port. If the bulb was factory-reset or upgraded from the legacy protocol, enter its **Pairing Code** to obtain new credentials. For manual new-protocol pairing, also supply the advertised **Discovered Name**.

The device's existing entities, history, and automations are preserved.

### If a Device Is Unreachable at Startup

Home Assistant checks that each device answers before creating its entities. If a device is powered off or unreachable when Home Assistant starts, its entry shows **Retrying setup** and Home Assistant retries automatically with increasing delays (up to a few minutes apart). The entities appear as soon as the device answers. If the device rejects the stored credentials, Home Assistant asks you to re-authenticate with the pairing code instead.

### Removing the Integration

To remove one device:

1. Go to **Settings -> Devices & Services -> Nanoleaf Lights (++)**.
2. Open the device's entry, click the **⋮** menu and choose **Delete**.

Deleting an entry also asks the device to revoke this integration's pairing token, when the device is reachable. The device stays on your Thread network and keeps working with the Nanoleaf app, Apple Home or Matter.

To remove the integration completely:

1. Delete every entry as described above.
2. Remove the files: in HACS, open **Nanoleaf Lights (++)**, click the **⋮** menu and choose **Remove**; for a manual installation, delete `config/custom_components/nanoleaf_lights/`.
3. If you installed the [scene card](#custom-scene-upload-card), remove its dashboard resource and `config/www/nanoleaf-lights/`.
4. Restart Home Assistant.

### Scene Support

New firmware supplies scene names directly. For older firmware, names are resolved from the local scene database. Use the **Refresh Scene Database** button entity to download the latest scene data from the Nanoleaf cloud. This download can take several minutes to complete. See [SCENES.md](SCENES.md) for how palette matching works.

Place the scene catalogue at `config/www/nanoleaf-lights/scenes.json`.
When present, the integration uses this file for lookups, scene preview/upload
actions, and **Refresh Scene Database** downloads. A malformed catalogue does not
cause the integration to select another file.

### Custom Scene Upload Card

The card is in [frontend/nanoleaf-scene-card.js](frontend/nanoleaf-scene-card.js).
It stays in this repository and must be installed manually. It is not a separate
HACS Dashboard package and is not automatically installed with the integration.
It needs no build step or runtime dependencies.

#### Manual installation

The integration implements `nanoleaf_lights.preview_scene` and
`nanoleaf_lights.upload_scene` for NL45 and NL62 devices using the legacy protocol and NL67 bulbs
with firmware 5+ using native effectV2 over LTPDUv3. Update the
integration files and restart Home Assistant before testing these buttons.
If the actions are missing, the card displays a warning and disables the buttons.
Other models, older NL67 firmware, and unsupported transports are rejected without uploading.
NL62 and NL67 uploads are experimental: the encoding and endpoints are covered by offline tests,
but live preview/upload testing remains pending.

1. Create `config/www/nanoleaf-lights/` and copy `frontend/nanoleaf-scene-card.js`
   to `config/www/nanoleaf-lights/nanoleaf-scene-card.js`.
2. Place your downloaded scene catalogue at `config/www/nanoleaf-lights/scenes.json`.
   The integration and card share this file, and **Refresh Scene Database** updates
   it directly.
   Copy only the scene database, never bulb configuration files or pairing tokens.
   Files under `www` are served without authentication.
3. In **Settings -> Dashboards -> Resources** (enable Advanced Mode in your profile
   if needed), add `/local/nanoleaf-lights/nanoleaf-scene-card.js?v=3` as a **JavaScript module**.
   For YAML-managed resources, use the equivalent `url` and `type: module` entry.
4. Add a **Manual** dashboard card:

   ```yaml
   type: custom:nanoleaf-scene-card
   entity: light.your_nanoleaf_bulb
   title: Bedroom Scene Upload
   scenes_url: /local/nanoleaf-lights/scenes.json
   ```

Restart Home Assistant if this is the first `www` directory. Load only one copy of
the card.

| Option | Required | Default |
|---|---|---|
| `entity` | Yes | None; select an integration light or light helper group |
| `title` | No | `Nanoleaf Scene Upload` |
| `scenes_url` | No | `/local/nanoleaf-lights/scenes.json` |

The catalogue URL must use the Home Assistant origin. External URLs and redirects
are rejected.

Use the light entity created by this integration, not a separate Matter integration.
The card also has a visual configuration editor. Reload the browser after deployment.
When updating the JavaScript file, increment the resource query version to bypass caching.
If migrating from the earlier flat `www` layout, update both the resource URL and
the card's `scenes_url`. Only public frontend files belong in `www/nanoleaf-lights/`.

Search matches scene names and UUIDs. Only the first 50 matches are rendered; refine the
search for larger result sets. The card shows up to seven palette colors and provides
Fade, Random, and Highlight animations. Transition is a protocol integer (0-255), not
seconds. NL67 requires at least 1; the default 24 works for both models.
The card exposes only 1-255 of the native effectV2 range 1-600.
Upload does not activate the scene unless **Activate after upload** is checked.
The existing light's effect list may include presets; the card does not treat its length
as the number of saved user scenes.

Light helper groups are supported. Uploads are sequential, not synchronized or atomic.
If one bulb fails, earlier members may already have saved the scene.
Check each bulb before retrying a failed upload.

#### Scene actions

The card calls these actions with `target: {entity_id: light.your_nanoleaf_bulb}`:

| Action | Data |
|---|---|
| `nanoleaf_lights.preview_scene` | `scene` (catalogue UUID), `effect` (`fade`, `random`, `highlight`; NL62 also `flow`, `stripes`), `transition` (0-255), `direction` (`left`, `right`, `up`, `down`; default `left`), `segment` (0-255; default 50) |
| `nanoleaf_lights.upload_scene` | Same fields, plus `activate` (boolean, default false) |

`direction` applies to `flow` and `stripes`, and `segment` to `stripes`; other animations ignore
them. Requesting `flow` or `stripes` for any device other than an NL62 is rejected for every
selected target before anything is sent. The scene card still offers only Fade, Random and
Highlight; use the actions directly for the NL62-only animations.

The backend resolves the UUID against the selected catalogue. It validates device support
and motion parameters, serializes scene actions per device, enforces the NL45
app-compatible 12-user-scene limit (excluding preset IDs 244-254), and selects an unused
user ID. For NL67 it preserves four-byte IDs, conservatively counts every listed scene
against the 12-scene limit, and refuses duplicate stored names. Native preset classification
is not yet established, so no native IDs are excluded from that count.
Native Highlight uses probability 40, rather than the legacy value 80.
It never deletes or replaces existing scenes. Preview does not require a free
storage slot.

Upload checks the returned ID, reads the scene back, compares the encoded motion
metadata and quantized palette, and refreshes the stored effect list before returning.
Native uploads verify the requested ID, stored name, motion ID, sent options, and HSV
palette. An acknowledgement alone is not treated as proof that the scene was saved.
Activation is performed only after verification. Failures are reported as Home Assistant
service errors. A timed-out upload is not automatically retried; check the bulb's scene
list before retrying manually, since the first write may have succeeded.

The card uses the existing `light.turn_on` action to play a stored effect. It reports
backend action completion, not an independent frontend verification of bulb storage.

### Polling

The integration polls light state every 5 seconds and optional settings every 60 seconds. Device info, scene lists, and motion capabilities are fetched after connecting. Session expiry triggers automatic re-authentication.

Missing device metadata is retried during subsequent settings polls. Settings queries and writes run in sequence to preserve accepted values when controls and polling overlap.

Thread capabilities, role, and network information are read independently. A rejected endpoint does not prevent the other groups from loading. Missing groups are retried every 60 seconds without reconnecting; sensors with failed or absent readbacks are unavailable. Successful groups are cached until the next connection.

Thread roles use Matter's enum encoding on Matter bulbs and the bitfield encoding on older HomeKit bulbs. For example, a Matter role value of `3` is displayed as `router`.

HomeKit bulbs use `th/tc` for network information; Matter bulbs use `th/ds`. Thread capabilities and roles accept padded numeric values. HomeKit credentials do not include a mesh prefix, so that sensor remains unavailable when no prefix is returned.

### Circadian lighting and device settings

Supported bulbs expose **Auto Circadian Lighting** and **Power Loss Recovery** configuration switches, a **Sync Circadian clock and location** button, and a **Supported animations** diagnostic sensor. Enable the diagnostic sensor in the entity settings to inspect its animations and parameter attributes. Availability depends on the bulb's model, firmware, and successful settings readback.

Supported animations displays a description such as **Fade, Highlight, Random**. Its attributes retain the full animation list and parameter details.

**Circadian clock**, **Circadian timezone offset**, **Circadian latitude**, and **Circadian longitude** are diagnostic sensors, disabled by default. Enable them in the entity settings to inspect the bulb's stored time and location. They use settings readbacks refreshed every 60 seconds, including when circadian lighting is off. The clock is a timestamp displayed in Home Assistant's local timezone; the separate offset sensor shows the bulb's timezone as `UTC+03:00`, for example. Coordinates are in degrees. An unset clock (epoch zero) is shown as unknown; unsupported settings or failed readbacks make their sensors unavailable.

Configure the schedule through **Developer Tools → Actions → Nanoleaf Lights (++): Configure circadian lighting** (`nanoleaf_lights.set_circadian_config`). Supply all three white points, all three ramp durations, and the two timing fields for the selected mode. Fixed times are minutes after midnight; sunrise/sunset offsets are signed minutes. White points are clamped to the bulb's supported range.

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

If the schedule cannot be read but the enabled flag is readable, the circadian switch and synchronization button stay available. The switch exposes the schedule-read error in its `configuration_error` attribute. Circadian white points support up to 6535 K on older Essentials bulbs and are clamped to the selected bulb's range when saving.

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

Run the offline Python suite without contacting bulbs:

```sh
.venv/bin/python -m pytest --ignore=tests/test_device_integration.py -q
```

The offline suite groups protocol settings, protocol sessions, transport selection,
Home Assistant entities/actions, coordinator settings, and scene workflows by purpose.
Shared offline fixtures live in [tests/conftest.py](tests/conftest.py).

Do not run bare `pytest` against paired bulbs.
[tests/test_device_integration.py](tests/test_device_integration.py) is a separate
live-device suite. It unpairs and re-pairs the bulb, persists a new token, changes
light state, and deletes every stored scene. It restores light state, not scenes.
Run it only deliberately against a disposable test bulb with an IPv6 device config
and its pairing PIN:

```sh
NANOLEAF_CONF=test-bulb.json .venv/bin/python -m pytest tests/test_device_integration.py -q
```

---

## License

MIT License — Copyright (c) 2026 [@Jaano](https://github.com/Jaano). See [LICENSE](LICENSE) for full text.
