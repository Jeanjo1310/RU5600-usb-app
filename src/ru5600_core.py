#!/usr/bin/env python3
"""RU5600 / INVETON UHF reader - core transport binding.

Talks to the vendor's own cross-platform USB library (libCFHidApi.so), which
is the Linux build of the same API that ships as SWHidApi.dll in the Windows
ReaderSoftV5.1 SDK.  The library speaks the reader's proprietary module
protocol over libusb, so we do not reimplement the USB layer.

See PROTOCOL.md for the wire format and the reverse-engineering evidence.
"""
from __future__ import annotations

import atexit
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

# USB identity of the RU5600 reader (QinHeng HID).  Used to track the
# device node across re-enumerations: when the reader resets itself
# under stress it comes back on another device number (008 -> 011),
# and the fix is to look where it is now instead of where it was.
USB_VID = "1a86"
USB_PID = "e010"


def find_reader_usb_node():
    """Sysfs node (e.g. '001/011') where the reader is right now, or None.

    Solution, not a problem report (Creador 2026-10-26): on disconnect,
    re-verify the position instead of assuming the old one.
    """
    base = "/sys/bus/usb/devices"
    try:
        nodes = sorted(os.listdir(base))
    except OSError:
        return None
    for node in nodes:
        try:
            with open(os.path.join(base, node, "idVendor")) as fh:
                vid = fh.read().strip().lower()
            with open(os.path.join(base, node, "idProduct")) as fh:
                pid = fh.read().strip().lower()
        except OSError:
            continue
        if vid == USB_VID and pid == USB_PID:
            try:
                with open(os.path.join(base, node, "busnum")) as fh:
                    bus = fh.read().strip()
                with open(os.path.join(base, node, "devnum")) as fh:
                    dev = fh.read().strip()
                return f"{int(bus):03d}/{int(dev):03d}"
            except OSError:
                return node
    return None


# Device address used by every vendor demo: 0xFF == broadcast.
DEV_ADDR = 0xFF

# Device parameter addresses (documented in the SDK demos).
PARAM_TRANSPORT = 0x01
RESP_READ = 0x02          # tipo de frame de respuesta de una lectura
# Status codes of the reader's answer:
#   0x01 - the tag answered, the data is good
#   0x83 - the tag does not have that memory (a DEFINITIVE answer: a
#          read that does not fit the bank always gets this one)
#   0x00 - the reader did not answer properly (flaky): nothing is known
RESP_OK = 0x01
RESP_NO_DATA = 0x83
# The vendor library and the preloaded libusb both tear the USB context
# down when the process ends, and the second one aborts with a libusb
# mutex error ("pthread_mutex_lock(mutex) == 0 no se cumple").  When the
# reader was closed properly the process is ended right away, before that
# teardown runs.
_clean_close = False


def _exit_without_broken_teardown():
    if _clean_close:
        os._exit(0)


atexit.register(_exit_without_broken_teardown)

PARAM_WORKMODE = 0x02
PARAM_DEVADDR = 0x03
PARAM_FILTERTIME = 0x04
PARAM_RFPOWER = 0x05
PARAM_BEEP = 0x06
PARAM_UARTBAUD = 0x07

# Work modes of the reader, with the vendor's names (the same ones the
# Windows software uses for its AnswerMode / ActiveMode tabs):
#
#   ANSWER  - the reader answers one command at a time
#   ACTIVE  - the reader scans continuously and streams the tags it sees
#
# "Active" here means **continuous scanning of the reader**, NOT an
# active tag: every Gen2 tag handled by this tool is passive (no
# battery).  The memory reads answer much better in answer mode (86 %
# measured) than while the reader streams (43 %), so the scans put the
# reader back into answer mode when they are done.
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


# The CRC that the reader puts in front of the EPC: CRC-16/CCITT
# (polynomial 0x1021, most significant bit first) with an initial value
# of 0xEE14.  It was found by brute force over the polynomials and the
# initial values using two tags of the same model: the same value
# reproduces the CRC of both of them.
EPC_CRC_INIT = 0xEE14
EPC_CRC_POLY = 0x1021


