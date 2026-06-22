# Huion Note X10 — Offline Sync Protocol Map

Reverse-engineered from a real offline sync (`captures/sync-01.btsnoop`), reduced
to ATT frames by `huion_notes/extract_gatt.py`, and cross-checked against the app's
decompiled decoder (`apk/jadx-out`, package `com.huion.hinotes`, **not obfuscated**).
End-to-end decode validated: `sync-01` renders the captured page as legible
handwriting (`huion_notes/decode_offline.py`).

Device VERSION string: **`HUION_T218_230819`** (internal model T218, fw 2023-08-19).

Status legend: ✅ confirmed (capture + source) · 🟡 hypothesis · ❌ open.

---

## 1. Transport: characteristics ✅

| Handle | Role | Direction | Notes |
|--------|------|-----------|-------|
| `0x002b` | **Command** | app→device `write_command`, device→app `indication` | 41 writes + 2 indications in `sync-01` |
| `0x0027` | **Data / notify** | device→app `notification` | 793 notifications — the bulk stream |
| `0x0003` | GATT Service Changed | — | standard, ignore |

Request/response RPC: app writes a command to `0x002b`; responses (incl. bulk
offline data) stream back as notifications on `0x0027`. ACK/flow-controlled.

## 2. Framing ✅

Every characteristic value is a delimited frame:

```
cd <opcode:1> <len:1> <payload…> [ed]
```

- `cd` start byte; `opcode` per OrderCode (§3); `len` = total frame length.
- Command writes are fixed 8 bytes ending in `ed` (`cd op 08 xxxxxxxx ed`).
- Data notifications are length-delimited (`len`), e.g. stroke packets are 126 bytes.
- The extractor's `Frame.value` is this whole `cd…` packet (handle value).

## 3. Opcodes (from `OrderCode.java`) ✅

| Hex | Dec | Name | Role in sync |
|-----|-----|------|--------------|
| `0x80` | 128 | HEART_BEAT | keepalive |
| `0x81` | 129 | VERIFY_CONNECT | handshake start |
| `0x82` | 130 | VERIFY_RESULT | handshake result |
| `0x84` | 132 | MODE | set mode |
| `0x85` | 133 | CURRENT_PAGE | page cursor |
| `0x86` | 134 | **REQUEST_OFFLINE_DATA** | app asks for stored pages |
| `0x87` | 135 | **RETURN_OFFLINE_DATA** | bulk stroke packets (§4) |
| `0x88` | 136 | GET_PAGE_PACKAGE | page/packet count |
| `0x8a` | 138 | NEXT_PAGE | advance page |
| `0x91` | 145 | DEVICE_NAME | name string |
| `0x93` | 147 | GET_PWD | password/bind (§6) |
| `0x95` | 149 | **MAX_DATA** | device limits MAX_X/Y/PRESS (§4) |
| `0x96` | 150 | SET_MANY_PACKET_DISTANCE | transfer tuning |
| `0xc9` | 201 | VERSION | `HUION_T218_230819` |

Also seen: `0x83` VERIFY_PWD(131), `0x8d` ONLINE_DATA(141), `0x8e` ELECTRICITY(142),
`0x8f` ROM(143), `0x92` SET_PWD(146). (Repeated `0x8f/0x8d` writes during transfer
are status/flow polling.)

## 4. Offline page data ✅

### MAX_DATA `0x95` — device limits
From `cd 95 0b 28 6e 00 18 92 00 ff 1f`, parsed as `HiBluetoothManager` does
(`MAX_X=u24(b5,b4,b3)`, `MAX_Y=u24(b8,b7,b6)`, `MAX_PRESS=u16(b10,b9)`):

- **MAX_X = 28200**, **MAX_Y = 37400**, **MAX_PRESS = 8191** (13-bit).

### RETURN_OFFLINE_DATA `0x87` — stroke packets
- Packet = `cd 87 7e <seq:u16 LE>` (**5-byte header**) + points + **1 checksum byte**.
- `seq` increments `0x0001…`; used only to confirm ordering.
- **Points are parsed per packet, NOT concatenated across packets**
  (`BluetoothUtil.decodePackagePoint`): `N = (len - 5) / 6`, remainder discarded.
  A 126-byte packet → 20 points + 1 checksum. (Concatenating across packets was
  the original decode bug — it carried the checksum byte forward and drifted.)

