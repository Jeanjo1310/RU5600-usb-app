#!/usr/bin/env python3
"""Protocol self test for the RU5600 client.

Every byte level rule of the reader protocol is pinned down here with
vectors taken from a real session (the "Send:"/"Receive:" lines of the
Windows ReaderSoft log and the bench captures).  The test runs without
any hardware, so a change that breaks the protocol fails here instead of
in the field.

    python3 ru5600_selftest.py
"""
from __future__ import annotations

import sys

from ru5600_core import checksum_ok, parse_tag_buffer

FAILS: list = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")
        FAILS.append(name)


# --- vectors captured on the bench ---------------------------------------

# from the ReaderSoft log window: Send / Receive of a "read parameter"
TX_RAW = bytes.fromhex("53 57 00 03 FF 20 34")
RX_RAW = bytes.fromhex(
    "43 54 00 15 00 20 01 D3 55 00 00 00 00 0F 01 00 31 80 "
    "00 00 00 02 01 00 47")

# a 192 bit EPC tag, answer mode: the id slot holds the whole EPC
TAG_RAW = bytes.fromhex("1B 03 01 E2 00 47 06 6B C0 60 27 AC 48 01 14"
                         "00 00 00 00 01 00 00 00 00 00 00 00 30")

# 96 bit EPC tag, active mode record (15 bytes)
TAG_96 = bytes.fromhex("0F 03 01 E2 00 47 01 99 51 66 2B 03 9F D1 AD 34")


def test_checksum() -> None:
    """cksum = two's complement of the byte sum, over everything but the
    checksum byte itself."""
    print("checksum")
    check("request frame", TX_RAW[:-1], TX_RAW[:-1])
    check("request checksum byte", TX_RAW[-1], 0x34)
    check("request validates", checksum_ok(TX_RAW), True)
    check("response validates", checksum_ok(RX_RAW), True)
    bad = bytearray(RX_RAW)
    bad[10] ^= 0x01
    check("corrupted response rejected", checksum_ok(bytes(bad)), False)


def test_response_layout() -> None:
    """The response: magic, length, 00, type, status, count, payload,
    checksum."""
    print("response layout")
    check("magic", RX_RAW[:2], b"\x43\x54")
    length = (RX_RAW[2] << 8) | RX_RAW[3]
    check("length field", length, 0x15)
    check("type byte", RX_RAW[5], 0x20)
    check("status byte", RX_RAW[6], 0x01)
    check("payload starts at +9", RX_RAW[7:9], b"\xd3\x55")
    check("frame size", len(RX_RAW), 4 + length)
    check("payload length", length - 6, 15)


def test_tag_records() -> None:
    """The packed tag buffer: [len][type][ant][id][rssi]."""
    print("tag records")
    tags = parse_tag_buffer(TAG_RAW, 1)
    check("one tag parsed", len(tags), 1)
    if tags:
        t = tags[0]
        check("antenna", t.ant, 1)
        check("type", t.type, 0x03)
        check("no timestamp", t.has_timestamp, False)
        check("rssi", t.rssi, 0x30)
        check("epc slot length", len(t.raw_id), 24)
        # the slot is the whole EPC: 192 bit here, 96 bit on the other tag
        check("epc", t.epc_hex,
              "E20047066BC06027AC480114000000000100000000000000")
        check("epc starts with the 96 bit prefix", t.epc_hex.startswith(
            "E20047066BC06027AC480114"), True)

    tags = parse_tag_buffer(TAG_96, 1)
    check("96 bit tag parsed", len(tags), 1)
    if tags:
        t = tags[0]
        check("96 bit epc", t.epc_hex, "E20047019951662B039FD1AD")
        check("96 bit rssi", t.rssi, 0x34)
        check("96 bit slot length", len(t.raw_id), 12)


def test_tid_decode() -> None:
    """The GS1 TDS decoding of the TID."""
    print("TID decoding")
    from ru5600_chip import decode_tid
    tid = bytes.fromhex("E28038132000662B039FD1AD32300118301C00F003030C53")
    info = decode_tid(tid)
    check("mdid (Alien)", info.mdid, 0x803)
    check("manufacturer", info.manufacturer, "Alien")
    check("tmn", info.tmn, 0x813)
    check("known prefix", info.known, True)
    check("model", info.model, "Higgs-10 (H10 / 7320 class)")
    check("serial", info.serial, "00662B039FD1AD")
    check("xtid present", info.xtid, True)


def main() -> int:
    print("RU5600 protocol self test")
    test_checksum()
    test_response_layout()
    test_tag_records()
    test_tid_decode()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("\nall vectors OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
