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
App  <-                   enc( TLV(0x01F1, 0x00)      )
```

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

Inner TLV tags sent inside `TLV(0x0002, TLV(tag, data))` on the `ci` endpoint:

| Tag | Operation |
|---|---|
| `0x0701` | Preview scene (display without saving) |
| `0x0702` | Add (persist) scene |
| `0x0703` | List scene handles |
| `0x0704` | Get scene details |
| `0x0705` | Delete scene |
| `0x0706` | Execute (play) scene |
| `0x0707` | Get currently executing scene |

Scenes are only accessible via LTPDU  -  not via Matter.

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
