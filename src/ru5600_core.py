#!/usr/bin/env python3
"""RU5600 / INVETON UHF reader - core transport binding.

Talks to the vendor's own cross-platform USB library (libCFHidApi.so), which
is the Linux build of the same API that ships as SWHidApi.dll in the Windows
ReaderSoftV5.1 SDK.  The library speaks the reader's proprietary module
protocol over libusb, so we do not reimplement the USB layer.

See PROTOCOL.md for the wire format and the reverse-engineering evidence.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
# Vendor libraries live next to this module inside the AppImage
# (/opt/ru5600-reader/vendor) and one level up in the source tree.
_VENDOR_CANDIDATES = (
    os.path.join(HERE, "vendor"),
    os.path.join(os.path.dirname(HERE), "vendor"),
)
VENDOR_DIR = next((d for d in _VENDOR_CANDIDATES if os.path.isdir(d)),
                  _VENDOR_CANDIDATES[0])

# Device address used by every vendor demo: 0xFF == broadcast.
DEV_ADDR = 0xFF

# Device parameter addresses (documented in the SDK demos).
PARAM_TRANSPORT = 0x01
PARAM_WORKMODE = 0x02
PARAM_DEVADDR = 0x03
PARAM_FILTERTIME = 0x04
PARAM_RFPOWER = 0x05
PARAM_BEEP = 0x06
PARAM_UARTBAUD = 0x07

WORKMODE_ANSWER = 0x00
WORKMODE_ACTIVE = 0x01

# Memory banks for Gen2 read/write.
MEM_RESERVED = 0
MEM_EPC = 1
MEM_TID = 2
MEM_USER = 3

# Frequency table: band -> the 2 raw bytes the reader expects.
# Values transcribed from the SDK header (CFHid_SetFreq documentation).
FREQ_BANDS = {
    "USA": (0x31, 0x80),
    "Europe": (0x4E, 0x00),
    "China": (0x2C, 0xA3),
    "Korea": (0x29, 0x9D),
    "Australia": (0x2E, 0x9F),
    "New Zealand": (0x4E, 0x00),
    "India": (0x4E, 0x00),
    "Singapore": (0x2C, 0x81),
    "Hongkong": (0x2C, 0xA3),
    "Taiwan": (0x31, 0xA7),
    "Canada": (0x31, 0x80),
    "Mexico": (0x31, 0x80),
    "Brazil": (0x31, 0x99),
    "Israel": (0x1C, 0x99),
    "South Africa": (0x24, 0x9D),
    "Thailand": (0x2C, 0xA3),
    "Malaysia": (0x28, 0xA1),
    "Japan": (0x29, 0x9D),
}


class ReaderError(RuntimeError):
    """Raised when the reader is unreachable or rejects a command."""


EPC_SLOT_BYTES = 12
"""Reference size of the 96 bit EPC slot."""


@dataclass
class Tag:
    """One Gen2 tag as reported by the reader.

    The reader reports the tag id in a variable length slot that holds the
    **whole EPC** (12 bytes for a 96 bit tag, 24 bytes for a 192 bit one) -
    there is no padding.  ``raw_id`` keeps the slot as received.
    """

    type: int = 0
    ant: int = 0
    epc: bytes = b""
    rssi: int = 0
    timestamp: Optional[bytes] = None
    raw: bytes = b""
    raw_id: bytes = b""

    @property
    def epc_hex(self) -> str:
        return self.epc.hex().upper()

    @property
    def has_timestamp(self) -> bool:
        return bool(self.type & 0x80)


@dataclass
class SystemInfo:
    soft_ver: str = ""
    hard_ver: str = ""
    device_sn: str = ""


# --------------------------------------------------------------------------
# ctypes bindings
# --------------------------------------------------------------------------

_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_int,  # msg: 0 insert, 1 removed, 2 data
    ctypes.c_int,  # param1: tag count
    ctypes.POINTER(ctypes.c_ubyte),  # param2: tag buffer
    ctypes.c_int,  # param3: buffer length
    ctypes.POINTER(ctypes.c_ubyte),  # param4: device SN
)


def _load_libraries():
    """Load libusb (global) then libCFHidApi.

    libCFHidApi.so has no DT_NEEDED entry for libusb, it was linked against
    libusb but expects the symbols to be present in the global namespace.
    """
    candidates = []
    found = ctypes.util.find_library("usb-1.0")
    if found:
        candidates.append(found)
    bundled = os.path.join(VENDOR_DIR, "libusb-1.0.so.0")
    if os.path.exists(bundled):
        candidates.append(bundled)

    usb = None
    last_err = None
    for cand in candidates:
        try:
            usb = ctypes.CDLL(cand, mode=ctypes.RTLD_GLOBAL)
            break
        except OSError as exc:  # pragma: no cover - depends on host
            last_err = exc
    if usb is None:
        raise ReaderError(
            "libusb-1.0 not found. Install 'libusb-1.0-0' or keep "
            "vendor/libusb-1.0.so.0 next to the app."
        ) from last_err

    lib = None
    for cand in (
        os.path.join(VENDOR_DIR, "libCFHidApi.so"),
        "libCFHidApi.so",
    ):
        try:
            lib = ctypes.CDLL(cand)
            break
        except OSError as exc:  # pragma: no cover - depends on host
            last_err = exc
    if lib is None:
        raise ReaderError(
            "libCFHidApi.so not found. Expected it in vendor/ next to the app."
        ) from last_err
    return lib


_LIB = None
_LOCK = threading.RLock()


def _lib():
    global _LIB
    with _LOCK:
        if _LIB is None:
            _LIB = _load_libraries()
            _declare_signatures(_LIB)
        return _LIB


def _declare_signatures(lib) -> None:
    u8 = ctypes.c_ubyte
    u8p = ctypes.POINTER(ctypes.c_ubyte)
    u16p = ctypes.POINTER(ctypes.c_ushort)

    sig = {
        "CFHid_OpenDevice": ([], ctypes.c_ubyte),
        "CFHid_CloseDevice": ([], ctypes.c_ubyte),
        "CFHid_GetDeviceSystemInfo": ([u8, u8p], ctypes.c_ubyte),
        "CFHid_ReadDeviceOneParam": ([u8, u8, u8p], ctypes.c_ubyte),
        "CFHid_SetDeviceOneParam": ([u8, u8, u8], ctypes.c_ubyte),
        "CFHid_StopRead": ([u8], ctypes.c_ubyte),
        "CFHid_StartRead": ([u8], ctypes.c_ubyte),
        "CFHid_InventoryG2": ([u8, u8p, u16p, u16p], ctypes.c_ubyte),
        "CFHid_WriteEPCG2": ([u8, u8p, u8p, u8], ctypes.c_ubyte),
        "CFHid_ReadCardG2": ([u8, u8p, u8, u8, u8, u8p], ctypes.c_ubyte),
        "CFHid_WriteCardG2": ([u8, u8p, u8, u8, u8, u8p], ctypes.c_ubyte),
        "CFHid_RelayOn": ([u8], ctypes.c_ubyte),
        "CFHid_RelayOff": ([u8], ctypes.c_ubyte),
        "CFHid_SetFreq": ([u8, u8p], ctypes.c_ubyte),
        "CFHid_ReadFreq": ([u8, u8p], ctypes.c_ubyte),
        "CFHid_SetCallback": ([_CB], ctypes.c_int),
        "CFHid_GetTagBuf": ([u8p, u16p, u16p], ctypes.c_ubyte),
        "CFHid_ClearTagBuf": ([], ctypes.c_ubyte),
        "CFHid_RebootDevice": ([u8], ctypes.c_ubyte),
        "CFHid_LockCardG2": ([u8, u8p, u8, u8, u8, u8p, u8], ctypes.c_ubyte),
        "CFHid_ReadDeviceID": ([u8, u8p], ctypes.c_ubyte),
        "CFHid_SetDeviceID": ([u8, u8p], ctypes.c_ubyte),
        "CFHid_ReadDeviceTime": ([u8, u8p], ctypes.c_ubyte),
        "CFHid_SetDeviceTime": ([u8, u8p], ctypes.c_ubyte),
        "CFHid_ReadDeviceParam": ([u8, u8p], ctypes.c_ubyte),
        "CFHid_SetDeviceParam": ([u8, u8p], ctypes.c_ubyte),
    }
    for name, (argtypes, restype) in sig.items():
        fn = getattr(lib, name, None)
        if fn is None:
            continue
        fn.argtypes = argtypes
        fn.restype = restype


def parse_tag_buffer(buf: bytes, count: int) -> List[Tag]:
    """Decode the reader's packed tag buffer.

    Layout per record (repeated ``count`` times):
        [0]      record length (excludes this byte)
        [1]      type   (bit 0x80 => record carries a 6 byte timestamp)
        [2]      antenna
        [3..]    tag id (EPC or TID)
        [n-6..n-1] optional timestamp (6 bytes)
        [n]      RSSI
    """
    tags: List[Tag] = []
    off = 0
    for _ in range(count):
        if off >= len(buf):
            break
        pack_len = buf[off]
        if pack_len < 3 or off + 1 + pack_len > len(buf):
            break
        rec = buf[off + 1: off + 1 + pack_len]
        has_ts = bool(rec[0] & 0x80)
        id_len = pack_len - 7 if has_ts else pack_len - 1
        if id_len < 2:
            id_len = max(pack_len - 1, 0)
        epc = bytes(rec[2:id_len]) if id_len > 2 else b""
        ts = bytes(rec[id_len:id_len + 6]) if has_ts and id_len + 6 <= pack_len else None
        tags.append(
            Tag(
                type=rec[0],
                ant=rec[1],
                epc=epc,
                rssi=rec[id_len] if id_len < pack_len else 0,
                timestamp=ts,
                raw=bytes(rec),
                raw_id=epc,
            )
        )
        off += pack_len + 1
    return tags


class Ru5600Reader:
    """Thin, thread-safe wrapper over the vendor USB API.

    The reader drops commands that arrive while it is still busy with the
    previous one, so every call is paced: a small gap between any two
    commands and a longer settle after a command that changes reader state
    (set parameter, set frequency, start/stop reading, write).
    """

    MIN_GAP = 0.06   # seconds between two consecutive commands
    SETTLE = 0.45   # seconds to wait after a state changing command

    def __init__(self, on_tags: Optional[Callable[[List[Tag]], None]] = None,
                 on_event: Optional[Callable[[str], None]] = None):
        self._lib = _lib()
        self._on_tags = on_tags
        self._on_event = on_event
        self._cb = None
        self._open = False
        self._lock = threading.RLock()
        self._last_cmd = 0.0

    def _pace(self, settle: bool = False) -> None:
        """Wait until the reader is ready for the next command."""
        need = self.SETTLE if settle else self.MIN_GAP
        elapsed = time.monotonic() - self._last_cmd
        if elapsed < need:
            time.sleep(need - elapsed)
        self._last_cmd = time.monotonic()

    # -- lifecycle ---------------------------------------------------------
    def open(self, attempts: int = 3, retry_delay: float = 0.4) -> bool:
        """Open the reader.

        The USB interface is claimed exclusively, so a previous client that
        is still shutting down can make the first attempt fail.  Retry a few
        times before giving up.
        """
        with self._lock:
            if self._open:
                return True
            for attempt in range(attempts):
                if self._lib.CFHid_OpenDevice() == 1:
                    self._open = True
                    self._last_cmd = time.monotonic()
                    self._install_callback()
                    return True
                if attempt + 1 < attempts:
                    time.sleep(retry_delay)
            raise ReaderError(
                "Open failed: the reader could not be claimed.\n"
                "Either it is not connected, the udev rule is missing "
                "(press 'Install USB access rule'), or another program "
                "already has it open - only one client can use the "
                "reader at a time."
            ) from None

    def close(self) -> bool:
        with self._lock:
            if not self._open:
                return False
            try:
                self._lib.CFHid_SetCallback(_CB(0))
            except Exception:
                pass
            self._cb = None
            self._lib.CFHid_CloseDevice()
            self._open = False
            return True

    @property
    def is_open(self) -> bool:
        return self._open

    def _install_callback(self) -> None:
        def _cb(msg, param1, param2, param3, param4):
            try:
                if msg == 2 and param1 > 0 and param2:
                    raw = ctypes.string_at(param2, param3)
                    tags = parse_tag_buffer(raw, param1)
                    if tags and self._on_tags:
                        self._on_tags(tags)
            except Exception:
                pass

        self._cb = _CB(_cb)
        self._lib.CFHid_SetCallback(self._cb)

    # -- device information ------------------------------------------------
    def system_info(self) -> SystemInfo:
        self._pace()
        buf = (ctypes.c_ubyte * 16)()
        if self._lib.CFHid_GetDeviceSystemInfo(DEV_ADDR, buf) != 1:
            raise ReaderError("GetDeviceSystemInfo failed")
        raw = bytes(buf)
        return SystemInfo(
            soft_ver=f"{raw[0] >> 4}.{raw[0] & 0x0F}",
            hard_ver=f"{raw[1] >> 4}.{raw[1] & 0x0F}",
            device_sn=raw[2:9].hex().upper(),
        )

    def read_param(self, addr: int) -> int:
        self._pace()
        val = ctypes.c_ubyte(0)
        if self._lib.CFHid_ReadDeviceOneParam(DEV_ADDR, addr, ctypes.byref(val)) != 1:
            raise ReaderError(f"ReadDeviceOneParam(0x{addr:02X}) failed")
        return val.value

    def set_param(self, addr: int, value: int) -> bool:
        self._pace()
        if self._lib.CFHid_SetDeviceOneParam(DEV_ADDR, addr, value) != 1:
            raise ReaderError(f"SetDeviceOneParam(0x{addr:02X}, {value}) failed")
        self._pace(settle=True)
        return True

    # -- RF ----------------------------------------------------------------
    def rf_power(self) -> int:
        return self.read_param(PARAM_RFPOWER)

    def set_rf_power(self, dbm: int) -> bool:
        return self.set_param(PARAM_RFPOWER, dbm)

    def work_mode(self) -> int:
        return self.read_param(PARAM_WORKMODE)

    def set_work_mode(self, mode: int) -> bool:
        return self.set_param(PARAM_WORKMODE, mode)

    def beep(self) -> int:
        return self.read_param(PARAM_BEEP)

    def set_beep(self, on: bool) -> bool:
        return self.set_param(PARAM_BEEP, 1 if on else 0)

    def filter_time(self) -> int:
        return self.read_param(PARAM_FILTERTIME)

    def set_filter_time(self, seconds: int) -> bool:
        return self.set_param(PARAM_FILTERTIME, seconds)

    def read_freq(self) -> bytes:
        self._pace()
        buf = (ctypes.c_ubyte * 2)()
        if self._lib.CFHid_ReadFreq(DEV_ADDR, buf) != 1:
            raise ReaderError("ReadFreq failed")
        return bytes(buf)

    def set_freq(self, band: str) -> bool:
        if band not in FREQ_BANDS:
            raise ReaderError(f"unknown frequency band: {band}")
        lo, hi = FREQ_BANDS[band]
        self._pace()
        buf = (ctypes.c_ubyte * 2)(lo, hi)
        if self._lib.CFHid_SetFreq(DEV_ADDR, buf) != 1:
            raise ReaderError(f"SetFreq({band}) failed")
        self._pace(settle=True)
        return True

    # -- inventory / read / write -----------------------------------------
    def inventory(self, rounds: int = 12, delay: float = 0.03) -> List[Tag]:
        """One-shot inventory (answer mode).

        A single round only takes ~3 ms, so it catches a tag only some of
        the time.  Repeat the round until something is found or the budget
        is exhausted, and merge the results.
        """
        buf = (ctypes.c_ubyte * 65536)()
        total = ctypes.c_ushort(0)
        count = ctypes.c_ushort(0)
        seen: List[Tag] = []
        keys = set()
        for _ in range(max(1, rounds)):
            self._pace()
            if self._lib.CFHid_InventoryG2(
                DEV_ADDR, buf, ctypes.byref(total), ctypes.byref(count)
            ) == 1 and count.value:
                for tag in parse_tag_buffer(bytes(buf[: total.value]), count.value):
                    key = (tag.epc_hex, tag.ant)
                    if key not in keys:
                        keys.add(key)
                        seen.append(tag)
                if seen:
                    break
            if delay:
                time.sleep(delay)
        return seen

    def scan(self, dwell: float = 0.35, attempts: int = 4) -> List[Tag]:
        """Reliable one shot scan.

        The answer mode inventory is refused by the reader most of the
        time (it only answers in narrow windows).  Switching to active
        mode for a short dwell and draining the tag buffer is what the
        reader answers reliably.  The dwell is repeated until something
        shows up or the attempt budget runs out.
        """
        for attempt in range(max(1, attempts)):
            self._pace()
            self._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                              WORKMODE_ACTIVE)
            self._pace(settle=True)
            self._lib.CFHid_ClearTagBuf()
            self._pace()
            self._lib.CFHid_StartRead(DEV_ADDR)
            self._pace(settle=True)
            time.sleep(dwell)
            self._lib.CFHid_StopRead(DEV_ADDR)
            self._pace(settle=True)
            tags = self.poll_tags()
            if tags:
                return tags
        return []

    def poll_tags(self) -> List[Tag]:
        """Drain the tag buffer filled by active mode."""
        buf = (ctypes.c_ubyte * 65536)()
        total = ctypes.c_ushort(0)
        count = ctypes.c_ushort(0)
        ret = self._lib.CFHid_GetTagBuf(buf, ctypes.byref(total), ctypes.byref(count))
        if not ret or count.value == 0:
            return []
        return parse_tag_buffer(bytes(buf[: total.value]), count.value)

    def start_read(self) -> bool:
        self._pace()
        if self._lib.CFHid_StartRead(DEV_ADDR) != 1:
            raise ReaderError("StartRead failed")
        self._pace(settle=True)
        return True

    def stop_read(self) -> bool:
        self._pace()
        if self._lib.CFHid_StopRead(DEV_ADDR) != 1:
            raise ReaderError("StopRead failed")
        self._pace(settle=True)
        self._lib.CFHid_ClearTagBuf()
        self._pace()
        return True

    def clear_tag_buf(self) -> bool:
        self._lib.CFHid_ClearTagBuf()
        return True

    def read_card(self, mem: int = MEM_EPC, word_ptr: int = 0,
                  word_count: int = 6, password: bytes = b"\x00" * 4,
                  retries: int = 40, retry_delay: float = 0.03) -> bytes:
        """Read a Gen2 memory bank.

        ``word_count`` is in 16 bit words: 6 words = 12 bytes.

        The reader answers a memory read only inside short windows (about
        every 3 s it accepts a burst of a few commands), so a single call
        succeeds only ~20 % of the time.  Retrying every 30 ms makes it
        reliable; a wrong ``password`` fails every single time.
        """
        data = (ctypes.c_ubyte * 256)()
        pwd = (ctypes.c_ubyte * 4)(*password)
        for attempt in range(max(1, retries)):
            if attempt:
                time.sleep(retry_delay)
            else:
                self._pace()
            if self._lib.CFHid_ReadCardG2(
                DEV_ADDR, pwd, mem, word_ptr, word_count, data
            ) == 1:
                return bytes(data[: word_count * 2])
        raise ReaderError(
            f"ReadCardG2 failed (bank={mem}, word={word_ptr}, "
            f"words={word_count}, {retries} attempts). Is a tag on the "
            f"antenna, and is the access password correct?"
        ) from None

    def write_card(self, data: bytes, mem: int = MEM_EPC, word_ptr: int = 0,
                   password: bytes = b"\x00" * 4, retries: int = 3) -> bool:
        if len(data) % 2:
            raise ReaderError("data length must be even (whole 16-bit words)")
        word_count = len(data) // 2
        pwd = (ctypes.c_ubyte * 4)(*password)
        buf = (ctypes.c_ubyte * len(data))(*data)
        for _ in range(max(1, retries)):
            self._pace()
            if self._lib.CFHid_WriteCardG2(
                DEV_ADDR, pwd, mem, word_ptr, word_count, buf
            ) == 1:
                self._pace(settle=True)
                return True
            time.sleep(0.05)
        raise ReaderError(
            f"WriteCardG2 failed (bank={mem}, word={word_ptr}, "
            f"words={word_count}). Is a tag on the antenna?"
        ) from None

    def write_epc(self, epc: bytes, password: bytes = b"\x00" * 4,
                  retries: int = 3) -> bool:
        pwd = (ctypes.c_ubyte * 4)(*password)
        buf = (ctypes.c_ubyte * len(epc))(*epc)
        for _ in range(max(1, retries)):
            self._pace()
            if self._lib.CFHid_WriteEPCG2(DEV_ADDR, pwd, buf, len(epc)) == 1:
                self._pace(settle=True)
                return True
            time.sleep(0.05)
        raise ReaderError("WriteEPCG2 failed. Is a tag on the antenna?") from None

    # -- misc --------------------------------------------------------------
    def relay(self, on: bool) -> bool:
        fn = self._lib.CFHid_RelayOn if on else self._lib.CFHid_RelayOff
        if fn(DEV_ADDR) != 1:
            raise ReaderError("Relay command failed")
        return True

    def reboot(self) -> bool:
        if self._lib.CFHid_RebootDevice(DEV_ADDR) != 1:
            raise ReaderError("RebootDevice failed")
        return True

    def lock_card(self, mem: int = MEM_EPC, word_ptr: int = 0,
                  word_count: int = 2, password: bytes = b"\x00" * 4,
                  lock_level: int = 0) -> bool:
        data = (ctypes.c_ubyte * 256)()
        pwd = (ctypes.c_ubyte * 4)(*password)
        if self._lib.CFHid_LockCardG2(
            DEV_ADDR, pwd, mem, word_ptr, word_count, data, lock_level
        ) != 1:
            raise ReaderError("LockCardG2 failed")
        return True


if __name__ == "__main__":  # quick smoke test
    r = Ru5600Reader()
    try:
        r.open()
    except ReaderError as exc:
        print("FAIL:", exc)
        raise SystemExit(1)
    print("opened:", r.system_info())
    print("rf power:", r.rf_power(), "work mode:", r.work_mode())
    print("freq:", r.read_freq().hex())
    tags = r.inventory()
    print("tags:", len(tags))
    for t in tags:
        print("  ", t.ant, t.epc_hex, t.rssi)
    r.close()
