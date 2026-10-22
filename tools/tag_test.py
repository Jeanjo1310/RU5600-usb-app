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
    epc_crc_ok,
    MEM_RESERVED,
    MEM_TID,
    MEM_USER,
    ReaderError,
    Ru5600Reader,
)
from ru5600_chip import (  # noqa: E402
    TID_PREFIX_DB,
    consistency_check,
    decode_tid,
)

ZERO = b"\x00\x00\x00\x00"

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
    """How much of a bank belongs to this tag, and why it stopped.

    With a single tag in the field the reader can only be pointing at
    it, so no guard is needed.  With several tags the reader re-targets
    between commands and a read can come from another tag: then the
    target is checked before and after the sweep and the result is
    discarded if the reader moved.

    The reason matters: "the tag does not have that memory" is a fact
    about the tag, while "the reader did not answer" says nothing about
    the tag at all.
    """
    if guard:
        # the guard is checked before and after the sweep, not on every
        # chunk: the reader refuses most commands and each check is one
        if reader.current_epc_quick() != target:
            return b"", 0, "moved"
    try:
        data, words, reason = reader.probe_bank(bank, pwd)
    except ReaderError:
        return b"", 0, "no_answer"
    if guard and reader.current_epc_quick() != target:
        return b"", 0, "moved"
    if stale and data.hex().upper() == stale.upper():
        return b"", 0, "stale"
    return data, words, reason


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
    # the reader computes the CRC of the EPC it read: if it does not
    # add up, the answer was not fresh
    rep["epc_crc_ok"] = epc_crc_ok(cur, crc)
    # the PC carries the length of the EPC in words, in bits 11-14
    rep["epc_len_ok"] = ((int.from_bytes(pc, "big") >> 11) & 0x0F) * 2 == len(cur)

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
    entry = TID_PREFIX_DB.get(info.prefix)
    exp_user = None
    if entry:
        rep["expected"] = f"{entry[0]} {entry[1]}"
        if str(entry[3]).isdigit():
            exp_user = int(entry[3]) // 8
            rep["expected_user"] = exp_user

    # the other banks, guarded as well
    guard = n_tags > 1
    res, res_words, res_reason = probe_for_tag(reader, target, MEM_RESERVED,
                                               stale=rep["epc_bank"],
                                               guard=guard)
    rep["reserved"] = res.hex().upper()
    rep["reserved_len"] = res_words * 2
    rep["reserved_reason"] = res_reason
    usr, usr_words, usr_reason = probe_for_tag(reader, target, MEM_USER,
                                               stale=rep["epc_bank"],
                                               guard=guard)
    rep["user"] = usr.hex().upper()
    rep["user_len"] = usr_words * 2
    rep["user_reason"] = usr_reason

    # the identification has to agree with what the memory measured: if
    # it does not, the TID and the memory are not from the same tag
    rep["warnings"] = consistency_check(info, rep["user_len"])

    # the access password: the EPC bank read already used it, so a
    # successful read means the password is accepted (no extra command)
    rep["pwd"] = "00000000 ok" if rep.get("epc_bank") else "00000000 RECHAZADO"

    # The verdict, without claiming a lock the reader cannot prove.
    # The size the reader reports moves by a block (4 words = 8 bytes)
    # from one run to the next, so the comparison has that tolerance.
    if rep["user_len"] == 0:
        if usr_reason == "no_answer":
            rep["verdict"] = ("el lector no esta respondiendo (se queda "
                              "mudo despues de muchas ordenes): reinicia "
                              "el lector y proba de nuevo, no se puede decir "
                              "si el banco esta bloqueado")
        elif usr_reason == "moved":
            rep["verdict"] = "el lector apuntaba a otro tag: no se midio"
        else:
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
    print(f"#{rep.get('id', 1):<3} EPC       : {rep['epc']}  "
          f"({rep['epc_len']} B)   RSSI {rep['rssi']}  ant {rep['ant']}  "
          f"leido {rep.get('veces', 1)} vez/veces")
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
    print(f"EPC CRC      : {'OK (el CRC del lector coincide con el EPC)' if rep.get('epc_crc_ok') else 'NO COINCIDE: la respuesta no era fresca'}"
          f"   longitud {rep.get('epc_len_ok')}")
    print(f"RESERVED     : {rep['reserved'] or '(no leido)'}  "
          f"({rep['reserved_len']} B leidos)")
    print(f"USER         : {rep['user'] or '(no leido)'}  "
          f"({rep['user_len']} B leidos)")
    print(f"Access pwd   : {rep['pwd']}")
    print(f"Veredicto    : {rep['verdict']}")
    for w in rep.get("warnings", []):
        print(f"AVISO        : {w} - el TID y la memoria no son del mismo "
              f"tag, la identificacion no es de fiar")
    if rep.get("tags_seen", 1) > 1:
        print(f"NOTA         : hay {rep['tags_seen']} tags en la antena; cada "
              f"lectura se valida contra el tag, pero con el lector "
              f"re-apuntando entre comandos puede quedar sin datos.")


def scan_tags(reader, attempts=4):
    """The distinct tags the reader reports.

    Returns ``{epc: (rssi, antenna, times_read)}``: a tag that is
    reported again does not become a new record, it only gets its count
    higher (the same as the ReaderSoft demo does).
    """
    best = {}
    for _ in range(attempts):
        try:
            tags = reader.scan(dwell=0.3, attempts=2)
        except ReaderError:
            continue
        for t in tags:
            key = t.epc_hex
            if key not in best:
                best[key] = (t.rssi, t.ant, 1)
            else:
                rssi, ant, n = best[key]
                best[key] = (max(rssi, t.rssi), ant, n + 1)
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
            order = []
            counts = {}
            print("modo batch: poné los tags sobre la antena, Ctrl+C para salir")
            print("-" * 66)
            while True:
                try:
                    tags = reader.scan(dwell=0.3, attempts=2)
                except ReaderError:
                    continue
                for t in tags:
                    key = t.epc_hex
                    counts[key] = counts.get(key, 0) + 1
                    if key in seen:
                        # a tag that was already read does not get a new
                        # record: it keeps its number and its count
                        continue
                    seen.add(key)
                    order.append(key)
                    rep = describe(reader, t.epc, t.rssi, t.ant,
                                   n_tags=len(seen))
                    rep["id"] = len(order)
                    rep["veces"] = counts[key]
                    rows.append(rep)
                    print(f"#{rep['id']:<3} EPC {rep['epc']}  "
                          f"{rep.get('chip','?')}  "
                          f"[{rep.get('prefix','?')}]  "
                          f"user {rep.get('user_len',0)}B  "
                          f"pwd {rep.get('pwd','?')}  "
                          f"veces {rep['veces']}  "
                          f"({rep.get('estado','?')})")
        else:
            # Characterize the tags the reader points at, one at a time.
            # With several tags in the field the reader re-targets between
            # commands, so the tag is only characterized when a memory
            # read confirms that the reader is really pointing at it.
            done = set()
            order = []
            counts = {}
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline and len(done) < args.max_tags:
                found = scan_tags(reader, attempts=2)
                if not found:
                    continue
                for epc, (rssi, ant, times) in found.items():
                    counts[epc] = counts.get(epc, 0) + times
                    if epc in done:
                        # a tag that was already read does not get a new
                        # record: it keeps its number and its count
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
                        order.append(epc)
                        rep["id"] = len(order)
                        rep["veces"] = counts[epc]
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
