# RU5600 Reader — Linux client for the INVETON RU5600 UHF RFID reader

Unofficial Linux replacement for the Windows `ReaderSoftV5.1` that ships with
the RU5600 SDK. It talks to the reader over USB and reproduces the layout of
the original program: connection panel on the left, tabbed parameter pages on
the right, timestamped log at the bottom.

![RU5600 Reader on Linux](docs/screenshot.png)

## What it is

* **Transport**: USB, via the vendor's own Linux library `libCFHidApi.so`
  (from `SDK_V3.2/X64/USB/Linux/HidDemo`), which is the same code as the
  Windows `SWHidApi.dll` but built on libusb-1.0.
* **GUI**: Python 3 + GTK 3 (PyGObject).
* **Packaging**: AppImage (x86_64).

The wire protocol was reverse engineered from `SWHidApi.dll` with REA/Ghidra
and is documented in [PROTOCOL.md](PROTOCOL.md).

## Requirements

* Linux x86_64
* Python 3 with PyGObject (`python3-gi`, `gir1.2-gtk-3.0`)
* `libusb-1.0-0` (a private copy is bundled as a fallback)

The reader must be reachable through USB. On Linux the kernel exposes it as a
plain USB device (`1a86:e010`, QinHeng SWUSB_HID), so a udev rule is needed to
use it without root:

```
SUBSYSTEM=="usb", ATTR{idVendor}=="1a86", ATTR{idProduct}=="e010", MODE="0660", GROUP="plugdev"
```

Install it with:

```bash
sudo install -m 644 packaging/99-inveton-ru5600.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

The app also has an **"Install USB access rule"** button that does this via
`pkexec` (it will ask for the administrator password).

## Build the AppImage

```bash
cd packaging
APPIMAGETOOL=/path/to/appimagetool ./build_appimage.sh ../dist
```

Produces `dist/ru5600-reader-1.0.0-x86_64.AppImage`.

## Run from source

```bash
python3 src/ru5600_gui.py
```

## Command line client

Handy for scripted tests and for checking tags without the GUI:

```bash
python3 src/ru5600_cli.py info
python3 src/ru5600_cli.py inventory
python3 src/ru5600_cli.py watch --seconds 30
python3 src/ru5600_cli.py read --mem EPC --ptr 0 --count 2
python3 src/ru5600_cli.py write --epc 3034333234353536373839303132
python3 src/ru5600_cli.py params
python3 src/ru5600_cli.py freq --band USA
```

## GUI map

| Tab | What it does |
|---|---|
| **ParameterSet** | RF power, frequency band, single frequency, work mode (answer/active), filter time, buzzer, inquiry area, start address / byte length, read time, keyboard output format. |
| **Read/Write** | Read a Gen2 memory bank (EPC/TID/USER) and write the EPC. |
| **ActiveMode** | Continuous read with a live tag table (antenna, EPC, RSSI, hit count, last seen). |
| **AdvanceSet** | Read/set the frequency band, reboot the reader. |
| **BatchWrite / WhiteList** | Not exposed by the vendor Linux API — not implemented. |

The **Read** button performs a one-shot inventory (answer mode).
The **Set** button applies the ParameterSet page to the reader.

## Verified against the physical reader

Everything below was exercised on a real RU5600 on the bench:

| Check | Result |
|---|---|
| `CFHid_OpenDevice` | OK |
| `GetDeviceSystemInfo` | SN `D304260702562B`, firmware `7.1`, hardware `1.1` |
| `ReadDeviceOneParam` | RF power `18` dBm, beep `1`, filter time `0` |
| `ReadFreq` / `SetFreq` | USA `31 80` → Europe `4E 00` → USA `31 80` (read back) |
| `SetDeviceOneParam` | work mode answer ↔ active |
| `StartRead` / `StopRead` | OK |
| `InventoryG2` / `GetTagBuf` | OK (returned 0 tags - none on the antenna) |
| `ReadCardG2` / `WriteEPCG2` | correct failure when no tag is in the field |
| GUI *Open* button | SN, firmware and hardware populate; log entry written |

**Not yet exercised**: reading a real tag (no UHF tag was on the antenna
during the bench test) and writing EPC to a tag.

## Notes

* The reader must be in **AnswerMode** for the one-shot `Read` button and in
  **ActiveMode** for the live table. The app switches work mode when you
  press *Start read*.
* **Only one program can use the reader at a time** - the USB interface is
  claimed exclusively. If *Open* fails while the reader is plugged in,
  close the other client (the CLI, or a second copy of this app).
* `libCFHidApi.so` is the vendor's file, redistributed as-is from the SDK for
  interoperability with the hardware.
