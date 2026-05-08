# Nanoleaf App  -  Scene/Effect Discovery API

## Cloud base URL

```
https://my.nanoleaf.me/api/
```

Debug/staging variant (disabled in release builds): `https://my-test.nanoleaf.me/api/`

---

## Discover endpoints

The "Discover" screen has two tabs  -  **Featured** and **Community**  -  both backed by the same
request model. The distinction is a single boolean field in the POST body.

### Effects (individual scenes)

```json
POST /v1/effects/search
Content-Type: application/json

{
  "page":      1,          // 1-based
  "items":     10,         // page size (default 10)
  "featured":  true,       // true = Featured tab, false = Community tab
  "query":     "sunset",   // optional free-text search
  "sort":      "recent",   // "recent" | "top"
  "type":      "color",    // "color" | "music"  (omit for both)
  "features":  ["touch", "interactive"],  // optional array
  "filter":    "author",   // "author" | "plugin"  (optional)
  "tag":       "nature",   // optional
  "colorType": "HSB"       // "HSB" | "CCT"  (optional)
}
```

Response type: `DiscoverEffectsSearchResponse`

### Playlists (scene collections)

```
POST /v2/playlist/discover
```

Same request body as effects search. Response type: `DiscoverPlaylistSearchResponse`.

---

## Other discover-related endpoints

| Method | Path | Purpose |
| -------- | ------ | --------- |
| `GET`  | `v1/effects/download/{effect_id}` | Fetch full effect definition |
| `GET`  | `v2/playlist/download/{playlist_id}` | Fetch playlist with all effects |
| `PUT`  | `v1/effects/save/{effect_id}` | Increment effect download counter |
| `GET`  | `v2/playlist/userdownloaded/{effect_id}` | Track playlist download |
| `POST` | `v1/effects/upload` | Upload a user-created scene (`Authorization` header required) |
| `POST` | `v2/items/report` | Report/flag an item |
| `GET`  | `v2/plugins/{model_number}/{firmware_version}` | Fetch available motion plugins for a device |

---

## Request field reference

| Field | Type | Values | Notes |
| ------- | ------ | --------- | ------- |
| `page` | int | >= 1 | 1-based pagination |
| `items` | int | default 10 | Page size |
| `featured` | bool | `true` / `false` | Featured vs Community tab |
| `query` | string | free text | Omit or null for no filter |
| `sort` | string | `"recent"`, `"top"` | Recency or popularity |
| `type` | string | `"color"`, `"music"` | Scene type filter; omit for all |
| `features` | string[] | `"touch"`, `"interactive"` | Optional capability filter |
| `filter` | string | `"author"`, `"plugin"` | Search-within filter |
| `tag` | string | free text | Tag filter |
| `colorType` | string | `"HSB"`, `"CCT"` | Color model filter |

---

## Cloud effect `key` field

Every effect record includes a `key` field that base64-decodes to:

```
{creator_user_id}/{effect_uuid}
```

- `creator_user_id`  -  24-hex-char MongoDB ObjectId of the uploading user's account. The
  timestamp bytes encode the account creation date.
- `effect_uuid`  -  the effect's `uuid` field, verbatim.

This is the cloud object-storage path (AWS S3 or equivalent) for the full animation definition
file. Used internally by the app when downloading the raw effect data via
`GET v1/effects/download/{effect_id}`.

---

## Simple scene TLV format (device wire protocol)

Used for all scene operations on Matter/HomeKit (LTPDU) devices via the `ci` endpoint.

| ci tag   | Operation                              |
|----------|----------------------------------------|
| `0x0701` | Preview scene (display without saving) |
| `0x0702` | Add scene (persist to device)          |
| `0x0704` | Get scene (read back stored scene)     |
| `0x0705` | Delete scene                           |
| `0x0706` | Play scene (activate)                  |
| `0x0707` | Get current scene ID                   |

The payload for add/preview/get is a **TLV1** (1-byte tag + 1-byte length) pair sequence:

```text
TLV1(0x01, metadata_bytes)
TLV1(0x02, palette_bytes)
```

Source: `NanoleafEffect.toSimpleSceneTLV()` (write) and `buildFromSimpleSceneTLV()` (read).

### Metadata bytes

```text
[sceneId(1B), effectType(1B), ...effect-type-specific params]
```

`sceneId` valid range: **1-254** (`SIMPLE_SCENE_VALID_ID_RANGE`).
Built-in scenes occupy 244-254; user-created scenes use 1-243.

Effect type byte values and their motion parameters:

| Byte   | Name             | Params after `[id, type]`                                       |
|--------|------------------|-----------------------------------------------------------------|
| `0x01` | FADE             | transitTime(1B), delayTime(1B), loop(1B: 1=true)                |
| `0x02` | RANDOM           | transitTime(1B), delayTime(1B)                                  |
| `0x03` | HIGHLIGHT        | transitTime(1B), delayTime(1B), mainColorProbability(1B, 0-100) |
| `0x04` | STREAM\_CONTROL  | *(empty)*                                                       |
| `0x05` | FLOW             | transitTime(1B), delayTime(1B), linearDirection(1B), loop(1B)   |
| `0x06` | STRIPES          | transitTime(1B), linearDirection(1B), segment(1B, 0-100)        |