### 6-byte point record ✅ (from `BluePoint`)
```
x      = bytes[0] | bytes[1]<<8                 # LE, 0..MAX_X
y      = bytes[2] | bytes[3]<<8                 # LE, 0..MAX_Y
press  = bytes[4] | (bytes[5] & 0x1f)<<8        # 13-bit, 0..MAX_PRESS
status = bytes[5] >> 5                          # top 3 bits; 0 = pen up
```
Page mapping (non-A4 device — X10 is NOT `"Huion Tablet_T910"`, so no axis swap):
`page_x = x/MAX_X * PAGE_W`, `page_y = y/MAX_Y * PAGE_H`, **origin top-left, no flip**.
Pen-up (`status==0` or `press==0`) delimits strokes. `sync-01`: 13197 points → 176 strokes.

## 5. Reference implementation ✅

- `huion_notes/extract_gatt.py` — btsnoop → ATT frames (tested).
- `huion_notes/decode_offline.py` — frames → points → strokes → SVG (tested); reads
  MAX_* from the capture's `0x95` packet.

## 6. Bind / auth gate ✅ (local, replicable — no server)

**Challenge–response** (the connect handshake). The **client sends a `VERIFY_CONNECT
(0x81)` request first** (`cd 81 08 00 00 00 00 ed`, write to FFE2); the device then
replies `VERIFY_CONNECT (0x81)` (notification on FFE1) carrying a 3-byte challenge
`a,b,c` (bytes [3],[4],[5]). The device does **not** volunteer the challenge — it
must be polled (confirmed live + in `sync-multipage.btsnoop`). App → device replies
`VERIFY_RESULT (0x82)`:
```
cd 82 08 r1 r2 r3 00 ed
  r1 = ((a + b) << 2) % 255
  r2 = ((b + c) << 2) % 255
  r3 = ((c + 10) << 2) % 255
```
Pure arithmetic, **no key/secret**. (Matches captured `cd 82 08 42 fe 3d 00 ed`.)

Device → app `VERIFY_RESULT` status byte [3]: `0` fail · `1` ok, no PIN · `2` PIN required.

**Optional 6-digit PIN** (`VERIFY_PWD 0x83`), only when status==2. Sent as two
frames carrying `encodePwd(pin)`:
```
cd 83 08 01 e0 e1 e2 ed
cd 83 08 02 e3 e4 e5 ed
  e[i] = ascii(pin_digit[i]) + ascii("huion#")[i]      # offsets 104,117,105,111,110,35
```
Keyless; reproducible if the user knows their PIN. `sync-01` returned status 1 (no PIN).

**App-only gate:** the app additionally refuses to proceed unless a user is logged in
(`cookie`/`token`/`userinfo`). This is enforced **in the app, not the device** — a
Linux client skips it.

## 7. Command builders (for Sub-project B, from `HiBluetoothManager`) ✅

All commands are `cd <op> 08 <4 args> ed`, written to char `0x002b`:

| Method | Bytes |
|--------|-------|
| VERIFY_RESULT | `cd 82 08 r1 r2 r3 00 ed` (see §6) |
| VERIFY_PWD | `cd 83 08 01 e0 e1 e2 ed` + `cd 83 08 02 e3 e4 e5 ed` |
| requestMaxInfo (MAX_DATA) | `cd 95 08 00 00 00 00 ed` |
| requestSetManyPacketDistance | `cd 96 08 01 03 00 00 ed` + `cd 96 08 03 02 00 00 ed` |
| requestPwd (GET_PWD) | `cd 93 08 00 00 00 00 ed` |
| **requestPageData (REQUEST_OFFLINE_DATA)** | `cd 86 08 <page_lo> <page_hi> <idx> 00 ed` ← **dump trigger** |
| requestVersion | `cd c9 08 00 00 00 00 ed` |
| requestReGetPackageData (retransmit) | `cd 88 08 <page_lo> <page_hi> <idx_lo> <idx_hi> ed` (GET_PAGE_PACKAGE; reply `cd 88 7e <idx> …`) — see §10 |

