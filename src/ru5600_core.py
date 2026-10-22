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


# Read modes: (dwell seconds between polls, max polls) for one detection.
#   burst    - poll once, as fast as possible: detects the tag in a
#              single burst, but misses when the reader does not answer
#   balanced - a few polls with a short dwell: good speed, still reliable
#   reliable - keeps polling until the reader answers (the "8/8" mode)
# Commands that must never be sent by mistake.
#
# FORBIDDEN: they damage the hardware or the tag.
#   0x2B/0x2C/0x2D  firmware update of the reader (data, data in, end):
#                    a wrong one leaves the reader unusable
#   0x65            Kill6C: kills the tag for good
#
# RESERVED: they change the streaming state of the reader.  The GUI has
# proper controls for them, and a reader left streaming keeps beeping and
# flooding the tag buffer, so the raw path refuses them.
FORBIDDEN_COMMANDS = {0x2B, 0x2C, 0x2D, 0x65}
RESERVED_COMMANDS = {0x40, 0x41}


def _env_num(name, default, cast=float):
    """A tunable from the environment, with the default as fallback."""
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# The reader refuses a read of more than 8 words (16 bytes) in one
# command - measured: 9..16 words always fail, 1..8 answer.
MAX_READ_WORDS = 8


READ_MODES = {
    #          answer mode rounds, gap between rounds
    "burst": (1, 0.0),
    "balanced": (6, 0.03),
    "reliable": (40, 0.03),
}


@dataclass
class Detection:
    """Result of one detection round set."""
    tags: List[Tag] = field(default_factory=list)
    rounds: int = 0
    hits: int = 0
    elapsed: float = 0.0

    @property
    def rate(self) -> float:
        """Detections per second."""
        return self.hits / self.elapsed if self.elapsed > 0 else 0.0


class ReaderError(RuntimeError):
    """Raised when the reader is unreachable or rejects a command."""


EPC_SLOT_BYTES = 12
"""Reference size of the 96 bit EPC slot."""


def checksum_ok(frame: bytes) -> bool:
    """True when the last byte of the frame is the checksum of the others.

    The reader's checksum is the two's complement of the byte sum, so a
    frame is valid when ``(-sum(frame[:-1])) & 0xFF == frame[-1]``.
    """
    if len(frame) < 2:
        return False
    return ((-sum(frame[:-1])) & 0xFF) == frame[-1]



# The response frame the reader leaves in its buffer (and that arrives on
# the wire over TCP):
#
#   [0] 43 54      magic
#   [2] len        u16 BE, counts everything up to the checksum
#   [4] 00
#   [5] type       the command that is answered
#   [6] status     01 = ok
#   [7] payload    (for the inventory the first two bytes are the tag count)
#   ..  checksum
#
# The payload therefore starts at offset 7 - there is NO extra count
# field, the count of an inventory answer is simply the first two bytes
# of its payload.
RESPONSE_PAYLOAD_OFFSET = 7


def parse_module_response(frame: bytes):
    """Parse a reader answer -> (type, status, payload) or None."""
    if len(frame) < 8 or frame[:2] != b"\x43\x54":
        return None
    length = (frame[2] << 8) | frame[3]
    if len(frame) < 4 + length:
        return None
    if not checksum_ok(frame[:4 + length]):
        return None
    rtype = frame[5]
    status = frame[6]
    payload = frame[RESPONSE_PAYLOAD_OFFSET:3 + length]
    return rtype, status, payload



# The vendor library keeps the last answer in its .bss.  The frame starts
# at this offset inside the loaded library; it is located by scanning for
# the magic and validating the checksum, so a layout change shows up as a
# clean error instead of garbage.
RESPONSE_BUFFER_OFFSET = 0x230F60
RESPONSE_BUFFER_WINDOW = 0x60


