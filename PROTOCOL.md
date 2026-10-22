# PROTOCOL.md — INVETON RU5600 UHF RFID reader

Reverse-engineered wire protocol of the **RU5600** desktop reader
(`ReaderSoftV5.1` / `SWHidApi.dll`) and the reference for the Linux client
built on the vendor's own `libCFHidApi.so`.

---

## 1. What is what

| Artifact | Role |
|---|---|
| `ReaderSoftV5.1/ReaderSoft.exe` | MFC GUI (Windows). Calls `SWHidApi.dll` / `SWComApi.dll` / `SWNetApi.dll`. |
| `ReaderSoftV5.1/SWHidApi.dll` | USB (HID) transport, 79 exported `SWHid_*` functions. Holds the module framing. |
| `SDK_V3.2/X64/USB/Linux/HidDemo/libCFHidApi.so` | **Vendor's own Linux build** of the same API (`CFHid_*`), on top of **libusb-1.0**. |
| `SDK_V3.2/X64/USB/Linux/HidDemo/YPDHidApi.h` | Authoritative C prototypes + parameter documentation. |

**The Linux client uses `libCFHidApi.so`**, so the USB/HID layer and the
module protocol are the vendor's own code — only the GUI is reimplemented.

`libCFHidApi.so` has **no `DT_NEEDED` entry for libusb** (it was linked with
`-lusb` but the dependency was not recorded), so libusb must be present in the
global namespace before it is loaded:

```python
ctypes.CDLL("libusb-1.0.so.0", mode=ctypes.RTLD_GLOBAL)
ctypes.CDLL("libCFHidApi.so")
```

---

## 2. Module frame (the "SW/CT" protocol)

The reader speaks a small framed protocol. Two magic values:

* `0x53 0x57` (`"SW"`) — host → module
* `0x43 0x54` (`"CT"`) — module → host

### Request

```
offset  size  field
  0      2    magic 0x5357  ("SW")
  2      2    length (u16, big endian) = payload_len + 3
  4      1    0xFF
  5      1    command code
  6      n    payload (n bytes)
 6+n      1    checksum
```

Total frame size = `n + 7`.

### Response

```
offset  size  field
  0      2    magic 0x4354  ("CT")
  2      2    length (u16, big endian) = payload_len + 1
  4      1    0x00
  5      1    command code (echo of the request)
  6      1    status  (1 = ok)
  7      n    payload
 7+n      1    checksum
```

### Checksum

```c
uint8_t cksum(const uint8_t *buf, uint16_t len) {
    uint8_t s = 0;
    for (uint16_t i = 0; i < len; i++) s += buf[i];
    return (uint8_t)(~s + 1);          /* two's complement of the sum */
}
```

It covers every byte of the frame **except the checksum byte itself**.

Verified against the log window of the real Windows program:

```
Send   : 53 57 00 03 FF 20 34
Receive: 43 54 00 15 00 20 01 D3 55 00 00 00 00 0F 01 00 31 80 00 00 00 02 01 00 47
```

* request: magic `SW`, len `0x0003` = payload_len(0) + 3, cmd `0x20`,
  checksum `0x34`; `~0x1CC + 1 = 0x34` ✔
* response: magic `CT`, len `0x0015` = 21 = payload(20) + 1, cmd echo `0x20`,
  status `0x01`, checksum `0x47`; `~0x2B9 + 1 = 0x47` ✔

### Evidence (SWHidApi.dll, image base `0x10000000`)

| What | Where |
|---|---|
| checksum function `cksum()` | `0x10001140` |
| frame builder + send | `0x10001770` (`FUN_10001770`) |
| response wait/validate | `0x10001870` (`FUN_10001870`) |
| checksum verification vs. frame tail | `0x10002449`–`0x10002476` ("sum error=%d") |
| generic escape hatch | `SWHid_CommunicateWithModule` @ `0x100038a0` |

---

## 3. Command codes

