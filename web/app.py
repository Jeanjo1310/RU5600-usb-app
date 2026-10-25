"""Barrido web P2 — piloto Flask + Flowbite/Tailwind (Creador 2026-10-26).

Reutiliza ru5600_core tal cual (USB abre en el servidor, misma maquina).
Sin npm por ahora: Tailwind Play CDN + Flowbite CDN (piloto dev;
produccion compila con @tailwindcss/cli segun la guia oficial).
"""
from __future__ import annotations

import os
import sys
import threading
import time

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "src"))

from ru5600_chip import TID_PREFIX_DB  # noqa: E402
from ru5600_core import Ru5600Reader, interpret_active_blob  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
EXCEL = os.path.normpath(os.path.join(BASE, "..", "..", "CONOCIMIENTO",
                                      "TAGS EPC.xlsx"))

app = Flask(__name__)
_reader: Ru5600Reader | None = None
_lock = threading.Lock()
_stop = threading.Event()
_state = {"running": False, "tags": [], "seen": set(), "started": 0.0,
          "error": ""}


def _excel():
    import openpyxl
    wb = openpyxl.load_workbook(EXCEL, data_only=True)
    idx, codes = {}, set()
    for name in wb.sheetnames:
        if name == "PRUEBAS":
            continue
        ws = wb[name]
        for rr in range(2, ws.max_row + 1):
            b = ws.cell(row=rr, column=2).value
            a = ws.cell(row=rr, column=1).value
            c = ws.cell(row=rr, column=3).value
            if b:
                idx[str(b).replace(" ", "").upper()] = (name, rr, a)
            if c:
                codes.add(str(c))
    return idx, codes


def _resolve(blob: bytes, idx, codes):
    """EPC + chip + excel. Devuelve dict listo para JSON."""
    bhe = blob.hex().upper()
    out = {"epc": "?", "chip": "interpreter pending", "xl": "-",
           "conf": "", "code": ""}
    # patron conocido: layout B o A + prefijo DB
    cand, tp = None, None
    if len(blob) >= 24 and blob[0] == 0xE2 and blob[12] == 0xE2:
        cand, tp = blob[0:12], blob[12:24]
    elif (len(blob) >= 36 and blob[0] == 0xE2 and blob[24] == 0xE2
            and all(b == 0 for b in blob[12:24])):
        cand, tp = blob[0:12], blob[24:36]
    if cand is not None and tp.hex().upper()[:8] in TID_PREFIX_DB:
        out.update(epc=cand.hex().upper(),
                   chip=tp.hex().upper()[:8] + " (patron)", conf="patron")
    elif len(blob) >= 12:
        e = blob[0:12].hex().upper()
        hit = idx.get(e) if idx is not None else None
        if hit is not None:
            out.update(epc=e, chip="desconocido (excel)",
                       xl=f"{hit[0]} #{hit[1]}", conf="excel")
        else:
            out.update(epc=e, chip="desconocido (candidato)",
                       conf="candidato")
    if out["epc"] != "?":
        out["code"] = out["epc"][6:14]
        if idx is not None:
            hit = idx.get(out["epc"])
            out["xl"] = (f"{hit[0]} #{hit[1]}" if hit
                         else "NO ESTA (nuevo)")
            if not hit and out["code"] in codes:
                out["xl"] = f"code {out['code']} DUPLICADO"
    return out


def _get_reader():
    global _reader
    if _reader is None or not _reader.is_open:
        _reader = Ru5600Reader()
        _reader.open()
    return _reader


@app.route("/")
def index():
    return render_template("barrido.html")


@app.route("/api/status")
def status():
    return jsonify(running=_state["running"], error=_state["error"],
                   count=len(_state["tags"]), tags=_state["tags"],
                   elapsed=round(time.monotonic() - _state["started"], 1)
                   if _state["running"] else 0)


@app.route("/api/start", methods=["POST"])
def start():
    seconds = float((request.get_json(silent=True) or {}).get("seconds", 60))
    with _lock:
        if _state["running"]:
            return jsonify(ok=False, error="ya corriendo"), 409
        _state.update(running=True, tags=[], error="",
                      started=time.monotonic())
        _state["seen"] = set()
    _stop.clear()
    threading.Thread(target=_run, args=(seconds,), daemon=True).start()
    return jsonify(ok=True)


@app.route("/api/stop", methods=["POST"])
def stop():
    _stop.set()
    return jsonify(ok=True)


def _run(seconds):
    try:
        idx, codes = _excel()
    except Exception:  # noqa: BLE001 - excel es bonus
        idx, codes = None, set()
    try:
        r = _get_reader()
        from ru5600_core import DEV_ADDR, PARAM_WORKMODE, WORKMODE_ACTIVE
        r._pace()
        r._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                       WORKMODE_ACTIVE)
        r._pace(settle=True)
        r._lib.CFHid_ClearTagBuf()
        r._pace()
        r._lib.CFHid_StartRead(DEV_ADDR)
        r._pace(settle=True)
        deadline = None if seconds <= 0 else time.monotonic() + seconds
        try:
            while ((deadline is None or time.monotonic() < deadline)
                    and not _stop.is_set()):
                time.sleep(0.1)
                for tag in r.poll_tags():
                    blob = (bytes(tag.raw_id) if tag.raw_id
                            else bytes(tag.epc))
                    key = blob.hex().upper()
                    if key in _state["seen"]:
                        continue
                    _state["seen"].add(key)
                    info = _resolve(blob, idx, codes)
                    info.update(raw=key[:48],
                                ant=tag.ant, rssi=tag.rssi)
                    _state["tags"].append(info)
        finally:
            try:
                r._lib.CFHid_StopRead(DEV_ADDR)
            except Exception:  # noqa: BLE001
                pass
            r._pace(settle=True)
    except Exception as exc:  # noqa: BLE001
        _state["error"] = str(exc)
    finally:
        _state["running"] = False


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8321, threaded=True)