def _library_base(lib) -> int:
    """Load address of the vendor library (it is a PIE)."""
    path = None
    try:
        path = lib._name
    except AttributeError:
        pass
    for line in open("/proc/self/maps"):
        if "libCFHidApi.so" in line and "r" in line.split()[1]:
            return int(line.split("-")[0], 16)
    # fallback: the symbol address minus its offset in the file
    return 0

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
        "CFHid_LockCardG2": ([u8p, u8], ctypes.c_ubyte),
        "CFHid_SendCommand": ([u8, u8p, ctypes.c_int], ctypes.c_ubyte),
        "CFHid_GetSpecificPocket": ([u8, ctypes.c_ushort],
                                    ctypes.c_ubyte),
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

    # Timings, all tunable from the environment so nothing is hardcoded:
    #   RU5600_MIN_GAP      seconds between two consecutive commands
    #   RU5600_SETTLE       seconds to wait after a state changing command
    #   RU5600_READ_RETRIES attempts per memory read
    #   RU5600_READ_DELAY   seconds between those attempts
    MIN_GAP = _env_num("RU5600_MIN_GAP", 0.06)
    SETTLE = _env_num("RU5600_SETTLE", 0.45)
    READ_RETRIES = _env_num("RU5600_READ_RETRIES", 40, int)
    READ_DELAY = _env_num("RU5600_READ_DELAY", 0.03)

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
                    self._lib_base = _library_base(self._lib)
                    # let a previous client finish releasing the reader:
                    # claiming it too early upsets the vendor library
                    time.sleep(0.25)
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
        """Close the reader, leaving it quiet.

        A reader left in active mode keeps streaming (and beeping), so
        the stop is sent before closing even if the caller forgot it.
        """
        with self._lock:
            if not self._open:
                return False
            try:
                self._lib.CFHid_SetCallback(_CB(0))
            except Exception:
                pass
            self._cb = None
            for cmd, payload in ((0x40, b""), (0x00, b"")):
                try:
                    self._lib.CFHid_SendCommand(cmd, None, 0)
                    time.sleep(0.05)
                except Exception:
                    break
            try:
                self._lib.CFHid_ClearTagBuf()
            except Exception:
                pass
            try:
                self._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                                   WORKMODE_ANSWER)
            except Exception:
                pass
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
    def detect(self, mode: str = "balanced") -> Detection:
        """Look for tags with the selected read effort.

        The answer mode inventory is used here: with enough rounds it
        answers every time (measured 10/10 with 40 rounds), and with one
        round it is the fastest possible detection.

        * ``burst``    - a single round: fastest, misses most of the time
        * ``balanced`` - six rounds: good speed, still reliable
        * ``reliable`` - keeps going until the reader answers (the 8/8)
        """
        rounds, gap = READ_MODES.get(mode, READ_MODES["balanced"])
        det = Detection()
        buf = (ctypes.c_ubyte * 65536)()
        total = ctypes.c_ushort(0)
        count = ctypes.c_ushort(0)
        t0 = time.monotonic()
        for _ in range(max(1, rounds)):
            self._pace()
            det.rounds += 1
            if self._lib.CFHid_InventoryG2(
                DEV_ADDR, buf, ctypes.byref(total), ctypes.byref(count)
            ) == 1 and count.value:
                det.hits += 1
                for tag in parse_tag_buffer(bytes(buf[: total.value]),
                                           count.value):
                    if tag.epc_hex not in [t.epc_hex for t in det.tags]:
                        det.tags.append(tag)
                if det.tags:
                    break
            if gap:
                time.sleep(gap)
        det.elapsed = time.monotonic() - t0
        return det

    def measure(self, duration: float = 5.0, poll: float = 0.0) -> dict:
        """Measure the read speed and the antenna signal.

        The reader is switched to active mode and left streaming while
        the tag buffer is polled, so every detection the reader sends is
        counted.  The result is what to compare when mapping the reading
        area of an antenna: detections per second and the RSSI of the
        tags seen.
        """
        rssis = []
        seen = {}
        polls = hits = 0
        self._pace()
        self._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                          WORKMODE_ACTIVE)
        self._pace(settle=True)
        self._lib.CFHid_ClearTagBuf()
        self._pace()
        self._lib.CFHid_StartRead(DEV_ADDR)
        self._pace(settle=True)
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < duration:
                polls += 1
                tags = self.poll_tags()
                if tags:
                    hits += len(tags)
                    for tag in tags:
                        rssis.append(tag.rssi)
                        seen[tag.epc_hex] = seen.get(tag.epc_hex, 0) + 1
                if poll:
                    time.sleep(poll)
        finally:
            self._lib.CFHid_StopRead(DEV_ADDR)
            self._pace(settle=True)
            self._lib.CFHid_ClearTagBuf()
            self._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                              WORKMODE_ANSWER)
        elapsed = time.monotonic() - t0
        return {
            "elapsed": elapsed,
            "polls": polls,
            "detections": hits,
            "detections_per_s": hits / elapsed if elapsed else 0.0,
            "tags": seen,
            "rssi_count": len(rssis),
            "rssi_avg": sum(rssis) / len(rssis) if rssis else 0.0,
            "rssi_min": min(rssis) if rssis else 0,
            "rssi_max": max(rssis) if rssis else 0,
        }

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
                  retries: int = None, retry_delay: float = None,
                  pace: bool = True) -> bytes:
        """Read a Gen2 memory bank.

        ``word_count`` is in 16 bit words: 6 words = 12 bytes.

        The reader only answers **8 words (16 bytes) per command**, so a
        longer read is split into several commands of at most
        ``MAX_READ_WORDS`` and the pieces are joined.  That is what makes
        it possible to read a 192, 240 or 496 bit EPC without knowing in
        advance which chip is in the field.

        The reader answers a memory read only inside short windows (about
        every 3 s it accepts a burst of a few commands), so a single call
        succeeds only ~20 % of the time.  Retrying every 30 ms makes it
        reliable; a wrong ``password`` fails every single time.

        ``pace=False`` skips the minimum gap between commands, which is
        what a round robin scan over several areas wants.
        """
        if retries is None:
            retries = self.READ_RETRIES
        if retry_delay is None:
            retry_delay = self.READ_DELAY
        if word_count <= MAX_READ_WORDS:
            return self._read_chunk(mem, word_ptr, word_count, password,
                                    retries, retry_delay, pace)
        out = b""
        done = 0
        while done < word_count:
            chunk = min(MAX_READ_WORDS, word_count - done)
            out += self._read_chunk(mem, word_ptr + done, chunk, password,
                                    retries, retry_delay, pace)
            done += chunk
        return out

    def _read_chunk(self, mem: int, word_ptr: int, word_count: int,
                    password: bytes, retries: int, retry_delay: float,
                    pace: bool) -> bytes:
        """One read command (at most MAX_READ_WORDS words)."""
        data = (ctypes.c_ubyte * 256)()
        pwd = (ctypes.c_ubyte * 4)(*password)
        for attempt in range(max(1, retries)):
            if attempt:
                time.sleep(retry_delay)
            elif pace:
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

    def probe_bank(self, mem: int, password: bytes = b"\x00" * 4,
                   max_words: int = 64, chunk: int = MAX_READ_WORDS,
                   bank_budget: float = 5.0, chunk_budget: float = 1.2):
        """Find out how much of a memory bank the tag really has.

        The reader answers a read only intermittently (measured between
        25 % and 100 % depending on the moment), so "it refused once" does
        not mean "the memory ended".

        Instead of walking the bank chunk by chunk (slow, and it stops at
        the first refusal), the offsets are **sampled**: the bank is read
        at several offsets and the highest one that answers gives the
        size.  The result is a **lower bound** - the reader may have
        refused an offset that really exists - and that is what tells
        "the bank answers beyond the first chunk" (not locked) from "the
        bank does not answer at all" (locked or absent).

        Returns ``(bytes, words)``.
        """
        deadline = time.monotonic() + bank_budget
        out = b""
        words = 0
        step = max(chunk, 4)
        offset = 0
        while offset < max_words and time.monotonic() < deadline:
            size = min(chunk, max_words - offset)
            limit = time.monotonic() + min(chunk_budget,
                                          deadline - time.monotonic())
            part = None
            while time.monotonic() < limit:
                try:
                    part = self.read_card(mem, offset, size, password,
                                          retries=1, pace=True)
                    break
                except ReaderError:
                    time.sleep(0.04)
            if part is None:
                # the reader refused here: try further ahead before
                # concluding that the memory ends
                offset += step
                continue
            if out and part == out[-len(part):] and not any(part):
                # could be the reader repeating the previous chunk
                try:
                    self.read_card(MEM_TID, 0, 2, password, retries=1)
                except ReaderError:
                    pass
                try:
                    again = self.read_card(mem, offset, size, password,
                                           retries=3, pace=True)
                except ReaderError:
                    again = b""
                if again != part:
                    part = again
                else:
                    offset += step
                    continue
            if offset == 0:
                out = part
                words = size
            elif offset + size > words:
                if offset < words:
                    part = part[words - offset:]
                out += part
                words = offset + size
            offset += step
        return out[:words * 2], words

    def read_card_checked(self, mem: int, word_ptr: int, word_count: int,
                         password: bytes = b"\x00" * 4, expect: bytes = b"",
                         retries: int = 12, delay: float = 0.05) -> bytes:
        """Read a bank and check the answer against what must be there.

        The reader can answer a command with the response of a *previous*
        command of the same type, and the library hands that stale frame
        over as if it were fresh.  With several tags on the antenna that
        shows up as the EPC of another tag.

        ``expect`` is the byte string the answer has to contain (for the
        EPC bank: the EPC the inventory reported).  When it is given, a
        stale answer is rejected and the read is retried.
        """
        last = b""
        for attempt in range(max(1, retries)):
            try:
                data = self.read_card(mem, word_ptr, word_count, password)
            except ReaderError:
                time.sleep(delay)
                continue
            last = data
            if not expect or expect in data:
                return data
            time.sleep(delay)
        raise ReaderError(
            f"the reader answered with stale data ({last.hex().upper()}), "
            f"it does not contain the expected {expect.hex().upper()}"
        ) from None

    def bank_responds(self, mem: int, password: bytes = b"\x00" * 4,
                      budget: float = 2.0) -> bool:
        """Does the bank answer at all?  (locked vs present)"""
        limit = time.monotonic() + budget
        while time.monotonic() < limit:
            try:
                self.read_card(mem, 0, 2, password, retries=1)
                return True
            except ReaderError:
                time.sleep(0.04)
        return False

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

    def raw_command(self, cmd: int, payload: bytes = b"",
                    timeout_ms: int = 400, want_payload: bool = False):
        """Send an arbitrary command to the reader.

        This is the escape hatch the vendor exposes: `CFHid_SendCommand`
        builds the very same frame the typed API uses
        (`53 57 | len+3 | FF | cmd | payload | checksum`) and
        `CFHid_GetSpecificPocket` waits for the answer.

        It reaches the commands the Linux library does not wrap - the
        white list (0xE8-0xEC), the block lock (0x05), the batch write -
        but it also skips the argument checking the typed functions do, so
        a wrong payload can leave the reader in a bad state.
        """
        with self._lock:
            if not self._open:
                raise ReaderError("the reader is not open")
            if (cmd & 0xFF) in FORBIDDEN_COMMANDS:
                raise ReaderError(
                    f"command 0x{cmd & 0xFF:02X} is blocked: it updates "
                    f"the firmware of the reader (or kills a tag)")
            if (cmd & 0xFF) in RESERVED_COMMANDS:
                raise ReaderError(
                    f"command 0x{cmd & 0xFF:02X} is reserved: it starts or "
                    f"stops the continuous reading of the reader. Use the "
                    f"ActiveMode tab.")
            self._pace()
            buf = (ctypes.c_ubyte * max(1, len(payload)))(*payload) \
                if payload else None
            if not self._lib.CFHid_SendCommand(cmd & 0xFF, buf,
                                               len(payload)):
                return (False, b"") if want_payload else False
            self._pace()
            answer = self._lib.CFHid_GetSpecificPocket(cmd & 0xFF,
                                                       timeout_ms)
            if not want_payload:
                return answer == 1
            frame = self._read_response_frame()
            if frame is None:
                return (False, b"")
            parsed = parse_module_response(frame)
            if parsed is None:
                return (False, b"")
            rtype, status, body = parsed
            return (rtype == (cmd & 0xFF) and status == 0x01), body


    def _read_response_frame(self):
        """The last answer the vendor library received, from its .bss."""
        base = getattr(self, "_lib_base", 0)
        if not base:
            return None
        try:
            window = ctypes.string_at(
                base + RESPONSE_BUFFER_OFFSET, RESPONSE_BUFFER_WINDOW)
        except (OSError, ValueError):
            return None
        for i in range(len(window) - 8):
            if window[i:i + 2] != b"\x43\x54":
                continue
            length = (window[i + 2] << 8) | window[i + 3]
            if 4 + length > len(window):
                continue
            frame = window[i:i + 4 + length]
            if parse_module_response(frame) is not None:
                return frame
        return None

    # -- white list --------------------------------------------------------
    def whitelist_count(self) -> int:
        """How many entries the reader's white list holds."""
        ok, body = self.raw_command(0xE8, b"", want_payload=True)
        if not ok or len(body) < 2:
            raise ReaderError("the reader did not answer the white list count")
        return (body[0] << 8) | body[1]

    def whitelist_read(self, index: int) -> bytes:
        """One white list entry (32 bytes)."""
        ok, body = self.raw_command(0xE9, bytes([index >> 8, index & 0xFF]),
                                    want_payload=True)
        if not ok:
            raise ReaderError(f"the reader did not answer entry {index}")
        return body[:32]

    def whitelist_write(self, entry: bytes) -> bool:
        """Add an entry (the payload is [len][data])."""
        ok, _ = self.raw_command(0xEA, bytes([len(entry) & 0xFF]) + entry,
                                 want_payload=True)
        if not ok:
            raise ReaderError("the reader refused the white list entry")
        return True

    def whitelist_delete(self, entry: bytes) -> bool:
        """Delete one entry."""
        ok, _ = self.raw_command(0xEC, bytes([len(entry) & 0xFF]) + entry,
                                 want_payload=True)
        if not ok:
            raise ReaderError("the reader refused to delete the entry")
        return True

    def whitelist_clear(self) -> bool:
        """Delete every entry."""
        ok, _ = self.raw_command(0xEB, b"", want_payload=True)
        if not ok:
            raise ReaderError("the reader refused to clear the white list")
        return True

    def lock_card(self, lock_data: bytes, action: int,
                  confirm: bool = False) -> bool:
        """Gen2 block lock (the real memory protection of a tag).

        Signature taken from the library itself, not guessed:
        ``CFHid_LockCardG2(const void *lock4, unsigned char action)`` builds
        the payload ``[action][4 bytes of lock_data]`` and sends command
        0x05 - the same as the Windows ``SWHid_LockCardG2``, whose payload
        is ``[action][*lock_ptr]``.

        **The vendor does not document what the 4 bytes are.**  The
        layout of the generic Gen2 block lock is a 2 byte lock parameter
        (the password) plus the lock action code, so the 4 bytes most
        likely carry the target and/or the password; treat them as
        "whatever the Windows program sends".

        A block lock is **not** reversible: it is the way to make a tag
        read only for a reader that knows the password.  The library
        exposes no way to read a lock back, so check the arguments
        before calling it - hence the mandatory ``confirm=True``.
        """
        if len(lock_data) != 4:
            raise ReaderError("lock data must be exactly 4 bytes")
        if not confirm:
            raise ReaderError(
                "LockCardG2 needs confirm=True: a block lock is "
                "permanent and cannot be read back.")
        return self.raw_command(0x05, bytes([action & 0xFF]) + lock_data,
                                timeout_ms=350)


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