| Code | API (`SWHid_*` / `CFHid_*`) | Payload | Response |
|---|---|---|---|
| `0x01` | `InventoryG2` | — | tag count + packed tags |
| `0x02` | `ReadCardG2` | password(4), bank, word ptr, word count | data |
| `0x10` | `GetDeviceSystemInfo` | — | 9 bytes: soft, hard, SN(7) |
| `0x16` | `ReadDeviceID` | 4 bytes | id |
| `0x20` | `ReadDeviceParam` | — | param block |
| `0x21` | `SetDeviceParam` | `[param][0x55][data]` | — |
| `0x23` / `0x24` | `ReadDeviceOneParam` / `SetDeviceOneParam` | `[addr]` / `[addr][value]` | value |
| `0x3E` / `0x3F` | `ReadFreq` / `SetFreq` | 2 bytes | 2 bytes |
| `0x41` | `StartRead` | — | — |
| `0x40` | `StopRead` | — | — |

`0xFE` is used by `SWHid_CommunicateWithModule` to push a pre-built frame
verbatim (raw escape hatch).

---

## 4. Device parameters

| Addr | Name | Values |
|---|---|---|
| `0x01` | Transport | USB / COM / Net |
| `0x02` | WorkMode | 0 = AnswerMode, 1 = ActiveMode |
| `0x03` | Device address | — |
| `0x04` | Filter time | seconds |
| `0x05` | RF power | dBm |
| `0x06` | Beep | 0 / 1 |
| `0x07` | UART baud rate | — |

---

## 5. Frequency table

`SetFreq` / `ReadFreq` take 2 raw bytes:

| Band | Bytes | | Band | Bytes |
|---|---|---|---|---|
| USA | `31 80` | | Hongkong | `2C A3` |
| Europe | `4E 00` | | Taiwan | `31 A7` |
| China | `2C A3` | | Canada | `31 80` |
| Korea | `29 9D` | | Mexico | `31 80` |
| Australia | `2E 9F` | | Brazil | `31 99` |
| New Zealand | `4E 00` | | Israel | `1C 99` |
| India | `4E 00` | | South Africa | `24 9D` |
| Singapore | `2C 81` | | Thailand | `2C A3` |
| | | | Malaysia | `28 A1` |
| | | | Japan | `29 9D` |

---

## 6. Tag buffer layout

Both `InventoryG2` (answer mode) and `GetTagBuf` (active mode) return a packed
buffer. Records are back to back:

```
offset  size  field
  0      1    record length (does NOT include this byte)
  1      1    type       bit 0x80 => record carries a 6 byte timestamp
  2      1    antenna
  3      m    tag id slot (see below), m = len - 3  (len - 9 with timestamp)
  ...    1    RSSI        (immediately before the 6 byte timestamp)
```

Next record starts at `offset += record_length + 1`.

Cross-checked against four independent vendor demos (C++ `main.cpp`, Delphi
`Unit1.pas`, VB `Form1.vb`, Python `UsbDemo.py`) — all four decode it the
same way.

### Measured records (firmware 7.1)

96 bit EPC tag, answer mode — **28 bytes total**:

```
1B 03 01 | E2 00 47 01 99 51 66 2B 03 9F D1 AD | 00 00 00 00 01 00 00 00 00 00 00 | 30
^  ^  ^   ^------------------- EPC (12 bytes) -----^  ^-------- reader metadata (12) ---^  ^
|  |  ant                                                        RSSI = 0x30 = 48
|  type = 0x03
len = 27
```

192 bit EPC tag, answer mode — **28 bytes total** as well, with a 24 byte EPC
slot:

```
1B 03 01 | E2 00 47 06 6B C0 60 27 AC 48 01 14 | 00 00 00 00 01 00 00 00 00 00 00 | 30
```

96 bit EPC tag, active mode — **16 bytes per record** (the reader repeats
the tag with a fresh RSSI):

```
0F 03 01 E2 00 47 01 99 51 66 2B 03 9F D1 AD 34
^  ^  ^  ^---------------- EPC (12) ------------^  ^ RSSI
|  |  ant
|  type
len = 15
```

