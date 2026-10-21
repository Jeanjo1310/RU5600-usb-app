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
  3      m    tag id (EPC or TID),  m = len - 1   (len - 7 with timestamp)
  ...    6    timestamp (only when type & 0x80)
  ...    1    RSSI
```

Next record starts at `offset += record_length + 1`.

Cross-checked against four independent vendor demos (C++ `main.cpp`, Delphi
`Unit1.pas`, VB `Form1.vb`, Python `UsbDemo.py`) — all four decode it the
same way.

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

## 9. Hardware verification log

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