def epc_crc16(epc: bytes) -> int:
    """The CRC that the reader puts in front of the EPC."""
    crc = EPC_CRC_INIT
    for byte in epc:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ EPC_CRC_POLY) & 0xFFFF \
                if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def epc_crc_ok(epc: bytes, crc: bytes) -> bool:
    """Does the CRC that came with the EPC match the EPC?

    This is what tells a genuine answer from one that was not fresh:
    the reader computes the CRC of the EPC it read, so a frame that was
    not fresh (or that was not an EPC at all) does not add up.
    """
    if len(crc) != 2 or len(epc) < 2:
        return False
    return epc_crc16(epc) == ((crc[0] << 8) | crc[1])


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

    Answer-mode inventory holds the **whole EPC** in the slot (12 bytes
    for a 96 bit tag, 24 for a 192 bit one) - there is no padding.
    ``raw_id`` keeps the slot as received.

    Active-mode streaming packs a **compound** record instead: the EPC
    slot (PC-padded with zeros) followed by the TID.  The parser keeps
    the blob intact (vendor-faithful, like the `Form1.cs` demo) and the
    split happens chip-first in `interpret_active_blob()`, only for
    chips with an evidence-backed interpreter.  ``tid`` is populated
    there, not here.
    (REA P2 2026-10-25: carvear a ciegas rompía el dedup; leer mal es
    grave, escribir mal es crítico.)
    """

    type: int = 0
    ant: int = 0
    epc: bytes = b""
    tid: bytes = b""
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
        "CFHid_StopRead": ([], ctypes.c_ubyte),
        "CFHid_StartRead": ([], ctypes.c_ubyte),
        "CFHid_InventoryG2": ([u8, u8p, u16p, u16p], ctypes.c_ubyte),
        "CFHid_WriteEPCG2": ([u8, u8p, u8p, u8], ctypes.c_ubyte),
        "CFHid_ReadCardG2": ([u8, u8p, u8, u8, u8, u8p], ctypes.c_ubyte),
        "CFHid_WriteCardG2": ([u8, u8p, u8, u8, u8, u8p], ctypes.c_ubyte),
        "CFHid_RelayOn": ([u8], ctypes.c_ubyte),
        "CFHid_RelayOff": ([u8], ctypes.c_ubyte),
        "CFHid_SetFreq": ([u8, u8p], ctypes.c_ubyte),
        "CFHid_ReadFreq": ([u8, u8p], ctypes.c_ubyte),
        "CFHid_SetCallback": ([_CB], ctypes.c_int),
        # el .so escribe 4 bytes en cada out-param (objdump 0x6304/0x631c)
        "CFHid_GetTagBuf": ([u8p, ctypes.POINTER(ctypes.c_uint32),
                             ctypes.POINTER(ctypes.c_uint32)], ctypes.c_ubyte),
        "CFHid_CheckAnt": ([], ctypes.c_ubyte),
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
        # sondas de estado del vendor (firmas inferidas del .so 2026-10-26):
        # ambas envian un comando y esperan la respuesta 100 ms; 1 = contesto,
        # 0 = no contesto (leer el desensamblado: SendCommand + GetSpecificPocket).
        "CFHid_ReadTransState": ([u8, u8p], ctypes.c_ubyte),   # cmd 0x2D
        "CFHid_CheckModule": ([u8, u8p], ctypes.c_ubyte),      # cmd 0xE0
    }
    for name, (argtypes, restype) in sig.items():
        fn = getattr(lib, name, None)
        if fn is None:
            continue
        fn.argtypes = argtypes
        fn.restype = restype


def parse_tag_buffer(buf: bytes, count: int) -> List[Tag]:
    """Decodifica el buffer de tags del lector.

    Layout medido en el firmware 7.1 (2026-10-26, registro de 92 bytes):
        [0]      largo del registro - 1
        [1]      tipo
        [2]      antena
        [3..]    EPC (largo variable: 8, 12 ...)
        luego    TID 12 bytes
        cola     fija de 65 bytes (USER / timestamp / RSSI)
    El EPC se despeja del largo del registro: epc_len = n - 80.
    """
    tags: List[Tag] = []
    off = 0
    total = len(buf)
    for _ in range(max(0, count)):
        if off >= total:
            break
        n = buf[off] + 1
        if n < 6 or off + n > total:
            break
        rec = buf[off:off + n]
        ant = rec[2] if len(rec) > 2 else 0
        epc_len = n - 80
        if epc_len < 2 or epc_len > 64:
            off += n
            continue
        epc = rec[3:3 + epc_len]
        tid = rec[3 + epc_len:3 + epc_len + 12]
        rssi = rec[n - 44] if n >= 44 else 0
        try:
            tags.append(Tag(epc=epc, raw_id=bytes(rec), ant=ant, rssi=rssi,
                            tid=tid or b""))
        except TypeError:
            tag = Tag.__new__(Tag)
            tag.epc, tag.raw_id, tag.ant, tag.rssi, tag.tid = epc, bytes(rec), ant, rssi, tid or b""
            tags.append(tag)
        off += n
    return tags


def _parse_tag_buffer_legacy(buf: bytes, count: int) -> List[Tag]:
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
    dropped = 0
    for _ in range(count):
        # Resync ante record corrupto (un break descartaba todo el resto
        # del buffer, incluidos tags sanos de otros modelos).
        while off < len(buf) and (buf[off] < 3
                                  or off + 1 + buf[off] > len(buf)):
            off += 1
            dropped += 1
            if off >= len(buf) or dropped > 1024:
                break
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
        # NOTE (REA P2 2026-10-25, anti-sobre-ingeniería): el parser queda
        # fiel al vendor (blob íntegro, como el demo `Form1.cs`).
        # La separación EPC/TID NO se hace acá: se hace chip-primero en
        # `interpret_active_blob()` después de identificar por TID directo.
        tid = b""
        ts = bytes(rec[id_len:id_len + 6]) if has_ts and id_len + 6 <= pack_len else None
        tags.append(
            Tag(
                type=rec[0],
                ant=rec[1],
                epc=epc,
                tid=tid,
                rssi=rec[id_len] if id_len < pack_len else 0,
                timestamp=ts,
                raw=bytes(rec),
                raw_id=bytes(rec[2:id_len]),
            )
        )
        off += pack_len + 1
    return tags


# --- Chip-first compound interpreters (REA P2 2026-10-25) ------------------
# The active-mode blob is interpreted ONLY after the chip was identified
# by direct TID read (bank 2) + decode.  One entry per chip with hardware
# evidence.  Unknown chips stay vendor-faithful (whole blob + pending flag):
# reading wrong is grave, writing wrong is critical.
ACTIVE_COMPOUND_PREFIXES = {
    # prefix: evidence (chip-first; TID from direct bank-2 read + decode)
    "E2803821": "Higgs-9 live 2026-10-25/26: layouts A (100B) y B (88B)",
}


def interpret_active_blob(blob: bytes, tid_prefix: str):
    """Split a compound active blob for a KNOWN chip.

    Returns ``(epc, tid, pending)``: pending is None when the chip has
    an evidence-backed interpreter, else a string flagging the blob as
    uninterpreted (whole blob returned as epc, tid empty).

    Two reader packings observed live (same reader, different EPC slots):
      A: [EPC 12][pad 12][TID 12...]  (PC 12 words, e.g. H9 041 PC 6000)
      B: [EPC 12][TID 12...]          (no pad; TID class ID 0xE2 follows)
    The split key is the TID class byte 0xE2 (Gen2 invariant already used
    by read_tid_for), never a hardcoded chip model.
    """
    spec = ACTIVE_COMPOUND_PREFIXES.get(tid_prefix)
    if spec is None:
        return bytes(blob), b"", f"interpreter pending for {tid_prefix}"
    if len(blob) < 24 or blob[0] != 0xE2:
        return bytes(blob), b"", f"layout mismatch for {tid_prefix}"
    epc = bytes(blob[0:12])
    if len(blob) >= 24 and blob[12] == 0xE2:
        tid = bytes(blob[12:24])          # layout B: TID right after EPC
    elif (len(blob) >= 36 and blob[24] == 0xE2
            and all(b == 0 for b in blob[12:24])):
        tid = bytes(blob[24:36])         # layout A: 12B pad, then TID
    else:
        return bytes(blob), b"", f"layout mismatch for {tid_prefix}"
    if tid[0] != 0xE2:
        return bytes(blob), b"", f"layout mismatch for {tid_prefix}"
    return epc, tid, None


class Ru5600Reader:
    """Thin, thread-safe wrapper over the vendor USB API.

    The reader drops commands that arrive while it is still busy with the
    previous one, so every call is paced: a small gap between any two
    commands and a longer settle after a command that changes reader state
    (set parameter, set frequency, start/stop reading, write).
    """

    # Timings, all tunable from the environment so nothing is hardcoded:
    #   RU5600_MIN_GAP      seconds between two consecutive commands
    #                        (measured: 100 ms, with 0 ms the reader
    #                        answers with a stale frame or not at all)
    #   RU5600_SETTLE       seconds to wait after a state changing command
    #   RU5600_READ_RETRIES attempts per memory read
    #   RU5600_READ_DELAY   seconds between those attempts
    MIN_GAP = _env_num("RU5600_MIN_GAP", 0.04)
    SETTLE = _env_num("RU5600_SETTLE", 0.45)
    # The pace is the one the vendor's own software uses: it sleeps
    # 40 ms between two attempts (Sleep(0x28) in ReaderSoft.exe) and
    # 10 ms before starting.  Measured on the bench: 0 ms gives 2/12
    # good reads, 10 ms 11/12, 40 ms 9/12, 150 ms 11/12 - so 40 ms is
    # as good as waiting and 2.5 times faster.
    READ_RETRIES = _env_num("RU5600_READ_RETRIES", 12, int)
    READ_DELAY = _env_num("RU5600_READ_DELAY", 0.04)

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
    def open(self, attempts: int = 15, retry_delay: float = 2.5) -> bool:
        """Open the reader.

        The USB interface is claimed exclusively, so a previous client that
        is still shutting down can make the first attempt fail - the
        reader needs a few seconds to let go of it.  Retry before giving
        up.

        Re-enumeration proof (Creador 2026-10-26): when the reader resets
        itself it comes back on another device number, so the position is
        re-verified on every retry instead of assumed.  The vendor open
        itself is VID:PID based, so a moved reader opens fine.
        """
        with self._lock:
            if self._open:
                return True
            first_node = find_reader_usb_node()
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
                node = find_reader_usb_node()
                if node != first_node:
                    first_node = node  # moved (e.g. 001/008 -> 001/011)
                if attempt + 1 < attempts:
                    time.sleep(retry_delay)
            node = find_reader_usb_node()
            raise ReaderError(
                "Open failed: the reader could not be claimed.\n"
                f"USB node now: {node or 'not found - reenchufa el lector'}.\n"
                "Either it is not connected, the udev rule is missing "
                "(press 'Install USB access rule'), or another program "
                "has it open - only one client can use the reader at a "
                "time.  After another client disconnects, this reader "
                "needs about 20-30 s to release the USB port, so the "
                "client waits and retries."
            ) from None

    def reconnect(self, settle: float = 5.0) -> bool:
        """Best-effort close + open, re-locating a moved reader.

        To call after a `Hid Disconnect` or a mute streak: drops our side
        quietly, waits for the reader to come back (possibly on another
        device number), and opens it again.
        """
        try:
            with self._lock:
                self._open = False
                try:
                    self._lib.CFHid_CloseDevice()
                except Exception:  # noqa: BLE001 - already half gone
                    pass
        finally:
            time.sleep(settle)
        return self.open()

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
            for cmd, payload in ((0x40, b""),):
                try:
                    self._lib.CFHid_SendCommand(cmd, None, 0)
                    time.sleep(0.05)
                except Exception:
                    break
            # give the vendor's reader thread time to finish before the
            # library tears the libusb context down: closing while the
            # thread is still active aborts with a libusb mutex error
            time.sleep(0.3)
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
            global _clean_close
            _clean_close = True
            return True

    @property
    def is_open(self) -> bool:
        return self._open

    def _install_callback(self) -> None:
        def _cb(msg, param1, param2, param3, param4):
            try:
                st = getattr(self, "_cb_stats", None)
                if st is not None:
                    st["calls"] += 1
                    st["msg"][msg] = st["msg"].get(msg, 0) + 1
                    if msg == 2:
                        st["data_frames"] += 1
                        st["last_len"] = param3
                        st["last_count"] = param1
                if msg == 2 and param1 > 0 and param2:
                    raw = ctypes.string_at(param2, param3)
                    tags = parse_tag_buffer(raw, param1)
                    if tags:
                        # el callback es la via real por la que llegan los tags:
                        # guardarlos para que poll_tags los entregue
                        self._cb_tags.extend(tags)
                        if self._on_tags:
                            self._on_tags(tags)
            except Exception:
                pass

        if not hasattr(self, "_cb_tags"):
            self._cb_tags = []
        if not hasattr(self, "_cb_stats"):
            self._cb_stats = {"calls": 0, "msg": {}, "data_frames": 0,
                              "last_len": 0, "last_count": 0}
        self._cb = _CB(_cb)
        # NO registrar el callback: lo invoca un hilo C (y el monitor de hotplug
        # incluso despues de cerrar), y llamar a Python desde ahi puede tumbar el
        # proceso sin traceback (segfault). El buffer de tags se llena igual sin
        # callback: se drena con GetTagBuf.
        try:
            self._lib.CFHid_SetCallback(ctypes.cast(None, _CB))
        except Exception:  # noqa: BLE001
            pass

    # -- salud del enlace (senales del propio vendor) ------------------------
    # Offset del puntero al dispositivo hid dentro del .so (medido en
    # libCFHidApi.so x86-64: CFHid_SendCommand hace "cmpq $0x0, <handle>" antes
    # de cada comando, y el hilo de recepcion lo pone en NULL cuando hid_read
    # falla). Es el "LED apagado" del lector, leido del byte que el vendor usa.
    HID_HANDLE_OFF = 0x20AFF8

    def hid_handle_ptr(self):
        """Puntero interno al dispositivo hid, o None si el vendor ya detecto
        la desconexion (handle NULL) o si no se puede determinar."""
        base = getattr(self, "_lib_base", 0)
        if not base:
            return None
        addr = base + self.HID_HANDLE_OFF
        # validar que la direccion cae en una zona rw del .so (el offset es
        # del binario que tenemos; con otro build, no adivinar)
        try:
            for line in open("/proc/self/maps"):
                rng, perms = line.split()[0], line.split()[1]
                if "libCFHidApi.so" not in line or "w" not in perms:
                    continue
                lo, hi = (int(x, 16) for x in rng.split("-"))
                if lo <= addr < hi:
                    return ctypes.cast(
                        addr, ctypes.POINTER(ctypes.c_void_p)).contents.value
            return None
        except Exception:  # noqa: BLE001
            return None

    def trans_state(self):
        """Estado de transmision del lector (CFHid_ReadTransState, cmd 0x2D).

        Devuelve el BYTE de estado del firmware, o None si el equipo no
        contesto el comando. Ojo (bug medido 2026-10-26): la funcion C
        devuelve 1/0 como FLAG DE EXITO y escribe el estado en el out-param —
        un 0 es estado legal del firmware, NO significa "sin lector". Por eso
        None (no contesto) y un entero (contesto) no se pueden confundir."""
        with self._lock:
            self._pace()
            out = ctypes.c_ubyte(0)
            if self._lib.CFHid_ReadTransState(DEV_ADDR,
                                              ctypes.byref(out)) != 1:
                return None
            return out.value

    def check_module(self):
        """Chequeo del modulo RFID (CFHid_CheckModule, cmd 0xE0). None =
        no contesto; entero = contesto (el byte es el resultado del chequeo)."""
        with self._lock:
            self._pace()
            out = ctypes.c_ubyte(0)
            if self._lib.CFHid_CheckModule(DEV_ADDR, ctypes.byref(out)) != 1:
                return None
            return out.value

    # -- device information ------------------------------------------------
    def system_info(self) -> SystemInfo:
        with self._lock:
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
        with self._lock:
            self._pace()
            val = ctypes.c_ubyte(0)
            if self._lib.CFHid_ReadDeviceOneParam(DEV_ADDR, addr, ctypes.byref(val)) == 1:
                return val.value
            # el lector no contesta un parametro justo tras cambiar de modo o
            # arrancar el stream (medido 2026-10-26): un reintento tras el
            # settle lo salva sin abrir la carrera a otros comandos
            self._pace(settle=True)
            if self._lib.CFHid_ReadDeviceOneParam(DEV_ADDR, addr, ctypes.byref(val)) == 1:
                return val.value
            raise ReaderError(f"ReadDeviceOneParam(0x{addr:02X}) failed")

    def set_param(self, addr: int, value: int) -> bool:
        with self._lock:
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
        with self._lock:
            self._pace()
            buf = (ctypes.c_ubyte * 2)()
            if self._lib.CFHid_ReadFreq(DEV_ADDR, buf) == 1:
                return bytes(buf)
            self._pace(settle=True)   # mismo caso: reintento tras modo/stream
            if self._lib.CFHid_ReadFreq(DEV_ADDR, buf) == 1:
                return bytes(buf)
            raise ReaderError("ReadFreq failed")

    def set_freq(self, band: str) -> bool:
        with self._lock:
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

        NOTE 2026-10-26: the answer-mode InventoryG2 underneath is refused
        by the reader on Linux (ret=0 deterministic, not windows) - use
        scan() for discovery; detect() stays for API compatibility.

        The answer mode inventory was used here: with enough rounds it
        answered every time (measured 10/10 with 40 rounds), and with one
        round the fastest possible detection.

        * ``burst``    - a single round: fastest, misses most of the time
        * ``balanced`` - six rounds: good speed, still reliable
        * ``reliable`` - keeps going until the reader answers (the 8/8)
        """
        with self._lock:
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

    def measure(self, duration: float = 5.0, poll: float = 0.1,
                stop=None) -> dict:
        """Measure the read speed and the antenna signal.

        The reader is switched to active mode and left streaming while
        the tag buffer is polled, so every detection the reader sends is
        counted.  Poll default 0.1 s = vendor timer1 100 ms (a busy loop
        hammers the USB for millions of empty polls).  The result is what
        to compare when mapping the reading area of an antenna:
        detections per second and the RSSI of the tags seen.
        """
        rssis = []
        seen = {}
        polls = hits = 0
        self._pace()
        with self._lock:
            self._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                              WORKMODE_ACTIVE)
        self._pace(settle=True)
        with self._lock:
            self._lib.CFHid_ClearTagBuf()
        self._pace()
        # verificar que el modo quedo grabado: si no, el firmware no streamea
        for _try in range(3):
            mode_now = None
            try:
                mode_now = self.read_param(PARAM_WORKMODE)
            except Exception:  # noqa: BLE001
                pass
            if mode_now == WORKMODE_ACTIVE:
                break
            self._pace(settle=True)
            with self._lock:
                self._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                                  WORKMODE_ACTIVE)
            self._pace(settle=True)
        with self._lock:
            started = self._lib.CFHid_StartRead()
        self._pace(settle=True)
        self.last_start_ok = bool(started)
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < duration:
                if stop is not None and stop.is_set():
                    break
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
            with self._lock:
                self._lib.CFHid_StopRead()
            self._pace(settle=True)
            with self._lock:
                self._lib.CFHid_ClearTagBuf()
            with self._lock:
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
        with self._lock:
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

    def scan(self, dwell: float = 0.35, attempts: int = 4,
             restore_mode: bool = True) -> List[Tag]:
        """Reliable one shot scan.

        Vendor-faithful (ReaderSoft timer1_Tick, 100 ms): StartRead once,
        then drain the tag buffer REPEATEDLY while the reader streams
        instead of a single poll after StopRead.  The old single-shot
        drain missed the buffer; the exe never does that.

        The answer mode inventory (InventoryG2) is refused by the reader
        on Linux (ret=0 deterministic - fresh, rebooted, answer mode set,
        2048/65536 buffers, callback on/off - 2026-10-26): declared
        disabled/unmanaged here, use this active scan instead.

        A scan must not degrade the reads that come after it: the reader
        answers memory reads much better in answer mode (measured 86 %)
        than in active mode (43 %), so the mode is put back.
        """
        previous = None
        if restore_mode:
            try:
                previous = self.work_mode()
            except Exception:
                previous = None
        for attempt in range(max(1, attempts)):
            self._pace()
            with self._lock:
                self._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                                  WORKMODE_ACTIVE)
            self._pace(settle=True)
            with self._lock:
                self._lib.CFHid_ClearTagBuf()
            self._pace()
            with self._lock:
                self._lib.CFHid_StartRead()
            self._pace(settle=True)
            tags: List[Tag] = []
            seen = set()
            deadline = time.monotonic() + dwell
            try:
                while time.monotonic() < deadline:
                    time.sleep(0.1)  # timer1.Interval = 100 ms
                    for tag in self.poll_tags():
                        key = (tag.epc_hex, tag.ant)
                        if key not in seen:
                            seen.add(key)
                            tags.append(tag)
            finally:
                with self._lock:
                    self._lib.CFHid_StopRead()
                self._pace(settle=True)
            if tags:
                break
        if restore_mode:
            try:
                self.set_work_mode(previous if previous is not None
                                   else WORKMODE_ANSWER)
            except Exception:
                pass
        return tags

    def sweep(self, duration: float = 60.0, target_count: int = 0,
              poll: float = 0.1, on_new=None) -> List[Tag]:
        """Persistent multi-tag sweep (barrido).

        Vendor pattern (timer1 100 ms): StartRead once, drain the buffer
        continuously while streaming.  Like the antenna it only stops on
        stop order (Ctrl+C -> StopRead) or time budget (duration <= 0 =
        until Ctrl+C).  target_count is just progress meta (100/500 tags
        in one read), never a stop condition: counted tags are never
        re-reported as new (dedup by raw blob).  Elevated tags and big
        batches need time, not power: keep polling, don't hammer with
        extra orders.
        """
        previous = None
        try:
            previous = self.work_mode()
        except Exception:
            previous = None
        tags: List[Tag] = []
        seen = set()
        self._pace()
        with self._lock:
            self._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                              WORKMODE_ACTIVE)
        self._pace(settle=True)
        with self._lock:
            self._lib.CFHid_ClearTagBuf()
        self._pace()
        with self._lock:
            self._lib.CFHid_StartRead()
        self._pace(settle=True)
        deadline = None if duration <= 0 else time.monotonic() + duration
        try:
            while deadline is None or time.monotonic() < deadline:
                time.sleep(poll)
                for tag in self.poll_tags():
                    key = bytes(tag.raw_id).hex().upper() if tag.raw_id else tag.epc_hex
                    if key not in seen:
                        seen.add(key)
                        tags.append(tag)
                        if on_new is not None:
                            on_new(tag, len(tags))
        finally:
            try:
                with self._lock:
                    self._lib.CFHid_StopRead()
            except Exception:
                pass
            self._pace(settle=True)
        try:
            self.set_work_mode(previous if previous is not None
                               else WORKMODE_ANSWER)
        except Exception:
            pass
        return tags

    def callback_stats(self) -> dict:
        st = getattr(self, "_cb_stats", None) or {}
        return {"calls": st.get("calls", 0),
                "data_frames": st.get("data_frames", 0),
                "by_msg": {str(k): v for k, v in (st.get("msg") or {}).items()},
                "last_len": st.get("last_len", 0),
                "last_count": st.get("last_count", 0),
                "queued": len(getattr(self, "_cb_tags", []) or [])}

    def poll_tags(self) -> List[Tag]:
        """Drain the tag buffer filled by active mode (callback primero)."""
        with self._lock:
            tags = []
            queued = getattr(self, "_cb_tags", None)
            if queued:
                tags.extend(queued)
                del queued[:]
            # drenar SIEMPRE el buffer del SDK tambien: si nadie lo lee, el hilo de
            # recepcion deja de acumular (tope 0x1f7ff)
            buf = (ctypes.c_ubyte * 131072)()
            total = ctypes.c_uint32(0)
            count = ctypes.c_uint32(0)
            ret = self._lib.CFHid_GetTagBuf(buf, ctypes.byref(total), ctypes.byref(count))
            if ret and count.value:
                tags.extend(parse_tag_buffer(bytes(buf[: total.value]), count.value))
            return tags

    def start_read(self) -> bool:
        with self._lock:
            self._pace()
            if self._lib.CFHid_StartRead() != 1:
                raise ReaderError("StartRead failed")
            self._pace(settle=True)
            return True

    def stop_read(self) -> bool:
        with self._lock:
            self._pace()
            if self._lib.CFHid_StopRead() != 1:
                raise ReaderError("StopRead failed")
            self._pace(settle=True)
            self._lib.CFHid_ClearTagBuf()
            self._pace()
            return True

    def clear_tag_buf(self) -> bool:
        with self._lock:
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
        data, status = self.read_card_status(mem, word_ptr, word_count,
                                             password, retries, retry_delay,
                                             pace)
        if status != RESP_OK:
            raise ReaderError(
                f"ReadCardG2 did not return data (status 0x{status:02X})."
            ) from None
        return data

    def read_card_status(self, mem: int = MEM_EPC, word_ptr: int = 0,
                         word_count: int = 6,
                         password: bytes = b"\x00" * 4,
                         retries: int = None, retry_delay: float = None,
                         pace: bool = True):
        """Read a bank and report the status of the answer.

        Returns ``(data, status)``:

        * ``RESP_OK``  - the data is good;
        * ``RESP_NO_DATA`` - the tag does not have that memory.  It is a
          definite answer: a read that does not fit the bank always gets
          it, and that is what makes it possible to find the exact end
          of a bank;
        * ``0`` - the reader did not answer properly.  Nothing is known
          and the attempt is worth repeating.

        A read longer than 8 words is split, and the status of the last
        piece is the one returned.
        """
        if retries is None:
            retries = self.READ_RETRIES
        if retry_delay is None:
            retry_delay = self.READ_DELAY
        out = b""
        status = RESP_OK
        done = 0
        while done < word_count:
            chunk = min(MAX_READ_WORDS, word_count - done)
            data, status = self._read_chunk(mem, word_ptr + done, chunk,
                                            password, retries, retry_delay,
                                            pace)
            if status != RESP_OK:
                return b"", status
            out += data
            done += chunk
        return out, status

    def _read_chunk(self, mem: int, word_ptr: int, word_count: int,
                    password: bytes, retries: int, retry_delay: float,
                    pace: bool):
        """One read command (at most MAX_READ_WORDS words).

        Returns ``(data, status)``: the data is only meaningful when the
        status is RESP_OK.  A status of RESP_NO_DATA means the tag does
        not have that memory, which is a definite answer and does not
        need to be retried.  A status of 0 means the reader did not
        answer properly and the attempt is worth repeating.
        """
        with self._lock:
            data = (ctypes.c_ubyte * 256)()
            pwd = (ctypes.c_ubyte * 4)(*password)
            stale = 0
            status = 0
            for attempt in range(max(1, retries)):
                if attempt:
                    time.sleep(retry_delay)
                elif pace:
                    self._pace()
                self._wipe_response()
                self._lib.CFHid_ReadCardG2(
                    DEV_ADDR, pwd, mem, word_ptr, word_count, data)
                # The library hands over the last frame it received, even
                # when it is the answer of a *previous* command; after the
                # wipe the buffer only holds a frame if the reader answered
                # right now, and a frame that does not match the request is
                # discarded instead of being passed off as data.
                frame = self._read_response_frame()
                parsed = parse_module_response(frame) if frame else None
                if parsed is None:
                    status = 0
                    continue
                ftype, status, body = parsed
                if ftype != RESP_READ:
                    status = 0
                    stale += 1
                    continue
                if status == RESP_OK and len(body) == word_count * 2:
                    # The data is taken from the frame, not from the buffer the
                    # vendor library fills: that buffer can still hold the
                    # bytes of a previous answer, and the frame is what was
                    # validated.
                    return bytes(body), RESP_OK
                if status == RESP_NO_DATA:
                    # the tag does not have that memory: a real answer, and
                    # the same one every time
                    return b"", RESP_NO_DATA
                stale += 1
            extra = f", {stale} stale" if stale else ""
            raise ReaderError(
                f"ReadCardG2 failed (bank={mem}, word={word_ptr}, "
                f"words={word_count}, {retries} attempts{extra}, status "
                f"0x{status:02X}). Is a tag on the antenna, and is the access "
                f"password correct?"
            ) from None


    def _read_at(self, reader, mem, offset, size, password, budget):
        """Read a chunk at an offset, retrying until the budget is spent."""
        limit = time.monotonic() + budget
        while time.monotonic() < limit:
            try:
                return reader.read_card(mem, offset, size, password,
                                        retries=1, pace=True)
            except ReaderError:
                time.sleep(0.04)
        return None

    def probe_bank(self, mem: int, password: bytes = b"\x00" * 4,
                   max_words: int = 64, chunk: int = MAX_READ_WORDS,
                   bank_budget: float = 12.0, offset_budget: float = 0.8):
        """How much of a memory bank the tag really has - and its content.

        The idea is the one the vendor's software uses (read a range;
        when the tag does not have that memory the read does not answer
        and the memory ends there), with the reader's three behaviours
        worked around:

        * the answer carries a **status**: RESP_OK when the tag
          answered, RESP_NO_DATA (0x83) when the tag does not have that
          memory - a definitive answer - and 0 when the reader did not
          answer at all;
        * in a bad phase the reader answers with that same error status
          although the memory is there, so a "no data" is only believed
          when a control read at a word that exists answers too;
        * the reader can answer with the frame of a **previous**
          command, so the reads that decide the end of the bank are
          done with two different sizes: a frame left over from the
          previous one has another length and is discarded.

        The last word of the bank is confirmed with **two** reads of
        different sizes, so a frame that was not fresh cannot be
        counted twice.

        Returns ``(bytes, words)``.
        """
        deadline = time.monotonic() + bank_budget

        # the last offset that answered: it is the control to know
        # whether the reader is healthy
        control = [0]

        def probe(offset, size_, tries=3):
            """(outcome, data): 'ok', 'nodata' or 'unknown'."""
            for _ in range(max(1, tries)):
                try:
                    data, status = self.read_card_status(
                        mem, offset, size_, password, retries=1)
                except ReaderError:
                    status = 0
                if status == RESP_OK:
                    control[0] = offset
                    return "ok", data
                if status == RESP_NO_DATA:
                    try:
                        _, cstatus = self.read_card_status(
                            mem, control[0], 1, password, retries=1)
                    except ReaderError:
                        cstatus = 0
                    if cstatus == RESP_OK:
                        return "nodata", b""
                    # the reader is not answering: this error means
                    # nothing, try again
                    continue
            return "unknown", b""

        # 1. the size of the read that fits the bank: a read that does
        #    not fit is refused by the tag
        size = min(chunk, max_words)
        reason = "no_answer"
        while size > 1:
            outcome, _ = probe(0, size, tries=3)
            if outcome == "ok":
                reason = "ok"
                break
            if outcome == "nodata":
                # the tag does not have that memory: the bank is
                # smaller than the read
                size = max(1, size // 2)
                continue
            size = max(1, size // 2)
        if reason != "ok":
            # nothing answered: the reader is not answering, which is
            # NOT the same as the tag not having the memory
            return b"", 0, "no_answer"

        # 2. the walk, chunk by chunk.  The reader has a small budget of
        #    commands before it stops answering (measured: about 30 to 60),
        #    so the walk uses the biggest chunk that fits and does not
        #    waste commands.
        data = b""
        offset = 0
        while offset < max_words and time.monotonic() < deadline:
            outcome, chunk = probe(offset, size, tries=3)
            if outcome == "ok":
                data += chunk
                offset += size
                continue
            if outcome == "nodata":
                reason = "ok"
                break
            reason = "no_answer"
            break

        # 3. the last words, one at a time, each one confirmed with a
        #    second read of a different size, so a frame that was not
        #    fresh cannot be counted twice
        words = offset
        while offset < max_words and time.monotonic() < deadline:
            first = probe(offset, 1, tries=2)
            if first[0] != "ok":
                break
            second = probe(offset, 2, tries=2)
            if second[0] == "ok":
                data += second[1][:2]
                offset += 1
                words = offset
                continue
            if second[0] == "nodata":
                # the word exists but the next one does not: the bank
                # ends here
                data += first[1][:2]
                offset += 1
                words = offset
                continue
            break

        return data, words, reason

    def read_current_epc(self, retries: int = 14):
        """The EPC of the tag that is really in front of the reader.

        The active mode stream of the reader also contains **history**
        (tags it saw earlier, with their old RSSI), so it cannot be used
        to tell what is in the field.  A memory read does: the reader
        answers with the tag it has selected, which is the one in the
        field.

        Two consecutive reads have to agree, and the CRC that comes with
        the EPC has to match it, which rules out an answer that was not
        fresh.  Returns ``(epc, crc, pc)`` with the EPC without the
        CRC/PC header.
        """
        last = b""
        for _ in range(max(1, retries)):
            try:
                data = self.read_card(MEM_EPC, 0, 8, b"\x00" * 4, retries=1)
            except ReaderError:
                time.sleep(0.05)
                continue
            if len(data) < 6:
                continue
            epc = data[4:]
            if not any(epc):
                continue
            if not epc_crc_ok(epc, data[0:2]):
                # the CRC the reader put in front does not match the
                # EPC: the answer was not fresh (or not an EPC at all)
                continue
            if last and last != epc:
                # the reader answered with a different tag: try again
                last = b""
                time.sleep(0.05)
                continue
            if last == epc:
                return epc, data[0:2], data[2:4]
            last = epc
            time.sleep(0.05)
        raise ReaderError(
            "the reader did not give a consistent EPC for the tag in "
            "front of it") from None

    def del_dev_cache(self) -> bool:
        """Drop the reader's cache of the tags it saw (command 0x44).

        The cache is what makes the reader report tags that are no longer
        in the field.
        """
        with self._lock:
            if not self._open:
                return False
            self._pace()
            ok = bool(self._lib.CFHid_SendCommand(0x44, None, 0))
            self._pace()
            return ok

    def current_epc_quick(self, tries: int = 4) -> Optional[bytes]:
        """The EPC of the tag the reader points at, or None.

        Cheap enough to use as a guard around another read: the reader
        answers with the tag it has selected, and that selection can
        change between two commands when several tags are in the field.
        The reader refuses a lot of commands, so it is asked a few times.
        """
        for _ in range(max(1, tries)):
            try:
                data = self.read_card(MEM_EPC, 0, 8, b"\x00" * 4, retries=1)
            except ReaderError:
                continue
            if len(data) < 6:
                continue
            epc = data[4:]
            if any(epc):
                return epc
        return None

    def read_for_tag(self, target: bytes, bank: int, word_ptr: int = 0,
                     word_count: int = 8, password: bytes = b"\x00" * 4,
                     tries: int = 8, delay: float = 0.05) -> Optional[bytes]:
        """Read a chunk, but only while the reader points at ``target``.

        With several tags in the field the reader re-targets between
        commands, so a read can come from another tag.  The read is
        therefore guarded: the reader has to point at the target before
        *and* after the read, otherwise the answer is discarded and the
        read is repeated.

        This is what keeps two tags from sabotaging each other, without
        assuming anything about the chip.
        """
        for _ in range(max(1, tries)):
            before = self.current_epc_quick()
            if before != target:
                time.sleep(delay)
                continue
            try:
                data = self.read_card(bank, word_ptr, word_count, password,
                                      retries=1)
            except ReaderError:
                time.sleep(delay)
                continue
            after = self.current_epc_quick()
            if after != target:
                time.sleep(delay)
                continue
            return data
        return None

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
        with self._lock:
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



    def write_card_verified(self, data: bytes, mem: int = MEM_EPC,
                            word_ptr: int = 0,
                            password: bytes = b"\x00" * 4,
                            attempts: int = 3) -> bool:
        """Write a chunk and check that the tag really stored it.

        The reader answers a write when it **sent** it, not when the tag
        stored it, so the data is read back and compared word by word.
        A read back that does not match is either an answer that was not
        fresh or a write that did not take: the write is repeated.

        This is what makes it safe to configure a tag: what is reported
        as written was really read from the tag afterwards.
        """
        if len(data) % 2:
            raise ReaderError("data length must be even (whole 16-bit words)")
        for attempt in range(max(1, attempts)):
            self.write_card(data, mem, word_ptr, password, retries=2)
            try:
                back = self.read_card(mem, word_ptr, len(data) // 2,
                                      password, retries=4)
            except ReaderError:
                time.sleep(0.1)
                continue
            if back == data:
                return True
            time.sleep(0.1)
        raise ReaderError(
            "the write could not be verified: the tag did not read back "
            "what was written"
        ) from None

    def write_epc(self, epc: bytes, password: bytes = b"\x00" * 4,
                  retries: int = 3, verify: bool = True) -> bool:
        with self._lock:
            pwd = (ctypes.c_ubyte * 4)(*password)
            buf = (ctypes.c_ubyte * len(epc))(*epc)
            for _ in range(max(1, retries)):
                self._pace()
                if self._lib.CFHid_WriteEPCG2(DEV_ADDR, pwd, buf, len(epc)) == 1:
                    self._pace(settle=True)
                    if not verify:
                        return True
                    # verificacion tolerante a frames viejos: el tag tarda en
                    # quedar grabado y el lector puede devolver la lectura previa.
                    # Se reintenta LEER (sin reescribir) antes de declarar fallo.
                    words = max(4, (len(epc) + 4 + 1) // 2)
                    time.sleep(0.15)
                    for _v in range(4):
                        try:
                            back = self.read_card(MEM_EPC, 0, words, password,
                                                  retries=2)
                        except ReaderError:
                            time.sleep(0.12)
                            continue
                        if back[4:4 + len(epc)] == epc:
                            return True
                        time.sleep(0.12)
                    time.sleep(0.1)
                    continue
                time.sleep(0.05)
            raise ReaderError(
                "WriteEPCG2 failed or could not be verified. Is a tag on the "
                "antenna?"
            ) from None

        # -- misc --------------------------------------------------------------
            # -- misc --------------------------------------------------------------
                # -- misc --------------------------------------------------------------
    def relay(self, on: bool) -> bool:
        with self._lock:
            fn = self._lib.CFHid_RelayOn if on else self._lib.CFHid_RelayOff
            if fn(DEV_ADDR) != 1:
                raise ReaderError("Relay command failed")
            return True

    def reboot(self) -> bool:
        with self._lock:
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
            self._wipe_response()
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


    def _wipe_response(self) -> bool:
        """Blank the vendor library's answer buffer.

        The library hands over the last frame it received, even when it
        is the answer of a *previous* command.  Blanking the buffer
        before the command makes "no answer" detectable: after a wipe the
        buffer only holds a frame if the reader answered right now.
        """
        base = getattr(self, "_lib_base", 0)
        if not base:
            return False
        try:
            ctypes.memset(base + RESPONSE_BUFFER_OFFSET, 0,
                          RESPONSE_BUFFER_WINDOW)
            return True
        except (OSError, ValueError):
            return False

    def _response_matches(self, rtype: int, payload_len: int,
                          strict: bool = False) -> bool:
        """Does the last frame answer exactly what was asked?

        When there is no frame to check the answer is trusted: a wrong
        guess here would break every read, so the check is only applied
        when a well formed frame is actually there.
        """
        frame = self._read_response_frame()
        if not frame:
            return not strict
        parsed = parse_module_response(frame)
        if parsed is None:
            return not strict
        ftype, status, body = parsed
        if ftype != rtype or status != 0x01:
            return False
        return len(body) == payload_len

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


# The TID is normalized to its first 12 bytes (96 bits), so a fingerprint
# learned before the probe measured the exact end of the bank still
# matches.
TID_FINGERPRINT_LEN = 12


def learn_fingerprint(reader, epc: bytes) -> str:
    """Read the TID of the tag in front of the reader and remember it."""
    tid, _ = reader.probe_bank(MEM_TID)[0:2]
    if not tid:
        raise ReaderError("the TID could not be read")
    tid = tid[:TID_FINGERPRINT_LEN]
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
    tid, _ = reader.probe_bank(MEM_TID)[0:2]
    if not tid:
        return False, "no se pudo leer el TID para verificar la huella"
    tid = tid[:TID_FINGERPRINT_LEN]
    if tid.hex().upper() != expected.upper():
        return False, (f"huella no coincide: TID {tid.hex().upper()} "
                        f"!= {expected.upper()}")
    return True, "huella verificada"