### The ID slot is the whole EPC

The slot length follows the EPC length: 12 bytes for a 96 bit tag, 24 for a
192 bit one. It is **not** padded - the bytes past the EPC belong to the
EPC itself.

> An earlier revision of this client clamped the slot to 12 bytes and
> truncated 192 bit EPCs. That was wrong: a second tag with a 12 byte slot
> proved the slot length tracks the EPC length. `ReadCardG2` is the
> ground truth and always agrees with the slot.

The real EPC was confirmed against `ReadCardG2`:

```
ReadCardG2(bank=EPC, word=0, words=8) -> 55B334BF | E20047019951662B039FD1AD
                                        ^^^^^^^^   ^--- EPC starts at word 2
                                        CRC + PC header
ReadCardG2(bank=TID, word=0, words=6) -> E2803813 | 2000662B039FD1AD
```

Note the Gen2 relationship: the last 6 bytes of the TID (`00662B039FD1AD`)
are the last 6 bytes of the EPC (`00662B039FD1AD`).

Cross-checked against four independent vendor demos (C++ `main.cpp`, Delphi
`Unit1.pas`, VB `Form1.vb`, Python `UsbDemo.py`) — all four decode the
record the same way.

> Open item: the reader's *access password* is a 4 byte parameter of the
> read command. With `00000000` the read succeeds; **any** other value
> fails 100 % of the time (0/14 attempts), so a wrong password is easy to
> recognise.

---

## 6.1 Command pacing (critical)

The reader **drops commands that arrive while it is still busy**. Measured
on the bench:

| Gap between two commands | Answer-mode inventory success |
|---|---|
| 0 ms (back to back) | 0 / 15 |
| 50 ms | ~4 / 8 |
| 60 ms (what this client uses) | 6 / 6 |

A single answer-mode inventory round takes only ~3 ms, and roughly half of
the rounds are refused even with a gap. The client therefore

* waits **60 ms** between any two commands (`Ru5600Reader.MIN_GAP`),
* waits **450 ms** after a state changing command - set parameter, set
  frequency, start/stop reading, write (`Ru5600Reader.SETTLE`),
* retries the inventory and the memory reads.

### Memory reads only answer in bursts

A single `ReadCardG2` succeeds only **~20 %** of the time. The successes
come in bursts - roughly 4 consecutive accepted commands every ~3.3 s:

```
t = 0.07 3.06 5.78 6.43 6.61 9.32 9.51 9.70 9.89 13.34 13.53 16.71 16.90 ...
```

Retrying every **30 ms** turns it into a reliable operation (measured 10/10
successful reads, 0.4 - 2.4 s per read). The client does exactly that
(`ReadCardG2` with `retries=40`).

### Active mode is the reliable detector

The answer mode inventory is refused far more often than active mode. The
client therefore scans with a short **active** dwell and drains the tag
buffer:

```
set work mode = Active -> clear tag buffer -> start read
-> dwell 350 ms -> stop read -> GetTagBuf
```

That returns the tag 2-5 times per scan, every time. The one shot *Read*
button of the GUI uses this path; the *ActiveMode* tab keeps the reader
streaming and fills the table from the library callback.

A one shot inventory also requires the reader to be in **answer mode**
(work mode parameter `0x02` = 0). The client switches back to answer mode
after stopping an active read.

---

## 7. HID layer (Windows transport, for reference)

`libCFHidApi.so` replaces this with libusb, but for completeness:

* `CH9326WriteData` (`SWHidApi.dll` @ `0x100015a0`): builds
  `[report_id=0][length][payload...]` and issues one `WriteFile`.
* `ReadUSBData` (`0x10001450`): `ReadFile` into a 256 byte buffer, the first
  byte of the payload is the length.
* Frames longer than **31 bytes** are split into 31 byte chunks before
  writing (`0x100016b0`).

The reader enumerates as a **WCH QinHeng `1a86:e010` "SWUSB_HID"** device
(HID interface + vendor interface). The SDK ships the matching udev rule.

