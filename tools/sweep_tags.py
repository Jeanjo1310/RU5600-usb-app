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


KNOWN_PREFIXES = ("E2803821", "E2803813", "E2801105", "E2806894", "E2806915",
                  "E2003412")


def main():
    ap = argparse.ArgumentParser(description="RU5600 persistent tag sweep")
    ap.add_argument("--target", type=int, default=10)
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--continuous", action="store_true",
                    help="no para al llegar al target: escucha como la antena")
    args = ap.parse_args()

    reader = Ru5600Reader()
    reader.open()
    try:
        t0 = time.monotonic()
        tags = reader.sweep(duration=args.seconds, target_count=args.target,
                            stop_on_target=not args.continuous)
        dt = time.monotonic() - t0
    finally:
        reader.close()

    idx = None
    try:
        # tools/ -> ru5600/tools, excel lives at <project>/CONOCIMIENTO/
        xl = os.path.normpath(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..", "CONOCIMIENTO", "TAGS EPC.xlsx"))
        if os.path.exists(xl):
            idx = load_excel(xl)
    except Exception as exc:  # noqa: BLE001 - excel is a bonus, not the read
        print(f"excel no cargado: {exc}")

    print(f"barrido {dt:.0f}s: {len(tags)} lecturas")
    # colision: mismo nucleo EPC+TID (24B) con colas distintas = doble
    # lectura del mismo tag o tags gemelos (mismo EPC grabado). No se
    # fusiona ni se duplica: se marca para verificar por TID directo.
    groups = {}
    for tag in tags:
        blob = bytes(tag.raw_id) if tag.raw_id else bytes(tag.epc)
        groups.setdefault(blob[:24].hex().upper(), []).append(tag)
    print(f"grupos por nucleo: {len(groups)}")
    n = 0
    for core, members in groups.items():
        flag = "" if len(members) == 1 else f" <- COLISION x{len(members)}: doble lectura o gemelos, verificar TID directo"
        n += 1
        tag = members[0]
        blob = (bytes(tag.raw_id) if tag.raw_id else bytes(tag.epc))
        bhe = blob.hex().upper()
        epc, chip, xl, conf = "?", "interpreter pending", "-", ""
        # chip-primero de verdad: TID directo (votos) + el TID tiene que
        # estar DENTRO del blob (dos fuentes independientes coinciden).
        # Sin eso no hay carve, solo crudo.
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
            # patron de alta probabilidad (Creador 2026-10-26): layout A/B
            # + prefijo TID en DB con evidencia. Se informa como patron,
            # el excel como tercera fuente lo respalda o lo tumba.
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
        print(f"#{n} epc={epc[:32] if len(epc) > 32 else epc} "
              f"chip={chip} excel={xl} {conf}{flag}")
        print(f"   blob {len(bhe) // 2}B {bhe[:64]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
