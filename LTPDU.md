# LTPDU Protocol Reference

Local control protocol used by Nanoleaf Essentials devices. CoAP over UDP, encrypted with X25519 + AES-128-CTR.

---

## Session Flow

### 1  |  Discovery

mDNS browse `_ltpdu._udp.local.` -> receive `ip  |  port  |  model  |  eui64` -> save device config JSON.

### 2  |  Key Exchange  _(every session)_

```text
App  ->  POST /nlsecure   TLV(0x0101, our_pubkey)
App  <-                   TLV(0x0101, device_pubkey)

shared  = X25519(our_privkey, device_pubkey)
key+IV  = SHA1(salt || shared)[:16]   (salt differs by model  -  see Encryption)
```

### 3  |  Pairing or Authentication

**First connection  -  PIN pairing:**

```text
App  ->  POST /nlsecure   enc( TLV(0x0103, PIN digits) )
App  <-                   enc( TLV(0x0104, token 8 B)  )
                         save token to config
```

**Returning session  -  token auth:**

```text
App  ->  POST /nlsecure   enc( TLV(0x0104, token 8 B) )
App  <-                   enc( 12 B response           )  ← token echo or status
```

Auth success response is 12 bytes encrypted (4B TLV header + 8B payload), not a
5-byte status frame. The device may echo the token back as `TLV(0x0104, token[8])`.

### 4  |  Encrypted Control  _(CoAP GET/POST /nlltpdu)_

```text
# Read
App  ->  GET    enc( TLV(0x0001, endpoint) + TLV(0x0002, "")    )
App  <-         enc( TLV(0x0001, endpoint) + TLV(0x0003, SC+data) )

# Write
App  ->  POST   enc( TLV(0x0001, endpoint) + TLV(0x0002, value) )
App  <-         enc( TLV(0x0001, endpoint) + TLV(0x0003, 0x00)  )
```

Endpoints: `lb/0/oo`  |  `lb/0/pb`  |  `lb/0/hu`  |  `lb/0/sa`  |  `lb/0/ct`  |  `ci`  |  `di`  |  `th/...`

### Session Expiry

If the device resets the cipher it sends a **plaintext** `TLV(0x01F1, error)`. Re-run Key Exchange and re-authenticate with the stored token.

### Unpair

```text
App  ->  POST /nlsecure   enc( TLV(0x0106, "") )
App  <-                   enc( ok )
                         delete token from config
```

---

## Encryption

X25519 key exchange produces a shared secret. Key and IV are each derived by SHA-1 over a model-specific salt concatenated with the shared secret, taking the first 16 bytes. The resulting key and IV initialize a single AES-128-CTR context that persists for the entire session. Requests must be serialized - concurrent use desynchronizes the cipher.

---

## TLV Encoding

```text
# Read   (GET /nlltpdu)
0001 LLLL <endpoint>   0002 0000

# Write  (POST /nlltpdu)
0001 LLLL <endpoint>   0002 LLLL <args>
```

Response  -  tag `0x0003` carries a status byte before the payload:

```text
0001 LLLL <endpoint>   0003 LLLL SC <data...>
                                ^^
                                status code (0x00 = OK)
```

Multiple endpoint pairs can be concatenated in a single payload (batch read/write).

---

## URL Paths

| Endpoint | Use |
|---|---|
| `/nlpublic` | Unauthenticated commands (e.g. identify) |
| `/nlsecure` | Authentication (PIN pairing, token auth) |
| `/nlltpdu` | Encrypted device control |

---

## Light Control Endpoints

Color model is **HSV** (hue, saturation, brightness).

| Endpoint | Feature | Value range | Width |
|---|---|---|---|
| `lb/0/oo` | Power on/off | `0x00` / `0x01` | read: 2 B, write: 1 B |
| `lb/0/pb` | Brightness | 0-100 % | 2 B |
| `lb/0/hu` | Hue | 0-360 deg | 2 B |
| `lb/0/sa` | Saturation | 0-100 % | 2 B |
| `lb/0/ct` | Color temperature | 1200-6500 K | read: 4 B, write: 2 B |
| `lb/0/id` | Identify (blink) |  -  | write-only |

---

## Scene / Effect Endpoints (`ci`)

All scene commands are sent as `TLV(0x0002, payload)` on the `ci` endpoint, where
`payload` is a `SimpleSceneCommand` TLV: a 2-byte tag + 2-byte length + data.

| Tag | Operation | Source |
|---|---|---|
| `0x0701` | Preview scene (display without saving) | Android confirmed |
| `0x0702` | Add (persist) scene | Android confirmed |
| `0x0703` | List scene ID handles | Android confirmed |
| `0x0704` | Get scene data (takes scene ID) | Android confirmed |
| `0x0705` | Delete scene | **inferred only** — not in Android app |
| `0x0706` | Activate / play scene | Android confirmed |
| `0x0707` | Get currently executing scene | **inferred only** — not in Android app |

### SimpleSceneTLV payload format (tags 0x0702 / 0x0701)

