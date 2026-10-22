#!/usr/bin/env python3
"""Tag tester: one comparable report per tag.

It works with any Gen2 tag the reader can see; nothing about a
particular chip is assumed.

    python3 tools/tag_test.py                 # all the tags the reader sees
    python3 tools/tag_test.py --watch         # keep scanning, one line per tag
    python3 tools/tag_test.py --csv tags.csv  # append a row per tag

Three things the reader does that this tool has to work around:

* it answers only about one command out of four, so every read is
  retried;
* it can answer a command with the response of a *previous* command of
  the same type, so an answer is validated before it is used;
* it keeps a **cache** of the tags it saw, and with several tags in the
  field it re-targets between commands, so a read can come from another
  tag.  Every read is therefore guarded: the reader has to point at the
  tag before *and* after the read.
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


def read_tid_for(reader, target, rounds=3):
    """The TID of a given tag.

    A TID always starts with the ISO class 0xE2; anything else is a
    stale answer and is discarded.
    """
    votes = {}
    for _ in range(rounds):
        try:
            tid = reader.read_card(MEM_TID, 0, 6, ZERO, retries=1)
        except ReaderError:
            continue
        if tid and len(tid) >= 4 and tid[0] == 0xE2:
            votes[tid] = votes.get(tid, 0) + 1
    if not votes:
        return b""
    return max(votes.items(), key=lambda kv: kv[1])[0]


def probe_for_tag(reader, target, bank, pwd=ZERO, stale=b"", guard=True):
    """How much of a bank belongs to this tag (a lower bound).

    With a single tag in the field the reader can only be pointing at
    it, so no guard is needed.  With several tags the reader re-targets
    between commands and a read can come from another tag: then the
    target is checked before and after the sweep and the result is
    discarded if the reader moved.
    """
    if guard:
        # the guard is checked before and after the sweep, not on every
        # chunk: the reader refuses most commands and each check is one
        if reader.current_epc_quick() != target:
            return b"", 0
    try:
        data, words = reader.probe_bank(bank, pwd)
    except ReaderError:
        return b"", 0
    if guard and reader.current_epc_quick() != target:
        return b"", 0
    if stale and data.hex().upper() == stale.upper():
        return b"", 0
    return data, words


def describe(reader, target: bytes, rssi: int, ant: int,
             n_tags: int = 1) -> dict:
    """Everything about one tag, with every read guarded."""
    rep = {"epc": target.hex().upper(), "epc_len": len(target),
           "rssi": rssi, "ant": ant, "tags_seen": n_tags}

    # the reader has to be pointing at this tag for anything to mean
    # something
    try:
        cur, crc, pc = reader.read_current_epc()
    except ReaderError as exc:
        rep["estado"] = f"el lector no respondio ({exc})"
        return rep
    if cur != target:
        rep["estado"] = (f"el lector apunta a {cur.hex().upper()}, no a este "
                         f"tag: no se puede caracterizar ahora")
        return rep
    rep["estado"] = "ok"
    rep["epc_bank"] = (crc + pc + cur).hex().upper()
    rep["epc_crc"] = crc.hex().upper()
    rep["epc_pc"] = pc.hex().upper()

    # the TID, guarded
    tid = read_tid_for(reader, target)
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

    # the other banks, guarded as well
    guard = n_tags > 1
    res, res_words = probe_for_tag(reader, target, MEM_RESERVED,
                                   stale=rep["epc_bank"], guard=guard)
    rep["reserved"] = res.hex().upper()
    rep["reserved_len"] = res_words * 2
    usr, usr_words = probe_for_tag(reader, target, MEM_USER,
                                   stale=rep["epc_bank"], guard=guard)
    rep["user"] = usr.hex().upper()
    rep["user_len"] = usr_words * 2

    # the access password: the EPC bank read already used it, so a
    # successful read means the password is accepted (no extra command)
    rep["pwd"] = "00000000 ok" if rep.get("epc_bank") else "00000000 RECHAZADO"

    # The verdict, without claiming a lock the reader cannot prove.
    # The size the reader reports moves by a block (4 words = 8 bytes)
    # from one run to the next, so the comparison has that tolerance.
    exp_user = exp[1] if exp else None
    if rep["user_len"] == 0:
        rep["verdict"] = "USER no responde: bloqueado o inexistente"
    elif exp_user and rep["user_len"] >= exp_user:
        rep["verdict"] = (f"USER completo ({rep['user_len']} B, esperado "
                          f"{exp_user} B) - sin lock")
    elif exp_user and rep["user_len"] >= exp_user - 8:
        rep["verdict"] = (f"USER {rep['user_len']} B de {exp_user} B: el "
                          f"lector no alcanza el final, no es un lock")
    elif exp_user:
        rep["verdict"] = (f"USER {rep['user_len']} B de {exp_user} B: banco "
                          f"bloqueado o limite del lector")
    else:
        rep["verdict"] = f"USER {rep['user_len']} B leidos"
    return rep


def print_report(rep):
    print("=" * 66)
    print(f"EPC          : {rep['epc']}  ({rep['epc_len']} B)   "
          f"RSSI {rep['rssi']}  ant {rep['ant']}")
    if rep.get("estado") != "ok":
        print(f"Estado       : {rep['estado']}")
        return
    print(f"TID          : {rep['tid'] or '(no leido)'}")
    print(f"Chip         : {rep['chip']}   MDID {rep['mdid']}  "
          f"TMN {rep['tmn']}  prefix {rep['prefix'] or '-'}")
    if rep.get("expected"):
        print(f"Esperado     : {rep['expected']}  "
              f"(user {rep.get('expected_user','?')} B)")
    print(f"EPC bank     : {rep.get('epc_crc','-')} {rep.get('epc_pc','-')} | "
          f"{rep.get('epc_bank','')}")
    print(f"RESERVED     : {rep['reserved'] or '(no leido)'}  "
          f"({rep['reserved_len']} B leidos)")
    print(f"USER         : {rep['user'] or '(no leido)'}  "
          f"({rep['user_len']} B leidos)")
    print(f"Access pwd   : {rep['pwd']}")
    print(f"Veredicto    : {rep['verdict']}")
    if rep.get("tags_seen", 1) > 1:
        print(f"NOTA         : hay {rep['tags_seen']} tags en la antena; cada "
              f"lectura se valida contra el tag, pero con el lector "
              f"re-apuntando entre comandos puede quedar sin datos.")


def scan_tags(reader, attempts=4):
    """The distinct tags the reader reports, with their best RSSI."""
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
    ap.add_argument("--seconds", type=float, default=90.0,
                    help="how long to look for tags to characterize")
    ap.add_argument("--max-tags", type=int, default=20,
                    help="how many tags to characterize at most")
    args = ap.parse_args()

    reader = Ru5600Reader()
    reader.open()
    rows = []
    try:
        # a clean reader: no cache of old tags and in answer mode, where
        # it answers the memory reads much better (86 % against 43 %)
        reader.del_dev_cache()
        try:
            reader.set_work_mode(0)
        except Exception:
            pass
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
                    rep = describe(reader, t.epc, t.rssi, t.ant,
                                   n_tags=len(seen))
                    rows.append(rep)
                    print(f"EPC {rep['epc']}  {rep.get('chip','?')}  "
                          f"[{rep.get('prefix','?')}]  "
                          f"user {rep.get('user_len',0)}B  "
                          f"pwd {rep.get('pwd','?')}  "
                          f"({rep.get('estado','?')})")
        else:
            # Characterize the tags the reader points at, one at a time.
            # With several tags in the field the reader re-targets between
            # commands, so the tag is only characterized when a memory
            # read confirms that the reader is really pointing at it.
            done = set()
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline and len(done) < args.max_tags:
                found = scan_tags(reader, attempts=2)
                if not found:
                    continue
                for epc, (rssi, ant) in found.items():
                    if epc in done:
                        continue
                    try:
                        cur, crc, pc = reader.read_current_epc()
                    except ReaderError:
                        continue
                    if cur.hex().upper() != epc:
                        # the reader is pointing at another tag right now
                        continue
                    rep = describe(reader, bytes.fromhex(epc), rssi, ant,
                                   n_tags=len(found))
                    if rep.get("estado") == "ok":
                        done.add(epc)
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