Suggested Linux handshake: enable notify on `0x0027` + indications on `0x002b` →
answer `VERIFY_CONNECT` with `VERIFY_RESULT` → (PIN if status 2) → `requestMaxInfo`
→ `requestSetManyPacketDistance` → `requestPageData` → collect `0x87` → `decode_offline`.

## 8. Open items

- ~~Multi-page sync~~ ✅ **RESOLVED — see §10** (`captures/sync-multipage.btsnoop`).
- Pressure→width and the app's quad-Bézier smoothing (`bluePointToPathData`) — only
  needed for visual fidelity, not data recovery.
- Bind/auth (§6).
- Sub-project B: reproduce the dump live over `bleak` on Linux (issue
  `REQUEST_OFFLINE_DATA`, subscribe `0x0027`, decode).

## 9. Artifacts (gitignored — local only)

`captures/sync-01.btsnoop`, `sync-multipage.btsnoop`, `frames-01.jsonl`,
`decoded-final.svg/.png` (the page), `apk/reference/*.java` (decompiled source).

## 10. Multi-page offline sync ✅ (from `captures/sync-multipage.btsnoop` + `HiBluetoothManager`)

A 3+ page sync, cross-checked against the app's `requestPageData` /
`requestReGetPackageData` and the case-134/135/136 handlers. Pages are addressed
**directly by index** — `NEXT_PAGE` is NOT used in the sync loop (it is a
device→app "new page created" notice).

### Per-page download
1. `requestPageData(page, sub=0)` → `cd 86 08 <page_lo> <page_hi> <sub> 00 ed`
   (REQUEST_OFFLINE_DATA). [`HiBluetoothManager.requestPageData`]
2. Device replies `cd 86 05 <count_lo> <count_hi>` → `packageSum = count` (u16 LE). [case 134]
   - `count == 0` ⇒ page empty / no more pages ⇒ stop. [case 134, `packageSum == 0`]
3. Else device streams `count` × `RETURN_OFFLINE_DATA (0x87)`:
   `cd 87 7e <seq_lo> <seq_hi> <20 points> <checksum>`, **seq = 1..count, reset per page**.
   Complete when the packet with `seq == count` arrives. [case 135, `iUnifiedByte2 == packageSum`]

### Gap fill (retransmit) — opcode 0x88, NOT 0x86
For each missing `seq i`: `requestReGetPackageData(page, i)` →
`cd 88 08 <page_lo> <page_hi> <i_lo> <i_hi> ed` (GET_PAGE_PACKAGE). Device replies
`cd 88 7e <i_lo> <i_hi> <20 points> <checksum>` — same point layout as `0x87`, index
at bytes [3..4]. [`requestReGetPackageData` + case 136]

### Page iteration
Loop `page = 0,1,2,…`; advance when a page completes; stop when a page returns
`count == 0`. (The official app pre-knew the page count; `count == 0` is the safe
terminator — confirm against the device.)

### Other opcodes
- `0x8b DELETE_PAGE (139)`: delete one page by index
  (`cd 8b 08 <page_lo> <page_hi> 00 00 ed`; reply [3]==1 ⇒ ok). The extractor sends this by
  default after a page is exported and saved to disk (opt out with `--keep`). [case 139]
- `0x8c CLEAR_CACHE (140)`: end-of-session cache clear. [case 140]
- `0x8a NEXT_PAGE (138)`: device→app "page created" notice; unrelated to the dump. [case 138]
- `0x80`/`0x8d`/`0x8e`/`0x8f`/`0x91`: heartbeat / online-mode / battery / ROM-status /
  name polls; ignore during a dump.

### Validation
`sync-multipage.btsnoop` decoded cleanly: 6 page-streams (counts 661 / 4370 / 4370 /
18 / 24 / 14), **0 packets missing after retransmit** on every page. Auth re-confirmed
on a fresh challenge (28,63,239) → (109,188,231) = `cd 82 08 6d bc e7 00 ed`.