Payload is two inner sub-TLVs using 1-byte tags (distinct from the outer 2-byte TLVs):

```
TLV1(0x01, metadata_bytes)
TLV1(0x02, palette_bytes)
```

Each `TLV1`: `tag(1B) + length(2B BE) + data`.

**Metadata bytes** = `[effectId(1B), effectType(1B), ...effect-specific]`:

| EffectType | Type byte | Extra bytes | Default values |
|---|---|---|---|
| FADE | `0x01` | transitTime, delayTime, loop | 24, 0, 1 |
| RANDOM | `0x02` | transitTime, delayTime | 24, 0 |
| HIGHLIGHT | `0x03` | transitTime, delayTime, mainColorProb | 24, 0, 80 |
| STREAM_CONTROL | `0x04` | *(none)* | — |
| FLOW | `0x05` | transitTime, delayTime, linearDir, loop | 24, 0, 0, 1 |
| STRIPES | `0x06` | transitTime, linearDir, segment | 24, 0, 50 |

The `cli.py` implementation uses FADE (`0x01`) for all cloud scenes:
`metadata = [scene_id, 0x01, 24, 0, 1]` (5 bytes). Confirmed correct by Android source.

**Palette bytes** = `count(1B) + color entries`:

```
Per color (3 bytes packed, big-endian uint24):
  bit 23     = repeat_flag  (1 = this color repeats >1× in pattern)
  bits 22-14 = hue   (0-360)
  bits 13-7  = sat   (0-100)
  bits  6-0  = bri   (0-100)

If repeat_flag is set, an additional repeat_count(1B) follows the 3 bytes.
If not set, the color appears exactly once.
```

Max 7 colors enforced by device firmware. Colors with `bri = 0` must be filtered
before encoding (device treats `0x000000` as a null terminator).

The repeat-count extension is optional — devices accept plain (non-repeated) entries.
`cli.py` does not use repeat counts; each color is written once.

### Model-specific behaviour

| Device | Firmware | Scene upload path |
|---|---|---|
| NL45 (HomeKit Essentials) | any | `ci` endpoint, tag `0x0702` |
| NL67 (Matter Essentials) | < 5.0.0 | `ci` endpoint, tag `0x0702` |
| NL67 (Matter Essentials) | ≥ 5.0.0 | Android uses Matter attribute write (EFFECT_V2 UUID `A18E6901-…`); `ci` + `0x0702` still accepted empirically |

Scenes are not accessible via the Matter data model. The `ci` endpoint is
LTPDU-only.

See [SCENES.md](SCENES.md) for scene name resolution and the cloud palette format.

---

## Device Information Endpoint (`di`)

NL67 uses sub-TLV encoding inside the response (`uint8 tag + uint16 len`):

| Sub-tag | Field |
|---|---|
| `0x03` | Hardware version (ASCII, e.g. `4.1.8`) |
| `0x04` | Serial number (ASCII, e.g. `N25180B0K50`) |
| `0x06` | EUI-64 (8 bytes big-endian) |
| `0x0b` | Model (ASCII, e.g. `67`) |
| `0xfe` | Firmware version (3 bytes: major.minor.patch) |

NL45 uses a fixed flat layout: 10 B hw + 8 B fw (ASCII) + 11 B serial + 8 B EUI-64.

---

## Thread Endpoints (`th/`)

| Endpoint | Description |
|---|---|
| `th/nc` | Node capabilities (1-byte bitfield) |
| `th/tr` | Thread role (1-byte bitfield) |
| `th/ds` | Active Operational Dataset (MeshCoP TLV, NL67/Matter) |
| `th/tc` | Thread network info TLV8 (legacy devices) |

**`th/nc`  -  node capabilities bitfield:**

| Bit | Meaning |
|---|---|
| `0x01` | Minimal |
| `0x02` | Sleepy |
| `0x04` | Full |
| `0x08` | Router-eligible |
| `0x10` | Border Router capable |

**`th/tr`  -  Thread role bitfield:**

| Bit | Meaning |
|---|---|
| `0x01` | Disabled |
| `0x02` | Detached |
| `0x04` | Joining |
| `0x08` | Child |
| `0x10` | Router |
| `0x20` | Leader |
| `0x40` | Border Router |

---

## Other TLV Tags

| Tag | Operation |
|---|---|
| `0x0201` | LTPDU access token (used during firmware update) |
| `0x0202` | Read EUI-64 |
| `0x0801` | Device control R/W wrapper |
| `0x0902` | Unknown |

### `0x0801` Device Control Sub-endpoints

```
TAG  L0   RW  TAG  L1   EP      TAG  L2   DATA
0801 LL   RW  0001 LL   <ascii> 0002 LL   <data>
```

`RW`: `0` = read, `1` = write.

| Endpoint | Type | Notes |
|---|---|---|
| `ac` | tlv8 | Circadian Lighting |
| `lglt` | 8 bytes | Circadian Lighting |
| `tm` | 8-byte uint | Current time (seconds since epoch) |
| `tz` | 8-byte int | Timezone offset in seconds |
| `ac/en` | ? | Unknown |