---

## 8. Open items

* The **batch write** and **whitelist** commands are exposed by the Windows
  DLL but not by `libCFHidApi.so`; they are not implemented in this client.
* Reading and writing a **physical tag** was not exercised during the bench
  test (the reader had no tag on its antenna). The tag record decoder matches
  four independent vendor demos, but it has not been confirmed against real
  tag data yet.

## 9. Chip identification (TID / GS1 TDS)

The TID memory bank identifies the chip. The first four bytes are decoded
following the GS1 EPC Tag Data Standard:

```
byte 0      0xE2                     ISO/IEC 18000-6C class
byte 1      bit 7  = XTID indicator
            bit 6  = security (Authenticate / Challenge)
            bit 5  = file (FileOpen)
byte 1..2   MDID  = (byte1 << 4) | (byte2 >> 4)      12 bit, the manufacturer
byte 2..3   TMN   = ((byte2 & 0x0F) << 8) | byte3    12 bit, the chip model
```

Both formulas were verified against the published TMN values (NXP UCODE 8 =
`0x894`, UCODE 9 = `0x995`, UCODE 8M = `0x994`, Impinj Monza M730 = `0x191`,
M750 = `0x190`, R6 = `0x160`).

The tag that was on the bench:

```
TID : E2 80 38 13 20 | 00 66 2B 03 9F D1 AD | 32 30 01 18 30 1C 00 F0 03 03 0C 53 CA 62
       ^^^^^^^^^^^^^   ^^^^^^^^^^^^^^^^^^^   ^^^^^^^^^^^^^^^^^ XTID (dynamic bytes)
       MDID 0x803      serial (7 bytes)
       = Alien         TMN 0x813
```

`E2803813` is the published prefix of the **Alien Higgs-10 (H10)**, the
chip sold as the 7320 / H3 class: EPC 96 (128) bit, **user memory 32 bit**,
32 bit access and kill password. The public tables that list the H10 as
`E2803813` also say it has 32 bits of user memory - which explains the
benchmark result that the user memory stops after 2 words: the chip
simply has 4 bytes of user memory, it is **not locked**.

The client ships a table of known prefixes (`TID_PREFIX_DB` in
`ru5600_chip.py`) plus the GS1 MDID list, and reports the model, the
serial and the XTID.

### Password

The access password is the 4 byte parameter of the read command. On this
tag `00000000` works and **every** other value fails on every single
attempt (0/14), so a wrong password is easy to recognise:

```
password 00000000  -> EPC read 3/12 ok
password E3700001  -> EPC read 0/12
password 00000001  -> EPC read 0/12
```

The *Test password* button in the *Chip info* tab uses exactly that
property, so a candidate can be verified without unlocking anything.

### Dynamic bytes

Two areas change on every read and are not a stable identifier:

* the XTID byte at offset 15 (`0x17`, `0x18`, `0x19`, ...) - a counter,
* the reserved words 4-5 (`E370`, `74D1`, `5B33`, ...) - the Alien
  "dynamic authentication" fingerprint.

Words 6-7 of the reserved bank (`0002 0000`) are stable.

---

## 10. Read modes and the antenna measurement

Two primitives, measured on the bench:

**One detection** (`detect(mode)`) - the answer mode inventory with a
bounded number of rounds:

| Mode | Rounds | Time | Hit rate |
|---|---|---|---|
| `burst` | 1 | 60 ms | 1/8 - the reader refuses the round most of the time |
| `balanced` | 6 | 0,2-0,4 s | 5/8 |
| `reliable` | 40 | 0,3-2,4 s | 8/8 (measured 10/10) |

The gap between the rounds is 30 ms; the reader answers roughly one
command out of four, and the answers come in bursts.

**Continuous measurement** (`measure(duration)`) - the reader is switched
to active mode and left streaming while the tag buffer is polled. This
is the primitive for mapping the reading area of an antenna:

