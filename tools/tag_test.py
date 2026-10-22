#!/usr/bin/env python3
"""Tag tester: one comparable report per tag.

Place a tag on the antenna and run it.  It reports what the tag IS
(chip model from the TID), what it HAS (memory banks and their sizes)
and how it is PROTECTED (access password, locked banks).

    python3 tools/tag_test.py                 # one tag, human readable
    python3 tools/tag_test.py --csv tags.csv  # append a row per tag
    python3 tools/tag_test.py --quick         # skip the memory map

The reader only answers a memory read inside short windows (roughly one
command out of four), so each read is retried; a full report takes a few
seconds and the numbers are comparable between tags.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from ru5600_core import (  # noqa: E402
    MEM_EPC,
    MEM_RESERVED,
    MEM_TID,
    MEM_USER,
    ReaderError,
    Ru5600Reader,
)
from ru5600_chip import decode_tid  # noqa: E402

# TID prefix -> (chip name, EPC bytes, TID bytes, USER bytes) as published
EXPECTED = {
    "E2803813": ("Alien Higgs-10 (H10/7320)", 16, 28, 4),
    "E2003412": ("Alien Higgs-3", 16, 12, 512),
    "E2003414": ("Alien Higgs-4", 16, 12, 128),
    "E2003411": ("Alien Higgs-2", 16, 12, 0),
    "E2803821": ("Alien Higgs-9", 16, 12, 688),
    "E2806894": ("NXP UCODE 8", 16, 12, 0),
    "E2806994": ("NXP UCODE 8M", 16, 12, 32),
    "E2806995": ("NXP UCODE 9", 16, 12, 0),
    "E2806890": ("NXP UCODE 7", 16, 12, 0),
    "E2801100": ("Impinj Monza 4D", 16, 12, 32),
    "E2801114": ("Impinj Monza 4i", 16, 12, 480),
    "E280110C": ("Impinj Monza 4E", 16, 12, 128),
    "E2801130": ("Impinj Monza 5", 16, 12, 32),
    "E2801160": ("Impinj Monza R6", 16, 12, 0),
}

ZERO = b"\x00\x00\x00\x00"


def read(reader, bank, word, words, pwd=ZERO, retries=15):
    """One memory read, or None when the area does not answer.

    The reader answers roughly one command out of four, so a short retry
    budget is tried first (fast) and a long one only if that failed.
    """
    try:
        return reader.read_card(bank, word, words, pwd, retries=retries)
    except ReaderError:
        pass
    if retries < 40:
        try:
            return reader.read_card(bank, word, words, pwd, retries=40)
        except ReaderError:
            return None
    return None


def read_bank(reader, bank, pwd=ZERO, plan=((0, 8), (8, 4), (12, 2), (14, 1))):
    """Read a bank with a few chunk sizes; returns (bytes, word count)."""
    data = b""
    words = 0
    for word, size in plan:
        if word < words:
            continue
        part = read(reader, bank, word, size, pwd)
        if part is None:
            break
        data += part
        words += size
    return data, words


def test_tag(reader, quick=False, full=False):
    """Full report of the tag in front of the reader."""
    rep = {}
    t0 = time.time()

    # 1. TID first: after an inventory the reader is busy and the TID does
    #    not answer
    tid_data, tid_words = read_bank(reader, MEM_TID, plan=((0, 8),))
    info = decode_tid(tid_data)
    rep["tid"] = tid_data.hex().upper()
    rep["tid_len"] = tid_words * 2
    rep["prefix"] = info.prefix
    rep["mdid"] = f"0x{info.mdid:03X}"
    rep["tmn"] = f"0x{info.tmn:03X}"
    rep["chip"] = (f"{info.manufacturer} {info.model}" if info.known
                   else (f"{info.manufacturer} (TMN {info.tmn:03X})"
                         if info.manufacturer else "desconocido"))
    exp = EXPECTED.get(info.prefix)
    rep["expected"] = exp[0] if exp else ""
    rep["expected_user"] = exp[3] if exp else ""

    # 2. inventory
    tags = []
    for _ in range(6):
        tags = reader.scan(dwell=0.25, attempts=2)
        if tags:
            break
    if not tags:
        raise ReaderError("no tag detected")
    tag = tags[0]
    epc = tag.epc
    rep["epc"] = epc.hex().upper()
    rep["epc_len"] = len(epc)
    rep["rssi"] = tag.rssi
    rep["ant"] = tag.ant
    rep["type"] = f"0x{tag.type:02X}"

    # 3. EPC bank, checked against the inventoried EPC (the reader
    #    sometimes answers with the previous buffer)
    epc_data, epc_words = read_bank(reader, MEM_EPC, plan=((0, 8),))
    if epc and len(epc_data) < 4 + len(epc):
        epc_data, epc_words = read_bank(reader, MEM_EPC, plan=((0, 8),))
    rep["epc_bank"] = epc_data.hex().upper()
    rep["epc_bank_len"] = epc_words * 2
    if len(epc_data) >= 4:
        rep["crc"] = epc_data[0:2].hex().upper()
        rep["pc"] = epc_data[2:4].hex().upper()
        rep["epc_ok"] = ("si" if epc_data[4:4 + len(epc)].hex().upper()
                         == epc.hex().upper() else "NO (stale)")

    if not quick:
        # 4. the other banks
        res_data, res_words = read_bank(reader, MEM_RESERVED,
                                        plan=((0, 8),))
        rep["reserved"] = res_data.hex().upper()
        rep["reserved_len"] = res_words * 2
        usr_data, usr_words = read_bank(reader, MEM_USER,
                                        plan=((0, 2), (2, 2)))
        rep["user"] = usr_data.hex().upper()
        rep["user_len"] = usr_words * 2

        # 5. access password
        rep["pwd"] = ("00000000 ok" if read(reader, MEM_EPC, 0, 4)
                      else "00000000 RECHAZADO")
        rep["pwd_other"] = ("acepta cualquiera"
                            if read(reader, MEM_EPC, 0, 4,
                                    b"\x11\x22\x33\x44")
                            else "rechaza otros")
        if not full:
            rep["pwd_other"] = "-"
            rep["_"] = ""

    rep["elapsed"] = round(time.time() - t0, 2)
    return rep


def print_report(rep):
    print("=" * 64)
    print(f"EPC          : {rep['epc']}  ({rep['epc_len']} B)   "
          f"RSSI {rep['rssi']}  ant {rep['ant']}  type {rep['type']}")
    print(f"TID          : {rep['tid'] or '(no leido)'}")
    print(f"Chip         : {rep['chip']}   MDID {rep['mdid']}  "
          f"TMN {rep['tmn']}  prefix {rep['prefix'] or '-'}")
    if rep.get("expected"):
        print(f"Esperado     : {rep['expected']}  "
              f"(user {rep.get('expected_user','?')} B)")
    print(f"EPC bank     : {rep.get('crc','-')} {rep.get('pc','-')} | "
          f"{rep.get('epc_bank','')}  ({rep.get('epc_bank_len',0)} B)"
          + (f"  EPC {rep['epc_ok']}" if "epc_ok" in rep else ""))
    if "reserved_len" in rep:
        print(f"RESERVED     : {rep['reserved']}  "
              f"({rep['reserved_len']} B)")
        print(f"USER         : {rep['user']}  ({rep['user_len']} B)")
        print(f"Access pwd   : {rep['pwd']}  | otras: {rep['pwd_other']}")
    print(f"( {rep['elapsed']} s )")



def watch(reader, args):
    """Report every new tag that shows up, until interrupted."""
    seen = {}
    rows = []
    print("modo batch: poné los tags sobre la antena, Ctrl+C para salir")
    print("-" * 64)
    try:
        while True:
            try:
                tags = reader.scan(dwell=0.25, attempts=2)
            except ReaderError:
                continue
            for tag in tags:
                key = tag.epc_hex
                if key in seen:
                    seen[key]["rssi"] = tag.rssi
                    continue
                entry = {"epc": key, "epc_len": len(tag.epc),
                         "rssi": tag.rssi, "ant": tag.ant,
                         "type": f"0x{tag.type:02X}"}
                tid_data, tid_words = read_bank(reader, MEM_TID, plan=((0, 8),))
                info = decode_tid(tid_data)
                entry["tid"] = tid_data.hex().upper()
                entry["prefix"] = info.prefix
                entry["mdid"] = f"0x{info.mdid:03X}"
                entry["tmn"] = f"0x{info.tmn:03X}"
                entry["chip"] = (f"{info.manufacturer} {info.model}"
                                 if info.known else
                                 (f"{info.manufacturer} TMN {info.tmn:03X}"
                                  if info.manufacturer else "desconocido"))
                exp = EXPECTED.get(info.prefix)
                entry["expected"] = exp[0] if exp else ""
                seen[key] = entry
                rows.append(entry)
                print(f"EPC {key}  ({entry['epc_len']} B)  rssi {tag.rssi:3d}  "
                      f"{entry['chip']}  [{entry['prefix']}]  "
                      f"tid {entry['tid'][:24]}")
    except KeyboardInterrupt:
        print("\n" + "-" * 64)
    finally:
        reader.close()
    if args.csv and rows:
        exists = os.path.exists(args.csv)
        with open(args.csv, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            if not exists:
                w.writeheader()
            for row in rows:
                w.writerow(row)
        print(f"-> {len(rows)} tag(s) agregados a {args.csv}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="RU5600 tag tester")
    ap.add_argument("--csv", help="append a row per tag to this CSV")
    ap.add_argument("--quick", action="store_true",
                    help="skip the memory map and the password test")
    ap.add_argument("--full", action="store_true",
                    help="also test that a wrong password is refused")
    ap.add_argument("--watch", action="store_true",
                    help="keep scanning and report every new tag (Ctrl+C to "
                         "stop) - the fast way to test a batch")
    args = ap.parse_args()

    reader = Ru5600Reader()
    reader.open()
    if args.watch:
        return watch(reader, args)
    try:
        rep = test_tag(reader, args.quick, args.full)
    except ReaderError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        reader.close()

    print_report(rep)
    if args.csv:
        exists = os.path.exists(args.csv)
        with open(args.csv, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rep.keys()))
            if not exists:
                w.writeheader()
            w.writerow(rep)
        print(f"-> fila agregada a {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
