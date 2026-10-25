#!/usr/bin/env python3
"""Persistent multi-tag sweep: streams until N distinct tags or timeout.

    python3 tools/sweep_tags.py --target 10 --seconds 90

Vendor-faithful: raw blob per tag (exact on any antenna), dedup by blob,
EPC carved only where a chip interpreter exists (else blob shown +
interpreter pending). Crosses carved EPCs against TAGS EPC.xlsx.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from ru5600_core import Ru5600Reader, interpret_active_blob  # noqa: E402


def load_excel(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    idx = {}
    for name in wb.sheetnames:
        if name == "PRUEBAS":
            continue
        ws = wb[name]
        for rr in range(2, ws.max_row + 1):
            b = ws.cell(row=rr, column=2).value
            a = ws.cell(row=rr, column=1).value
            if b:
                idx[str(b).replace(" ", "").upper()] = (name, rr, a)
    return idx


def resolve_group(reader, blob, bhe, idx):
    """Dato completo de un grupo: TID directo + carve + excel.

    Devuelve (epc, chip, xl, conf). conf = confirmado | patron | "".
    """
    from ru5600_core import interpret_active_blob
    epc, chip, xl, conf = "?", "interpreter pending", "-", ""
    votes = {}
    for _ in range(3):
        try:
            t = reader.read_card(2, 0, 6, b"\x00" * 4, retries=1)
            if t and len(t) >= 4 and t[0] == 0xE2:
                votes[t] = votes.get(t, 0) + 1
        except Exception:  # noqa: BLE001 - lector intermitente
            pass
    if votes:
        tid = max(votes.items(), key=lambda kv: kv[1])[0]
        the = tid.hex().upper()
        if the in bhe:
            prefix = the[:8]
            e, _, pending = interpret_active_blob(blob, prefix)
            if pending is None:
                epc = e.hex().upper()
                chip = prefix
                conf = "confirmado"
                if idx is not None:
                    xl = idx.get(epc, "NO ESTA (nuevo)")
    if not conf:
        from ru5600_chip import TID_PREFIX_DB
        cand, tp = None, None
        if (len(blob) >= 24 and blob[0] == 0xE2 and blob[12] == 0xE2):
            cand, tp = blob[0:12], blob[12:24]      # layout B
        elif (len(blob) >= 36 and blob[0] == 0xE2 and blob[24] == 0xE2
                and all(b == 0 for b in blob[12:24])):
            cand, tp = blob[0:12], blob[24:36]      # layout A
        if cand is not None and tp.hex().upper()[:8] in TID_PREFIX_DB:
            epc = cand.hex().upper()
            chip = tp.hex().upper()[:8] + " (patron)"
            conf = "patron"
            if idx is not None:
                xl = idx.get(epc, "NO ESTA (nuevo)")
    return epc, chip, xl, conf


def main():
    ap = argparse.ArgumentParser(description="RU5600 persistent tag sweep")
    ap.add_argument("--target", type=int, default=0,
                    help="para cuando N grupos tengan dato completo (0 = sin meta)")
    ap.add_argument("--seconds", type=float, default=120.0,
                    help="presupuesto total; 0 = hasta Ctrl+C (orden de parar)")
    ap.add_argument("--round", type=float, default=15.0,
                    help="streaming por vuelta antes de resolver")
    args = ap.parse_args()

    reader = Ru5600Reader()
    reader.open()
    meta = f"/{args.target}" if args.target else ""
    idx = None
    try:
        xl = os.path.normpath(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..", "CONOCIMIENTO", "TAGS EPC.xlsx"))
        if os.path.exists(xl):
            idx = load_excel(xl)
    except Exception as exc:  # noqa: BLE001 - excel is a bonus
        print(f"excel no cargado: {exc}")

    groups, t0 = {}, time.monotonic()
    dead_rounds = 0
    try:
        while True:
            if args.seconds and time.monotonic() - t0 >= args.seconds:
                print("presupuesto agotado");
                break
            fresh = reader.sweep(
                duration=args.round,
                on_new=lambda tag, n: print(
                    f"  stream #{n}{meta} "
                    f"{(bytes(tag.raw_id) if tag.raw_id else bytes(tag.epc)).hex().upper()[:24]}",
                    flush=True))
            for tag in fresh:
                blob = bytes(tag.raw_id) if tag.raw_id else bytes(tag.epc)
                key = blob[:24].hex().upper()
                groups.setdefault(key, {"blob": blob, "n": 0,
                                         "epc": "?", "chip": "interpreter pending",
                                         "xl": "-", "conf": ""})
                groups[key]["n"] += 1
            for key, g in groups.items():
                if not g["conf"]:
                    g["epc"], g["chip"], g["xl"], g["conf"] = resolve_group(
                        reader, g["blob"], g["blob"].hex().upper(), idx)
            done = sum(1 for g in groups.values() if g["conf"])
            print(f"vuelta: {len(groups)} grupos, {done} con dato completo{meta}",
                  flush=True)
            if not fresh:
                dead_rounds += 1
            else:
                dead_rounds = 0
            if dead_rounds >= 3:
                print("lector mudo 3 vueltas: reboot + reintentar")
                break
            if args.target and done >= args.target:
                print(f"listo: {done} tags con dato completo")
                break
    finally:
        reader.close()

    n = 0
    for key, g in groups.items():
        n += 1
        bhe = g["blob"].hex().upper()
        flag = "" if g["n"] == 1 else (
            f" <- COLISION x{g['n']}: doble lectura o gemelos")
        print(f"#{n} epc={g['epc'][:32] if len(g['epc']) > 32 else g['epc']} "
              f"chip={g['chip']} excel={g['xl']} {g['conf']}{flag}")
        print(f"   blob {len(bhe) // 2}B {bhe[:64]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