```
detections : 51 in 10 s (5.1/s)
RSSI       : avg 71.5, min 45, max 90
```

The RSSI of the tag records is the signal strength reported by the
reader, and the detection rate is how often the tag is seen - together
they describe how well a spot of the antenna reads.

---

## 11. The network transport (TCP)

`SWNetApi.dll` from the Windows SDK was decompiled to find out how the
networked antennas talk.  The module protocol is **identical** to the USB
one - only the transport changes:

| function | command | response type |
|---|---|---|
| `SWNet_InventoryG2` | 0x01 | 0x01 |
| `SWNet_ReadCardG2` | 0x02 | 0x02 |
| `SWNet_GetDeviceSystemInfo` | 0x10 | 0x10 |
| `SWNet_ReadDeviceOneParam` | 0x23 | 0x23 |
| `SWNet_SetDeviceOneParam` | 0x24 | 0x24 |
| `SWNet_ReadFreq` | 0x3E | 0x3E |
| `SWNet_StartRead` | 0x41 | 0x41 |

Request frame (the same buffer the vendor builds):

```
[0] 53 57      magic
[2] len        u16 BE = payload + 3
[4] FF         fixed
[5] cmd        the command code
[6] payload
..  checksum
```

The client sends it with a two byte length prefix, and parses the answer
by looking for the `CT` magic, so a framed or an unframed answer both
work:

```
[0] 43 54      magic
[2] len        u16 BE, counts everything up to the checksum
[4] 00
[5] type       the command that is answered
[6] status     01 = ok
[7] count      u16 BE, number of tags (inventory)
[9] payload
..  checksum
```

`SWNet_OpenDevice(char *ip)` connects a TCP socket to the antenna
(`inet_addr` + `connect`, 500 ms socket timeout), so the client is the
one that connects - the antenna is the server.

---

## 12. WhiteList and batch write

They are **not** missing from the protocol, only from the Linux library.
The Windows DLL exports them and their command codes are now known:

| command | function | payload |
|---|---|---|
| 0xE8 | `ReadWhiteListCnt` | - |
| 0xE9 | `ReadWhiteList` | index (u16 BE) |
| 0xEA | `WriteWhiteList` | `[len][data]` |
| 0xEB | `DelAllWhiteList` | - |
| 0xEC | `DelOneWhiteList` | `[len][data]` |

Two ways to reach them from Linux:

1. **Over TCP** - the client builds the frames itself, so the commands
   can be sent right away.
2. **Over USB** - `libCFHidApi.so` exports `CFHid_SendCommand(cmd, ...)`
   and `CFHid_GetSpecificPocket(cmd, timeout)`, the raw escape hatch that
   takes a command code instead of a typed function.

So there is no need to ask the factory for anything; it is an afternoon
of work.  What is missing is the *semantics* of the payload (what the
white list entries look like), which the vendor does not document - that
part would need the Windows program as a reference.

---

## 13. Hardware verification log

| Test | Reader answer |
|---|---|
| `GetDeviceSystemInfo` | `71 11 D3 04 26 07 02 56 2B` → soft 7.1, hard 1.1, SN `D304260702562B` |
| `ReadDeviceOneParam(0x05)` | 18 (dBm) |
| `ReadDeviceOneParam(0x06)` | 1 (beep) |
| `ReadDeviceOneParam(0x04)` | 0 (filter time) |
| `ReadFreq` | `31 80` (USA) |
| `SetFreq("Europe")` → `ReadFreq` | `4E 00` ✔ matches the documented table |
| `SetFreq("USA")` → `ReadFreq` | `31 80` ✔ |
| `SetDeviceOneParam(0x02, 1/0)` | work mode switches between active and answer |
| `StartRead` / `StopRead` | succeed |
| `InventoryG2` | succeeds, 0 tags (no tag on the antenna) |
| `ReadCardG2` / `WriteEPCG2` | fail as expected with no tag in the field |

The frequency round trip is the strongest confirmation of the command table:
the bytes the SDK header documents are exactly what the reader stores and
returns.