# --- fingerprint (huella) -------------------------------------------------
# The EPC can be written to any tag, so a clone is trivial.  The TID is
# read only in the chip and unique, so the pair (EPC, TID) identifies the
# physical tag.  The store below remembers that pair and the client can
# refuse to write to a tag whose TID does not match the expected one.

FINGERPRINT_FILE = os.path.join(
    os.path.expanduser("~"), ".config", "ru5600-reader", "fingerprints.json")


def fingerprint_path() -> str:
    return os.environ.get("RU5600_FINGERPRINTS", FINGERPRINT_FILE)


def load_fingerprints() -> dict:
    """The known (EPC -> TID) pairs."""
    import json
    try:
        with open(fingerprint_path()) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_fingerprints(data: dict) -> None:
    import json
    path = fingerprint_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)


def learn_fingerprint(reader, epc: bytes) -> str:
    """Read the TID of the tag in front of the reader and remember it."""
    tid, _ = reader.probe_bank(MEM_TID)
    if not tid:
        raise ReaderError("the TID could not be read")
    data = load_fingerprints()
    data[epc.hex().upper()] = tid.hex().upper()
    save_fingerprints(data)
    return tid.hex().upper()


def check_fingerprint(reader, epc: bytes):
    """Verify the tag against the stored fingerprint.

    Returns ``(ok, message)``.  A tag that is not in the store passes
    (nothing is known about it yet), a tag whose TID does not match fails.
    """
    data = load_fingerprints()
    expected = data.get(epc.hex().upper())
    if not expected:
        return True, "sin huella registrada"
    tid, _ = reader.probe_bank(MEM_TID)
    if not tid:
        return False, "no se pudo leer el TID para verificar la huella"
    if tid.hex().upper() != expected.upper():
        return False, (f"huella no coincide: TID {tid.hex().upper()} "
                        f"!= {expected.upper()}")
    return True, "huella verificada"
