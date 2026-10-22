#!/usr/bin/env python3
"""Mock RU5600 antenna for testing the TCP transport.

Speaks the module protocol of the real reader (PROTOCOL.md) so the client
in `ru5600_net.py` can be exercised without the hardware.  It also
rejects a frame with a wrong checksum, exactly like the reader does.

    python3 tools/mock_antenna.py [port]
"""
from __future__ import annotations

import socket
import struct
import sys
import threading

MAGIC_SW = b"\x53\x57"
MAGIC_CT = b"\x43\x54"

# a fake tag: EPC 96 bit + TID
EPC = bytes.fromhex("E20047019951662B039FD1AD")
TID = bytes.fromhex("E28038132000662B039FD1AD")
TAG_RECORD = bytes([0x0F, 0x03, 0x01]) + EPC + bytes([0x39])  # len, type, ant


def checksum(data: bytes) -> int:
    return (-sum(data)) & 0xFF


def make_response(cmd: int, payload: bytes = b"", count: int = 0,
                  status: int = 0x01) -> bytes:
    body = bytes([0x00, cmd, status]) + struct.pack(">H", count) + payload
    frame = bytearray(MAGIC_CT)
    frame += struct.pack(">H", len(body) + 1)
    frame += body
    frame.append(checksum(bytes(frame)))
    return bytes(frame)


def handle(conn):
    rx = b""
    streaming = False
    while True:
        try:
            chunk = conn.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        rx += chunk
        while len(rx) >= 2:
            if len(rx) < 2:
                break
            length = (rx[0] << 8) | rx[1]
            if len(rx) < 2 + length:
                break
            frame = rx[2:2 + length]
            rx = rx[2 + length:]
            if frame[:2] != MAGIC_SW or len(frame) < 7:
                continue
            if checksum(frame[:-1]) != frame[-1]:
                print("  mock: bad checksum, ignored")
                continue
            inner = (frame[2] << 8) | frame[3]
            cmd = frame[5]
            payload = frame[7:]
            print(f"  mock: cmd 0x{cmd:02X} len={inner} "
                  f"payload={payload.hex().upper()}")
            if cmd == 0x10:                       # system info
                conn.sendall(make_response(cmd, bytes([0x71, 0x11]) + TID[:7]))
            elif cmd == 0x01:                     # inventory
                conn.sendall(make_response(cmd, TAG_RECORD, count=1))
            elif cmd == 0x23:                     # read one param
                conn.sendall(make_response(cmd, bytes([18])))
            elif cmd == 0x24:                     # set one param
                conn.sendall(make_response(cmd))
            elif cmd == 0x3E:                     # read freq
                conn.sendall(make_response(cmd, bytes([0x31, 0x80])))
            elif cmd == 0x3F:                     # set freq
                conn.sendall(make_response(cmd))
            elif cmd == 0x02:                     # read card
                conn.sendall(make_response(cmd, b"\x2f\x7d\x60\x00" + EPC))
            elif cmd == 0x03:                     # write card
                conn.sendall(make_response(cmd))
            elif cmd == 0x04:                     # write epc
                conn.sendall(make_response(cmd))
            elif cmd == 0x41:                     # start read
                conn.sendall(make_response(cmd))
                streaming = True
            elif cmd == 0x40:                     # stop read
                conn.sendall(make_response(cmd))
                streaming = False
            elif cmd == 0x85:
                conn.sendall(make_response(cmd))
            elif cmd == 0x86:
                conn.sendall(make_response(cmd))
            elif cmd == 0x30:
                conn.sendall(make_response(cmd))
            else:
                conn.sendall(make_response(cmd))
        if streaming:
            # the reader streams the tag records
            for _ in range(3):
                conn.sendall(make_response(0x01, TAG_RECORD, count=1))
                time.sleep(0.05)
            streaming = False
    conn.close()


import time  # noqa: E402


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 60123
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    print(f"mock antenna on 127.0.0.1:{port}")
    while True:
        conn, _ = srv.accept()
        print("  mock: client connected")
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
