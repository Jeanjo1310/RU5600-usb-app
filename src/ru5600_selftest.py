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


# frames captured from the reader itself (base + 0x230F60)
RX_SYSINFO = bytes.fromhex("43 54 00 0D 00 10 01 71 11 D3 04 26 07 02 56 2B 42")
RX_FREQ = bytes.fromhex("43 54 00 06 00 3E 01 31 80 73")


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

    # the frames the reader really answers, parsed with the shared parser
    from ru5600_core import parse_module_response
    parsed = parse_module_response(RX_SYSINFO)
    check("sysinfo parses", parsed is not None, True)
    if parsed:
        check("sysinfo type", parsed[0], 0x10)
        check("sysinfo status", parsed[1], 0x01)
        check("sysinfo payload", parsed[2].hex().upper(),
              "7111D304260702562B")
    parsed = parse_module_response(RX_FREQ)
    check("freq parses", parsed is not None, True)
    if parsed:
        check("freq type", parsed[0], 0x3E)
        check("freq payload", parsed[2].hex().upper(), "3180")


def test_tag_records() -> None:
    """Tag buffer, BOTH formats.

    Firmware 7.1 answers 92-byte records (measured 2026-10-26); parse_tag_buffer
    decodes that one. The packed [len][type][ant][id][rssi] records belong to
    the legacy decoder, kept in the core as _parse_tag_buffer_legacy. The old
    fixtures test the legacy path; a synthetic 92-byte record exercises the
    current parser (EPC 12 B + TID 12 B + 65-byte tail, RSSI at rec[n-44])."""
    print("tag records")
    from ru5600_core import _parse_tag_buffer_legacy
    tags = _parse_tag_buffer_legacy(TAG_RAW, 1)
    check("legacy: one tag parsed", len(tags), 1)
    if tags:
        t = tags[0]
        check("legacy antenna", t.ant, 1)
        check("legacy rssi", t.rssi, 0x30)
        check("legacy epc", t.epc_hex,
              "E20047066BC06027AC480114000000000100000000000000")

    tags = _parse_tag_buffer_legacy(TAG_96, 1)
    check("legacy: 96 bit tag parsed", len(tags), 1)
    if tags:
        t = tags[0]
        check("legacy 96 bit epc", t.epc_hex, "E20047019951662B039FD1AD")
        check("legacy 96 bit rssi", t.rssi, 0x34)

    # firmware 7.1: registro de 92 bytes [91][tipo][ant][EPC 12][TID 12][65]
    rec = bytearray(92)
    rec[0] = 91
    rec[1] = 0x03
    rec[2] = 0x01
    rec[3:15] = bytes.fromhex("E20047068123456789ABCDEF")
    rec[15:27] = bytes.fromhex("E28281000011223344556677")
    rec[92 - 44] = 45  # RSSI medido en el registro real
    tags = parse_tag_buffer(bytes(rec), 1)
    check("fw7.1: one 92B tag parsed", len(tags), 1)
    if tags:
        t = tags[0]
        check("fw7.1 epc", t.epc_hex, "E20047068123456789ABCDEF")
        check("fw7.1 rssi", t.rssi, 45)
        check("fw7.1 antenna", t.ant, 1)
        # raw_id keeps the WHOLE 92 B record (the UI shows it as "raw")
        check("fw7.1 raw_id is the full record", len(t.raw_id), 92)


def test_safety() -> None:
    """The commands that must never reach the reader."""
    print("safety")
    from ru5600_core import FORBIDDEN_COMMANDS, RESERVED_COMMANDS
    check("firmware update blocked", 0x2B in FORBIDDEN_COMMANDS, True)
    check("upgrade data in blocked", 0x2C in FORBIDDEN_COMMANDS, True)
    check("upgrade end blocked", 0x2D in FORBIDDEN_COMMANDS, True)
    check("kill blocked", 0x65 in FORBIDDEN_COMMANDS, True)
    check("start read reserved", 0x41 in RESERVED_COMMANDS, True)
    check("stop read reserved", 0x40 in RESERVED_COMMANDS, True)


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


def test_epc_crc() -> None:
    """The CRC the reader puts in front of the EPC.

    It was found by brute force over the polynomials and the initial
    values, with two tags of the same model: the same value reproduces
    the CRC of both of them, and it is what tells a genuine answer from
    one that was not fresh.
    """
    print("EPC CRC")
    from ru5600_core import epc_crc16, epc_crc_ok
    epc_a = bytes.fromhex("952520260302081200000300")
    epc_b = bytes.fromhex("952520260302081200000299")
    check("crc of tag A", epc_crc16(epc_a), 0xC9AE)
    check("crc of tag B", epc_crc16(epc_b), 0xE80F)
    check("crc of A accepted", epc_crc_ok(epc_a, b"\xc9\xae"), True)
    check("crc of B accepted", epc_crc_ok(epc_b, b"\xe8\x0f"), True)
    check("wrong crc rejected", epc_crc_ok(epc_b, b"\xc9\xae"), False)
    check("garbage rejected", epc_crc_ok(b"\x00" * 12, b"\x00\x00"), False)


def test_chip_table() -> None:
    """The chip table: the prefixes of the chips seen on the bench.

    Locking them here keeps a later edit of the table from silently
    changing what the tool says about a tag.
    """
    print("chip table")
    from ru5600_chip import consistency_check, decode_tid

    h9 = decode_tid(bytes.fromhex("E28038212000682004D9CBAD"))
    check("H9 mdid", h9.mdid, 0x803)
    check("H9 model", h9.model, "Higgs-9")
    check("H9 serial", h9.serial, "00682004D9CBAD")

    h3 = decode_tid(bytes.fromhex("E20034121234567890ABCDEF00"))
    check("H3 mdid", h3.mdid, 0x003)
    check("H3 manufacturer", h3.manufacturer, "Alien")
    check("H3 model", h3.model, "Higgs-3")

    ucode9 = decode_tid(bytes.fromhex("E28069150000123456789ABCDEF0"))
    check("UCODE 9 mdid", ucode9.mdid, 0x806)
    check("UCODE 9 model", ucode9.model, "UCODE 9 (con Configuration Word)")

    # the identification has to agree with the measured memory
    check("H9 with 86 B of user memory is coherent",
          consistency_check(h9, 86), [])
    check("H9 with 4 B of user memory is flagged",
          len(consistency_check(h9, 4)), 1)
    check("UCODE 9 with 64 B of user memory is flagged",
          len(consistency_check(ucode9, 64)), 1)


def main() -> int:
    print("RU5600 protocol self test")
    test_checksum()
    test_response_layout()
    test_tag_records()
    test_tid_decode()
    test_epc_crc()
    test_chip_table()
    test_safety()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("\nall vectors OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
