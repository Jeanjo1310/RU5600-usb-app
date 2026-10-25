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
    args = ap.parse_args()

    reader = Ru5600Reader()
    reader.open()
    try:
        t0 = time.monotonic()
        tags = reader.sweep(duration=args.seconds, target_count=args.target)
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

    print(f"barrido {dt:.0f}s: {len(tags)} tags distintos")
    for i, tag in enumerate(tags, 1):
        blob = (bytes(tag.raw_id) if tag.raw_id else bytes(tag.epc)).hex().upper()
        epc, chip = "", "interpreter pending"
        for prefix in KNOWN_PREFIXES:
            at = blob.find(prefix)
            if at >= 0:
                e, t, pending = interpret_active_blob(
                    bytes.fromhex(blob), prefix)
                if pending is None:
                    epc, chip = e.hex().upper(), prefix
                    break
        if not epc:
            # H9-style carve attempt only as hypothesis display, not claim
            epc = f"BLOB {len(blob) // 2}B"
        xl_hit = idx.get(epc, "NO ESTA (nuevo)") if idx is not None and not epc.startswith("BLOB") else "-"
        print(f"#{i} epc={epc[:32]} chip={chip} excel={xl_hit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
