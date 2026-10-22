#!/usr/bin/env python3
"""TCP transport for the networked RU5600 antennas.

The vendor ships a Linux library only for the USB desktop reader
(`libCFHidApi.so`).  The networked antennas speak the **same module
protocol**, just carried over TCP instead of USB - that was confirmed by
decompiling `SWNetApi.dll` from the Windows SDK:

| function            | command | response wait   |
|---------------------|---------|-----------------|
| `SWNet_InventoryG2` | 0x01    | type 0x01       |
| `SWNet_ReadCardG2`  | 0x02    | type 0x02       |
| `SWNet_GetDeviceSystemInfo` | 0x10 | type 0x10  |
| `SWNet_ReadDeviceOneParam`  | 0x23 | type 0x23  |
| `SWNet_SetDeviceOneParam`  | 0x24 | type 0x24  |
| `SWNet_ReadFreq`    | 0x3E    | type 0x3E       |
| `SWNet_StartRead`   | 0x41    | type 0x41       |
| `SWNet_CommunicateWithModule` | 0xFE | any        |

The frames are the ones documented in PROTOCOL.md:

```
request   : 53 57 | len | FF | addr | payload | checksum
response  : 43 54 | len | 00 | type | status | count | payload | checksum
```

The request is sent with a two byte length prefix; the response is
parsed by looking for the `CT` magic, so both framed and unframed
answers are accepted.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Callable, List, Optional, Tuple

from ru5600_core import (
    FREQ_BANDS,
    MEM_EPC,
    MEM_TID,
    MEM_USER,
    PARAM_BEEP,
    PARAM_FILTERTIME,
    PARAM_RFPOWER,
    PARAM_WORKMODE,
    WORKMODE_ACTIVE,
    WORKMODE_ANSWER,
    Detection,
    ReaderError,
    Tag,
    checksum_ok,
    parse_tag_buffer,
)

MAGIC_SW = b"\x53\x57"
MAGIC_CT = b"\x43\x54"

CMD_INVENTORY = 0x01
CMD_READ_CARD = 0x02
CMD_SYSTEM_INFO = 0x10
CMD_READ_PARAM = 0x23
CMD_SET_PARAM = 0x24
CMD_READ_FREQ = 0x3E
CMD_SET_FREQ = 0x3F
CMD_START_READ = 0x41
CMD_WRITE_CARD = 0x03
CMD_WRITE_EPC = 0x04
CMD_RELAY_ON = 0x85
CMD_RELAY_OFF = 0x86
CMD_REBOOT = 0x30
CMD_STOP_READ = 0x40

ADDR_BROADCAST = 0xFF

# how the reader answers when it does not like the frame
STATUS_OK = 0x01


def checksum(data: bytes) -> int:
    """The reader's checksum: two's complement of the byte sum."""
    return (-sum(data)) & 0xFF





def build_request(cmd: int, payload: bytes = b"", addr: int = 0xFF) -> bytes:
    """Build a module request frame.

    The layout is the one of the vendor's own buffer:

    ```
    [0] 53 57   magic
    [2] len     (u16 BE) = payload + 3
    [4] FF      fixed
    [5] cmd     the command code
    [6] payload
    ..  checksum
    ```
    """
    frame = bytearray()
    frame += MAGIC_SW
    frame += struct.pack(">H", len(payload) + 3)
    frame.append(0xFF)
    frame.append(cmd & 0xFF)
    frame += payload
    frame.append(checksum(bytes(frame)))
    return bytes(frame)


def parse_response(data: bytes) -> Optional[Tuple[int, int, int, bytes]]:
    """Parse a response frame.

    Returns ``(type, status, count, payload)`` or None when the data does
    not hold a valid frame.
    """
    idx = data.find(MAGIC_CT)
    if idx < 0 or len(data) < idx + 10:
        return None
    f = data[idx:]
    length = (f[2] << 8) | f[3]
    if len(f) < 4 + length:
        return None
    body = f[4:4 + length]
    if not body:
        return None
    # the checksum covers every byte of the frame except the last one
    if not checksum_ok(f[:4 + length]):
        return None
    rtype = body[1] if len(body) > 1 else 0
    status = body[2] if len(body) > 2 else 0
    count = (body[3] << 8) | body[4] if len(body) > 4 else 0
    payload = body[5:-1]
    return rtype, status, count, payload


class NetReader:
    """RU5600 antenna over TCP.

    Same public API as the USB :class:`ru5600_core.Ru5600Reader`, so the
    GUI can drive both without knowing the difference.
    """

    def __init__(self, ip: str, port: int = 60000,
                 on_tags: Optional[Callable[[List[Tag]], None]] = None,
                 timeout: float = 1.0):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self._on_tags = on_tags
        self._sock: Optional[socket.socket] = None
        self._rx = b""
        self._lock = threading.RLock()
        self._open = False
        self._last_cmd = 0.0
        self._stream_tags: List[Tag] = []
        self._framed = True   # the request carries a two byte length prefix

    # -- transport --------------------------------------------------------
    def _pace(self, settle: float = 0.06) -> None:
        elapsed = time.monotonic() - self._last_cmd
        if elapsed < settle:
            time.sleep(settle - elapsed)
        self._last_cmd = time.monotonic()

    def open(self) -> bool:
        with self._lock:
            if self._open:
                return True
            try:
                self._sock = socket.create_connection(
                    (self.ip, self.port), timeout=self.timeout)
                self._sock.settimeout(self.timeout)
            except OSError as exc:
                raise ReaderError(
                    f"Could not connect to {self.ip}:{self.port} - {exc}") from exc
            self._rx = b""
            self._open = True
            self._last_cmd = time.monotonic()
            return True

    def close(self) -> bool:
        with self._lock:
            if not self._open:
                return False
            try:
                if self._sock:
                    self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._open = False
            return True

    @property
    def is_open(self) -> bool:
        return self._open

    def _send(self, cmd: int, payload: bytes = b"") -> None:
        if not self._sock:
            raise ReaderError("the reader is not connected")
        frame = build_request(cmd, payload)
        data = struct.pack(">H", len(frame)) + frame if self._framed else frame
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise ReaderError(f"send failed: {exc}") from exc

    def _recv_frame(self, timeout: float) -> Optional[bytes]:
        """Read one frame from the socket."""
        deadline = time.monotonic() + timeout
        while True:
            idx = self._rx.find(MAGIC_CT)
            if idx >= 0 and len(self._rx) >= idx + 4:
                length = (self._rx[idx + 2] << 8) | self._rx[idx + 3]
                if len(self._rx) >= idx + 4 + length:
                    frame = self._rx[idx:idx + 4 + length]
                    self._rx = self._rx[idx + 4 + length:]
                    return frame
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self._sock.settimeout(left)
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                return None
            except OSError as exc:
                raise ReaderError(f"receive failed: {exc}") from exc
            if not chunk:
                raise ReaderError("the reader closed the connection")
            self._rx += chunk

    def _transact(self, cmd: int, payload: bytes = b"",
                  timeout: float = 1.0) -> Tuple[int, int, int, bytes]:
        """Send a command and wait for its answer."""
        self._pace()
        self._send(cmd, payload)
        deadline = time.monotonic() + timeout
        while True:
            frame = self._recv_frame(max(0.05, deadline - time.monotonic()))
            if frame is None:
                raise ReaderError(f"no answer to command 0x{cmd:02X}")
            parsed = parse_response(frame)
            if parsed is None:
                continue
            rtype, status, count, body = parsed
            if rtype == cmd:
                if status != STATUS_OK:
                    raise ReaderError(
                        f"command 0x{cmd:02X} refused (status 0x{status:02X})")
                return rtype, status, count, body
            # an unsolicited frame: the reader is streaming tag data
            self._handle_stream(count, body)

    def _handle_stream(self, count: int, body: bytes) -> None:
        tags = parse_tag_buffer(body, count) if count else []
        if tags:
            self._stream_tags.extend(tags)
            if self._on_tags:
                try:
                    self._on_tags(tags)
                except Exception:
                    pass

    # -- device information ------------------------------------------------
    def system_info(self):
        from ru5600_core import SystemInfo
        _, _, _, body = self._transact(CMD_SYSTEM_INFO, timeout=1.0)
        raw = body[:9]
        if len(raw) < 9:
            raise ReaderError("short system info answer")
        return SystemInfo(
            soft_ver=f"{raw[0] >> 4}.{raw[0] & 0x0F}",
            hard_ver=f"{raw[1] >> 4}.{raw[1] & 0x0F}",
            device_sn=raw[2:9].hex().upper(),
        )

    def read_param(self, addr: int) -> int:
        _, _, _, body = self._transact(CMD_READ_PARAM, bytes([addr]))
        if not body:
            raise ReaderError(f"no value for parameter 0x{addr:02X}")
        return body[0]

    def set_param(self, addr: int, value: int) -> bool:
        self._transact(CMD_SET_PARAM, bytes([addr, value & 0xFF]))
        return True

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
        _, _, _, body = self._transact(CMD_READ_FREQ)
        return body[:2]

    def set_freq(self, band: str) -> bool:
        if band not in FREQ_BANDS:
            raise ReaderError(f"unknown frequency band: {band}")
        lo, hi = FREQ_BANDS[band]
        self._transact(CMD_SET_FREQ, bytes([lo, hi]))
        return True

    # -- inventory / read / write -----------------------------------------
    def inventory(self, rounds: int = 12, delay: float = 0.03) -> List[Tag]:
        tags: List[Tag] = []
        for _ in range(max(1, rounds)):
            try:
                _, _, count, body = self._transact(CMD_INVENTORY, timeout=1.0)
            except ReaderError:
                if delay:
                    time.sleep(delay)
                continue
            for tag in parse_tag_buffer(body, count):
                if tag.epc_hex not in [t.epc_hex for t in tags]:
                    tags.append(tag)
            if tags:
                break
            if delay:
                time.sleep(delay)
        return tags

    def detect(self, mode: str = "balanced") -> Detection:
        """One detection.  Over TCP the reader answers immediately, so the
        effort of the mode is the number of attempts."""
        rounds = {"burst": 1, "balanced": 4, "reliable": 12}.get(mode, 4)
        det = Detection()
        t0 = time.monotonic()
        for _ in range(max(1, rounds)):
            det.rounds += 1
            try:
                _, _, count, body = self._transact(CMD_INVENTORY, timeout=1.0)
            except ReaderError:
                continue
            det.hits += 1
            for tag in parse_tag_buffer(body, count):
                if tag.epc_hex not in [t.epc_hex for t in det.tags]:
                    det.tags.append(tag)
            if det.tags:
                break
        det.elapsed = time.monotonic() - t0
        return det

    def measure(self, duration: float = 5.0, poll: float = 0.0) -> dict:
        """Detection rate and RSSI while the reader streams."""
        rssis = []
        seen: dict = {}
        detections = 0
        self.set_work_mode(WORKMODE_ACTIVE)
        self.start_read()
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < duration:
                try:
                    frame = self._recv_frame(
                        max(0.05, duration - (time.monotonic() - t0)))
                except ReaderError:
                    break
                if frame is None:
                    continue
                parsed = parse_response(frame)
                if parsed is None:
                    continue
                _, _, count, body = parsed
                if count:
                    for tag in parse_tag_buffer(body, count):
                        detections += 1
                        rssis.append(tag.rssi)
                        seen[tag.epc_hex] = seen.get(tag.epc_hex, 0) + 1
                if poll:
                    time.sleep(poll)
        finally:
            self.stop_read()
            self.set_work_mode(WORKMODE_ANSWER)
        elapsed = time.monotonic() - t0
        return {
            "elapsed": elapsed,
            "polls": 0,
            "detections": detections,
            "detections_per_s": detections / elapsed if elapsed else 0.0,
            "tags": seen,
            "rssi_count": len(rssis),
            "rssi_avg": sum(rssis) / len(rssis) if rssis else 0.0,
            "rssi_min": min(rssis) if rssis else 0,
            "rssi_max": max(rssis) if rssis else 0,
        }

    def start_read(self) -> bool:
        self._transact(CMD_START_READ, timeout=1.0)
        return True

    def stop_read(self) -> bool:
        try:
            self._transact(CMD_STOP_READ, timeout=1.0)
        except ReaderError:
            pass
        return True

    def poll_tags(self) -> List[Tag]:
        tags = self._stream_tags
        self._stream_tags = []
        return tags

    def scan(self, dwell: float = 0.35, attempts: int = 4) -> List[Tag]:
        for _ in range(max(1, attempts)):
            self.set_work_mode(WORKMODE_ACTIVE)
            self.start_read()
            time.sleep(dwell)
            self.stop_read()
            tags = self.poll_tags()
            if tags:
                return tags
        return []

    def read_card(self, mem: int = MEM_EPC, word_ptr: int = 0,
                  word_count: int = 6, password: bytes = b"\x00" * 4,
                  retries: int = 10, retry_delay: float = 0.05) -> bytes:
        payload = bytes(password) + bytes([mem, word_ptr & 0xFF,
                                           word_count & 0xFF])
        last = None
        for _ in range(max(1, retries)):
            try:
                _, _, _, body = self._transact(CMD_READ_CARD, payload,
                                               timeout=1.0)
                return body[:word_count * 2]
            except ReaderError as exc:
                last = exc
                time.sleep(retry_delay)
        raise ReaderError(f"ReadCardG2 failed: {last}") from None

    def write_card(self, data: bytes, mem: int = MEM_EPC, word_ptr: int = 0,
                   password: bytes = b"\x00" * 4, retries: int = 3) -> bool:
        if len(data) % 2:
            raise ReaderError("data length must be even")
        payload = bytes(password) + bytes([mem, word_ptr & 0xFF,
                                           (len(data) // 2) & 0xFF]) + data
        for _ in range(max(1, retries)):
            try:
                self._transact(CMD_WRITE_CARD, payload, timeout=1.0)
                return True
            except ReaderError:
                time.sleep(0.05)
        raise ReaderError("WriteCardG2 failed")

    def write_epc(self, epc: bytes, password: bytes = b"\x00" * 4,
                  retries: int = 3) -> bool:
        payload = bytes(password) + bytes([0, 0, len(epc) & 0xFF]) + epc
        for _ in range(max(1, retries)):
            try:
                self._transact(CMD_WRITE_EPC, payload, timeout=1.0)
                return True
            except ReaderError:
                time.sleep(0.05)
        raise ReaderError("WriteEPCG2 failed")

    def relay(self, on: bool) -> bool:
        self._transact(CMD_RELAY_ON if on else CMD_RELAY_OFF)
        return True

    def reboot(self) -> bool:
        self._transact(CMD_REBOOT)
        return True
