#!/usr/bin/env python3
"""Tag tester: one comparable report per tag.

Place one or more tags on the antenna and run it.  For every tag it
reports what the tag IS (chip model from the TID), what it HAS (memory
banks and their size) and how it is PROTECTED (access password).

    python3 tools/tag_test.py                 # all the tags it sees
    python3 tools/tag_test.py --watch         # keep scanning, one line per tag
    python3 tools/tag_test.py --csv tags.csv  # append a row per tag

Two things the reader does that this tool has to work around:

* it answers only about one command out of four, so every read is
  retried;
* it can answer a command with the response of a *previous* command of
  the same type, so a read is validated against the EPC the inventory
  reported and a stale answer is rejected and retried.
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

ZERO = b"\x00\x00\x00\x00"

# TID prefix -> (chip name, USER memory in BYTES) as published
# (the sources give the user memory in bits: 688 bits = 86 bytes, ...)
EXPECTED = {
    "E2803813": ("Alien Higgs-10 (H10/7320)", 4),
    "E2003412": ("Alien Higgs-3", 64),
    "E2003414": ("Alien Higgs-4", 16),
    "E2003411": ("Alien Higgs-2", 0),
    "E2803821": ("Alien Higgs-9", 86),
    "E2003811": ("Alien Higgs-EC", 16),
    "E2806894": ("NXP UCODE 8", 0),
    "E2806994": ("NXP UCODE 8M", 4),
    "E2806995": ("NXP UCODE 9", 0),
    "E2806915": ("NXP UCODE 9 (2024+)", 0),
    "E2806890": ("NXP UCODE 7", 0),
    "E2801100": ("Impinj Monza 4D", 4),
    "E2801114": ("Impinj Monza 4i", 60),
    "E280110C": ("Impinj Monza 4E", 16),
    "E2801130": ("Impinj Monza 5", 4),
    "E2801160": ("Impinj Monza R6", 0),
}


def read_robust(reader, bank, word, words, pwd=ZERO, tries=8):
    """A read that keeps trying for a while."""
    for _ in range(tries):
        try:
            return reader.read_card(bank, word, words, pwd, retries=1)
        except ReaderError:
            time.sleep(0.05)
    return b""


def read_tid(reader, tries=6):
    """The TID, read in one go (it is small)."""
    return read_robust(reader, MEM_TID, 0, 6, tries=tries)


def read_tid_stable(reader, rounds=3):
    """The TID read several times; the most repeated one wins.

    With more than one tag on the antenna the reader may point at a
    different tag between commands, so a single read is not trustworthy.
    """
    votes = {}
    for _ in range(rounds):
        tid = read_tid(reader, tries=4)
        # a TID always starts with the ISO class 0xE2; anything else is a
        # stale answer (the reader sometimes answers with the response of
        # a previous command, for example the EPC bank)
        if tid and len(tid) >= 4 and tid[0] == 0xE2:
            votes[tid] = votes.get(tid, 0) + 1
    if not votes:
        return b""
    return max(votes.items(), key=lambda kv: kv[1])[0]


def probe(reader, bank, pwd=ZERO):
    """How much of a bank answers (a lower bound: the reader is flaky)."""
    try:
        data, words = reader.probe_bank(bank, pwd, bank_budget=4.0,
                                        offset_budget=0.7)
    except ReaderError:
        return b"", 0
    return data, words


def describe(reader, epc: bytes, rssi: int, ant: int) -> dict:
    """Everything about one tag, with every read validated."""
    rep = {"epc": epc.hex().upper(), "epc_len": len(epc),
           "rssi": rssi, "ant": ant}

    # 1. the EPC bank, validated against the inventoried EPC: this is what
    #    tells us the reader is really pointing at THIS tag
    try:
        bank = reader.read_card_checked(MEM_EPC, 0, 8, ZERO, expect=epc,
                                       retries=12)
        rep["epc_bank"] = bank.hex().upper()
        rep["epc_crc"] = bank[0:2].hex().upper()
        rep["epc_pc"] = bank[2:4].hex().upper()
        rep["epc_ok"] = "si"
    except ReaderError as exc:
        bank = b""
        rep["epc_bank"] = ""
        rep["epc_ok"] = f"NO ({exc})"

    # 2. the TID of the same tag
    tid = read_tid_stable(reader)
    info = decode_tid(tid)
    rep["tid"] = tid.hex().upper()
    rep["tid_len"] = len(tid)
    rep["prefix"] = info.prefix
    rep["mdid"] = f"0x{info.mdid:03X}"
    rep["tmn"] = f"0x{info.tmn:03X}"
    rep["chip"] = (f"{info.manufacturer} {info.model}" if info.known
                   else (f"{info.manufacturer} (TMN {info.tmn:03X})"
                         if info.manufacturer else "desconocido"))
    exp = EXPECTED.get(info.prefix)
    rep["expected"] = exp[0] if exp else ""
    rep["expected_user"] = exp[1] if exp else ""

    # 3. the other banks
    res, res_words = probe(reader, MEM_RESERVED)
    rep["reserved"] = res.hex().upper()
    rep["reserved_len"] = res_words * 2
    usr, usr_words = probe(reader, MEM_USER)
    rep["user"] = usr.hex().upper()
    rep["user_len"] = usr_words * 2

    # 4. the access password
    rep["pwd"] = ("00000000 ok"
                  if read_robust(reader, MEM_EPC, 0, 4, ZERO, tries=6)
                  else "00000000 RECHAZADO")

    # 5. the verdict, without claiming a lock the reader cannot prove
    if rep["user_len"] == 0:
        rep["verdict"] = "USER no responde: bloqueado o inexistente"
    elif exp and rep["user_len"] >= exp[1]:
        rep["verdict"] = f"USER completo ({rep['user_len']} B) - sin lock"
    else:
        rep["verdict"] = (f"USER {rep['user_len']} B leidos (esperados "
                          f"{exp[1] if exp else '?'} B): lock parcial o "
                          f"limite del lector")
    return rep


def print_report(rep):
    print("=" * 66)
    print(f"EPC          : {rep['epc']}  ({rep['epc_len']} B)   "
          f"RSSI {rep['rssi']}  ant {rep['ant']}")
    print(f"TID          : {rep['tid'] or '(no leido)'}"
          + ("   <-- puede ser de otro tag" if rep.get("tags_seen", 1) > 1
             else ""))
    print(f"Chip         : {rep['chip']}   MDID {rep['mdid']}  "
          f"TMN {rep['tmn']}  prefix {rep['prefix'] or '-'}")
    if rep.get("expected"):
        print(f"Esperado     : {rep['expected']}  "
              f"(user {rep.get('expected_user','?')} B)")
    print(f"EPC bank     : {rep.get('epc_crc','-')} {rep.get('epc_pc','-')} | "
          f"{rep.get('epc_bank','') or '(no leido)'}   EPC {rep['epc_ok']}")
    if rep.get("tags_seen", 1) > 1:
        print("ATENCION     : el lector puede re-apuntar a otro tag entre "
              "comandos; el TID de arriba puede no ser de este EPC. "
              "Saca los otros tags para caracterizar bien este.")
    print(f"RESERVED     : {rep['reserved'] or '(no leido)'}  "
          f"({rep['reserved_len']} B leidos)")
    print(f"USER         : {rep['user'] or '(no leido)'}  "
          f"({rep['user_len']} B leidos)")
    print(f"Access pwd   : {rep['pwd']}")
    print(f"Veredicto    : {rep['verdict']}")
    if rep.get("tags_seen", 1) > 1:
        print(f"ATENCION     : hay {rep['tags_seen']} tags en la antena; el "
              f"lector puede apuntar a otro entre comando y comando. "
              f"Saca los otros para caracterizar bien este.")


def scan_tags(reader, attempts=4):
    """The distinct tags the reader sees, with their best RSSI."""
    best = {}
    for _ in range(attempts):
        try:
            tags = reader.scan(dwell=0.3, attempts=2)
        except ReaderError:
            continue
        for t in tags:
            key = t.epc_hex
            if key not in best or t.rssi > best[key][0]:
                best[key] = (t.rssi, t.ant)
        if len(best) >= 2:
            break
    return best


def main():
    ap = argparse.ArgumentParser(description="RU5600 tag tester")
    ap.add_argument("--csv", help="append a row per tag to this CSV")
    ap.add_argument("--watch", action="store_true",
                    help="keep scanning and report every new tag")
    args = ap.parse_args()

    reader = Ru5600Reader()
    reader.open()
    rows = []
    try:
        if args.watch:
            seen = set()
            print("modo batch: poné los tags sobre la antena, Ctrl+C para salir")
            print("-" * 66)
            while True:
                try:
                    tags = reader.scan(dwell=0.3, attempts=2)
                except ReaderError:
                    continue
                for t in tags:
                    if t.epc_hex in seen:
                        continue
                    seen.add(t.epc_hex)
                    rep = describe(reader, t.epc, t.rssi, t.ant)
                    rows.append(rep)
                    print(f"EPC {rep['epc']}  {rep['chip']}  [{rep['prefix']}]  "
                          f"user {rep['user_len']}B  pwd {rep['pwd']}")
        else:
            found = scan_tags(reader)
            if not found:
                print("no se detecto ningun tag", file=sys.stderr)
                return 1
            for epc, (rssi, ant) in found.items():
                rep = describe(reader, bytes.fromhex(epc), rssi, ant)
                rows.append(rep)
                print_report(rep)
    except KeyboardInterrupt:
        pass
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


if __name__ == "__main__":
    raise SystemExit(main())