App defaults when creating a new scene: `transitTime=24`, `delayTime=0`, `loop=1`.

### Palette bytes

```text
[count(1B), color_entry...]
```

Each color entry is 3 bytes packed **big-endian** into a 24-bit integer, optionally followed
by a repeat byte:

```text
packed = byte[0]<<16 | byte[1]<<8 | byte[2]

  bit  23    : has_repeat   -  if 1, a 4th byte follows
  bits 22-14 : hue         (0-360, 9 bits)
  bits 13-7  : saturation  (0-100, 7 bits)
  bits  6-0  : brightness  (0-100, 7 bits)

  [repeat(1B)]   -  present only when has_repeat=1
                  color appears 1 + repeat times in the expanded palette
```

`count` is the number of compact color entries (before repeat expansion), not the total
expanded palette length.

---

## Source locations (jadx_out_11.9.2)

All paths relative to `reverse/android/jadx_out_11.9.2/sources/`.

| File | What it contains |
|------|-----------------|
| `me/nanoleaf/nanoleaf/nanoleafclient/UrlProvider.java` | Base URLs |
| `p920zg/InterfaceC29815c.java` | `EffectsApiService` Retrofit interface (all endpoints) |
| `me/nanoleaf/nanoleaf/nanoleafclient/requestmodels/DiscoverSearchRequest.java` | Request model + enums |
| `me/nanoleaf/nanoleaf/nanoleafclient/requestmodels/DiscoverSearchRequestSerializer.java` | Gson serializer (exact JSON field names) |
| `me/nanoleaf/nanoleaf/repository/DiscoverRepositoryImpl.java` | Business logic, paging, caching |
| `me/nanoleaf/nanoleaf/feature/accessory_control/base/scene/discover/DiscoverFilterRadioItem.java` | Featured / Community enum |
| `me/nanoleaf/nanoleaf/Constants.java` | Built-in scene ID -> name maps |
| `me/nanoleaf/nanoleaf/models/effect_manager_v2/NanoleafEffect.java` | `buildFromAnimationJson`, `buildFromSimpleSceneTLV`, name resolution |
| `me/nanoleaf/nanoleaf/communication/hardware/commands/animations/GetAllAnimationsRequest.java` | `requestAll` command wrapper |
| `me/nanoleaf/nanoleaf/communication/hardware/response/GetAllAnimationsResponseBody.java` | Response wrapper (`animations` array) |
| `me/nanoleaf/nanoleaf/models/dto/AccessoryAnimationDTO.java` | Per-animation DTO (includes `animName`) |
| `me/nanoleaf/nanoleaf/homekitclient/networking/command_centre/CommandCentreRepository.java` | Orchestrates `requestAll`, persists effects |

---

## How the app resolves scene names already on a device

### Path 1  -  User / downloaded scenes (Wi-Fi and Matter devices)

The app sends a `requestAll` command to the device over the local channel:

```json
{"command": "requestAll", "version": "2.0"}
```

The device replies with a JSON object:

```json
{
  "animations": [
    {
      "animName": "Northern Lights",
      "animType": "plugin",
      "pluginUuid": "ba632d3e-9c2b-...",
      "palette": [...],
      "pluginOptions": [...],
      ...
    }
  ]
}
```

The `animName` field is the display name exactly as stored on the device. The app takes it
The app takes it verbatim — no lookup table is involved. Each animation is parsed into a `NanoleafEffect`
via `NanoleafEffect.Companion.buildFromAnimationJson()` and the name is set directly from
`animName`.

There is no stable numeric ID in this path. The app derives a 32-bit ID by
hashing `animName` (Java `hashCode()` masked to 32 bits) and uses it internally.

### Path 2  -  Built-in scenes on Matter/Thread (HomeKit) devices

Devices that expose scenes via TLV (HomeKit characteristic) use a different path:
`buildFromSimpleSceneTLV()`. The TLV payload carries only a **numeric scene ID** (e.g. 251)
and color/motion data  -  no name string.

The app resolves the name by looking up the ID in one of the three hardcoded maps from
`Constants.java`, selected by device type:

```java
String name = (sourceAccessoryType != Accessory.TYPE.SECRETLABS_LIGHT_STRIPS)
    ? Constants.f59057A.get(id)           // secretlab map
    : AccessoryUtilsKt.isUmbra(type)
        ? Constants.f59084z.get(id)       // umbra map
        : Constants.f59083y.get(id);      // essentials map
// fallback if not found:
if (name == null) name = "Effect " + id;
```

The maps are the same ones documented in the "Built-in scenes" tables above.

### Summary

| Source                              | Name origin                                               | ID origin               |
|-------------------------------------|-----------------------------------------------------------|-------------------------|
| `requestAll` JSON (Wi-Fi/standard)  | `animName` field from device                              | hash of `animName`      |
| TLV characteristic (Matter/HomeKit) | Hardcoded map in `Constants.java` keyed by numeric scene ID | numeric scene ID from TLV |
