"""Barrido web P2 — piloto Flask + Flowbite/Tailwind (Creador 2026-10-26).

Reutiliza ru5600_core tal cual (USB abre en el servidor, misma maquina).
Sin npm por ahora: Tailwind Play CDN + Flowbite CDN (piloto dev;
produccion compila con @tailwindcss/cli segun la guia oficial).
"""
from __future__ import annotations

import collections
import io
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from flask import (Flask, jsonify, render_template, request,
                    send_file, redirect)

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "src"))
sys.path.insert(0, BASE)

from ru5600_chip import TID_PREFIX_DB  # noqa: E402
from ru5600_core import Ru5600Reader, interpret_active_blob, ReaderError  # noqa: E402
import ru5600_store as store  # noqa: E402

EXCEL = os.path.normpath(os.path.join(BASE, "..", "..", "CONOCIMIENTO",
                                      "TAGS EPC.xlsx"))

_pool = ThreadPoolExecutor(max_workers=8)

# --- log de eventos del lector (ring buffer) ---
_log_ring = collections.deque(maxlen=1500)


def _log(level, msg):
    """Registra un evento del lector: info | ok | warn | error | tx."""
    _log_ring.append({"t": time.strftime("%H:%M:%S"), "ts": time.time(),
                      "level": level, "msg": str(msg)[:400]})



def _guarded(fn, timeout=40):
    """Hardware con techo: en vez de colgar el navegador, error util."""
    fut = _pool.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except TimeoutError:
        raise RuntimeError("timeout: el lector no responde, reboot + reintentar")


app = Flask(__name__)
try:
    store.init_db()
except Exception as exc:  # noqa: BLE001 - la DB es aparte del lector
    print("ru5600_store: init_db fallo: %s" % exc, file=sys.stderr)
_reader: Ru5600Reader | None = None
_net_reader = None
_net_info: dict = {"mode": "usb"}
_lock = threading.Lock()
_stop = threading.Event()
_state = {"running": False, "tags": [], "seen": set(), "started": 0.0,
          "error": ""}


def _usb_nodes():
    found = []
    base = "/sys/bus/usb/devices"
    try:
        for node in sorted(os.listdir(base)):
            try:
                vid = open(os.path.join(base, node, "idVendor")).read().strip()
                pid = open(os.path.join(base, node, "idProduct")).read().strip()
            except OSError:
                continue
            if vid.lower() == "1a86" and pid.lower() == "e010":
                found.append(f"1a86:e010 ({node})")
    except OSError:
        pass
    return found


@app.route("/conexion")
def conexion():
    return render_template("conexion.html")


@app.route("/api/conn_state")
def conn_state():
    global _reader, _net_reader
    if _reader is not None and _reader.is_open:
        try:
            si = _reader.system_info()
            info = {"sn": si.device_sn, "fw": si.soft_ver,
                    "hw": si.hard_ver}
        except Exception:  # noqa: BLE001
            info = {}
        return jsonify(open=True, mode="usb", info=info)
    if _net_reader is not None and _net_reader.is_open():
        return jsonify(open=True, mode="net", info={})
    return jsonify(open=False, mode=_net_info.get("mode", "usb"), info={})


def _need_usb():
    if _reader is None or not _reader.is_open:
        raise RuntimeError("Abrí la conexión primero (pestaña Conexión).")
    return _reader


@app.route("/parametros")
def parametros():
    return render_template("parametros.html")


@app.route("/leer")
def leer():
    return render_template("leer.html")


@app.route("/chip")
def chip():
    return render_template("chip.html")


@app.route("/avanzado")
def avanzado():
    # la frecuencia y el reinicio se movieron a Parametros
    return redirect("/parametros")


@app.route("/api/params_read", methods=["POST"])
def api_params_read():
    try:
        r = _need_usb()
        return jsonify(ok=True, rf=r.rf_power(), mode=r.work_mode(),
                       beep=r.beep(), filter=r.filter_time(),
                       freq=r.read_freq().hex().upper())
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/params_apply", methods=["POST"])
def api_params_apply():
    """Escribe los parametros y los relee: devuelve que quedo grabado y que no."""
    cfg = request.get_json(silent=True) or {}
    try:
        r = _need_usb()
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))
    applied, failed = {}, []
    plan = [
        ("rf", lambda v: r.set_rf_power(int(v)), lambda: r.rf_power()),
        ("mode", lambda v: r.set_work_mode(int(v)), lambda: r.work_mode()),
        ("beep", lambda v: r.set_beep(bool(v)), lambda: r.beep()),
        ("filter", lambda v: r.set_filter_time(int(v)), lambda: r.filter_time()),
    ]
    try:
        for key, setter, getter in plan:
            if key not in cfg:
                continue
            want = cfg[key]
            try:
                setter(want)
                got = getter()
            except Exception as exc:  # noqa: BLE001
                failed.append({"field": key, "want": want, "got": None,
                               "error": str(exc)})
                continue
            norm_want = (1 if want else 0) if key == "beep" else int(want)
            norm_got = (1 if got else 0) if key == "beep" else int(got)
            applied[key] = {"want": norm_want, "got": norm_got}
            if norm_got != norm_want:
                failed.append({"field": key, "want": norm_want, "got": norm_got})
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))
    return jsonify(ok=not failed, applied=applied, failed=failed)


def _hex(b, n=4, default=b"\x00" * 4):
    try:
        v = bytes.fromhex((b or "").replace(" ", ""))
        return v if len(v) == n else default
    except ValueError:
        return default


@app.route("/api/mem_read", methods=["POST"])
def api_mem_read():
    cfg = request.get_json(silent=True) or {}
    mem = int(cfg.get("mem", 1))
    ptr = int(str(cfg.get("ptr", "00")), 16)
    count = int(str(cfg.get("count", "06")), 16)
    pwd = _hex(cfg.get("pwd"))

    def _do():
        r = _need_usb()
        data = r.read_card(mem, ptr, count, pwd)
        out = data.hex().upper()
        if mem == 1 and len(data) >= 4:
            out += "  (word 0-1 = CRC + PC, el EPC arranca en word 2)"
        return out
    try:
        return jsonify(ok=True, hex=_guarded(_do))
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/mem_write", methods=["POST"])
def api_mem_write():
    cfg = request.get_json(silent=True) or {}
    try:
        data = bytes.fromhex(str(cfg.get("data", "")).replace(" ", ""))
    except ValueError:
        return jsonify(ok=False, error="hex invalido")
    if len(data) % 2:
        return jsonify(ok=False, error="hex par (palabras de 2 bytes)")
    mem = int(cfg.get("mem", 3))
    ptr = int(str(cfg.get("ptr", "00")), 16)
    pwd = _hex(cfg.get("pwd"))
    fp = bool(cfg.get("fp"))

    def _do():
        r = _need_usb()
        if fp:
            from ru5600_core import check_fingerprint
            tags = r.scan()
            if not tags:
                raise RuntimeError("sin tag a la vista")
            okfp, msg = check_fingerprint(r, tags[0].epc or tags[0].raw_id)
            if not okfp:
                raise RuntimeError("huella: " + msg)
        r.write_card_verified(data, mem, ptr, pwd)
        return True
    try:
        _guarded(_do, timeout=120)
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/epc_write", methods=["POST"])
def api_epc_write():
    cfg = request.get_json(silent=True) or {}
    try:
        epc = bytes.fromhex(str(cfg.get("epc", "")).replace(" ", ""))
        if len(epc) % 2:
            return jsonify(ok=False, error="EPC par (multiplo de 2 bytes)")
    except ValueError:
        return jsonify(ok=False, error="EPC hex invalido")
    pwd = _hex(cfg.get("pwd"), 4)
    fp = bool(cfg.get("fp"))
    def _do():
        r = _need_usb()
        if fp:
            from ru5600_core import check_fingerprint
            tags = r.scan()
            if not tags:
                raise RuntimeError("sin tag a la vista")
            okfp, msg = check_fingerprint(r, tags[0].epc or tags[0].raw_id)
            if not okfp:
                raise RuntimeError("huella: " + msg)
        r.write_epc(epc, pwd, verify=True)
        return True
    ack_error = None
    try:
        _guarded(_do, timeout=120)
        return jsonify(ok=True, verified=True)
    except Exception as exc:  # noqa: BLE001
        ack_error = str(exc)
    # el ACK del lector miente seguido: la verdad la da la relectura del tag
    try:
        r = _need_usb()
        res, epc_leido, _raw, _det = _direct_read(r)
        if res == "ok" and epc_leido and epc_leido.upper() == epc.hex().upper():
            _log("warn", "escritura: ACK fallo pero el tag quedo con el EPC pedido")
            return jsonify(ok=True, verified=True, ack_error=ack_error,
                           note="el ACK del lector fallo, pero la relectura confirma el EPC")
        return jsonify(ok=False, verified=False, error=ack_error,
                       epc_leido=epc_leido)
    except Exception as exc2:  # noqa: BLE001
        return jsonify(ok=False, verified=False, error=ack_error,
                       verify_error=str(exc2))


@app.route("/api/fp_learn", methods=["POST"])
def api_fp_learn():
    try:
        from ru5600_core import learn_fingerprint
        r = _need_usb()
        tags = r.scan()
        if not tags:
            return jsonify(ok=False, error="sin tag a la vista")
        tid = learn_fingerprint(r, tags[0].epc or tags[0].raw_id)
        return jsonify(ok=True, tid=tid)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/fp_check", methods=["POST"])
def api_fp_check():
    try:
        from ru5600_core import check_fingerprint
        r = _need_usb()
        tags = r.scan()
        if not tags:
            return jsonify(ok=False, error="sin tag a la vista")
        ok, msg = check_fingerprint(r, tags[0].epc or tags[0].raw_id)
        return jsonify(ok=ok, msg=msg)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/chip_analyze", methods=["POST"])
def api_chip_analyze():
    cfg = request.get_json(silent=True) or {}
    pwd = _hex(cfg.get("pwd"))
    deep = bool(cfg.get("deep"))

    def _do():
        from ru5600_chip import analyze_chip, format_report
        r = _need_usb()
        return format_report(analyze_chip(r, pwd, deep=deep))
    try:
        return jsonify(ok=True, report=_guarded(_do, timeout=120))
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/pwd_test", methods=["POST"])
def api_pwd_test():
    cfg = request.get_json(silent=True) or {}
    try:
        r = _need_usb()
        r.read_card(1, 0, 2, _hex(cfg.get("pwd")), retries=2)
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/freq_read", methods=["POST"])
def api_freq_read():
    try:
        r = _need_usb()
        return jsonify(ok=True, raw=r.read_freq().hex().upper())
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/freq_set", methods=["POST"])
def api_freq_set():
    cfg = request.get_json(silent=True) or {}
    try:
        from ru5600_core import FREQ_BANDS
        r = _need_usb()
        band = str(cfg.get("band", "USA"))
        if band not in FREQ_BANDS:
            return jsonify(ok=False, error="banda desconocida")
        r.set_freq(band)
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/silence", methods=["POST"])
def api_silence():
    try:
        _need_usb().set_beep(False)
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    try:
        r = _need_usb()
        r._lib.CFHid_RebootDevice(0xFF)
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/open", methods=["POST"])
def api_open():
    global _reader, _net_reader
    cfg = request.get_json(silent=True) or {}
    t = cfg.get("transport", "usb")
    try:
        _log("info", "abrir conexión: transporte=%s ip=%s puerto=%s" % (t, cfg.get("ip"), cfg.get("port")))
        if t == "net":
            from ru5600_net import NetReader
            _net_reader = NetReader(cfg.get("ip", "192.168.1.250"),
                                    int(cfg.get("port", 60000)))
            _net_reader.open()
            _net_info["mode"] = "net"
        else:
            _net_info["mode"] = t
            _open_reader()
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/tcp_server", methods=["POST"])
def api_tcp_server():
    return jsonify(msg="TCPServer: no soportado en esta build (transporte USB)")


@app.route("/api/help")
def api_help():
    return jsonify(text="RU5600 Reader (cliente Linux no oficial). Transporte: USB via libusb (lib vendor libCFHidApi). Equipo: WCH 1a86:e010. Si Abrir falla, instala la regla udev para leer sin root.")


UDEV_RULE = 'SUBSYSTEM=="usb", ATTR{idVendor}=="1a86", ATTR{idProduct}=="e010", MODE="0666", GROUP="plugdev"\n'


@app.route("/api/udev_rule")
def api_udev_rule():
    cmd = ("install -m 644 99-inveton-ru5600.rules /etc/udev/rules.d/ && "
           "sudo udevadm control --reload-rules && sudo udevadm trigger")
    return jsonify(rule=UDEV_RULE.rstrip("\n"),
                   cmd="sudo sh -c 'echo \'%s\' > /etc/udev/rules.d/99-inveton-ru5600.rules' && %s" % (UDEV_RULE.rstrip("\n"), cmd))


@app.route("/api/config_export")
def api_config_export():
    from flask import Response
    try:
        r = _need_usb()
        cfg = {"rfpower": r.rf_power(), "workmode": r.work_mode(),
               "buzzer": int(r.beep()), "filtertime": r.filter_time(),
               "freq": r.read_freq().hex().upper()}
    except Exception:  # noqa: BLE001
        cfg = {}
    body = "\n".join(f"{k}={v}" for k, v in cfg.items()) + "\n"
    return Response(body, mimetype="text/plain",
                    headers={"Content-Disposition":
                             "attachment; filename=ru5600-config.ini"})


@app.route("/api/config_import", methods=["POST"])
def api_config_import():
    vals = {}
    for line in str(request.get_json(silent=True).get("text", "")).splitlines():
        if "=" in line:
            k, v = line.strip().split("=", 1)
            vals[k.strip()] = v.strip()
    try:
        r = _need_usb()
        if "rfpower" in vals:
            r.set_rf_power(int(vals["rfpower"]))
        if "workmode" in vals:
            r.set_work_mode(int(vals["workmode"]))
        if "buzzer" in vals:
            r.set_beep(vals["buzzer"] == "1")
        if "filtertime" in vals:
            r.set_filter_time(int(vals["filtertime"]))
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/relay", methods=["POST"])
def api_relay():
    cfg = request.get_json(silent=True) or {}
    try:
        _need_usb().relay(bool(cfg.get("on")))
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/close", methods=["POST"])
def api_close():
    global _reader, _net_reader
    try:
        if _reader is not None and _reader.is_open:
            _reader.close()
        if _net_reader is not None and _net_reader.is_open():
            _net_reader.close()
    except Exception:  # noqa: BLE001
        pass
    _reader, _net_reader = None, None
    return jsonify(ok=True)


@app.route("/api/scan_port", methods=["POST"])
def api_scan_port():
    devs = _usb_nodes() or ["1a86:e010 (SWUSB_HID)"]
    return jsonify(devices=devs)


@app.route("/api/scan_net", methods=["POST"])
def api_scan_net():
    import socket
    cfg = request.get_json(silent=True) or {}
    try:
        s = socket.create_connection((cfg.get("ip", "192.168.1.250"),
                                      int(cfg.get("port", 60000))), timeout=5)
        s.close()
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


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


def _open_reader():
    """Abre el lector (solo lo llama el boton Abrir y la recuperacion)."""
    global _reader
    if _reader is None or not _reader.is_open:
        r = Ru5600Reader()
        r.open()
        _reader = r
    return _reader


def _get_reader():
    """Devuelve el lector ya abierto. Nunca abre solo: si esta cerrado, avisa."""
    if _reader is None or not _reader.is_open:
        raise RuntimeError("Abrí la conexión primero (pestaña Conexión).")
    return _reader


@app.route("/")
def index():
    return render_template("conexion.html")


@app.route("/activa")
def activa():
    return render_template("activa.html")


@app.route("/riesgo")
def riesgo():
    return render_template("riesgo.html")


@app.route("/blanca")
def blanca():
    return render_template("blanca.html")


@app.route("/lote")
def lote():
    return render_template("lote.html")


_active = {"running": False, "tags": [], "seen": set()}
_active_stop = threading.Event()
_batch = {"running": False, "done": 0, "total": 0, "rows": []}
_batch_stop = threading.Event()


def _carve(blob):
    """(epc, chip) patron por layout + DB, o (None, None)."""
    from ru5600_chip import TID_PREFIX_DB
    cand, tp = None, None
    if len(blob) >= 24 and blob[0] == 0xE2 and blob[12] == 0xE2:
        cand, tp = blob[0:12], blob[12:24]
    elif (len(blob) >= 36 and blob[0] == 0xE2 and blob[24] == 0xE2
            and all(b == 0 for b in blob[12:24])):
        cand, tp = blob[0:12], blob[24:36]
    if cand is not None and tp.hex().upper()[:8] in TID_PREFIX_DB:
        return cand.hex().upper(), tp.hex().upper()[:8] + " (patron)"
    return None, None


@app.route("/api/active_start", methods=["POST"])
def api_active_start():
    with _lock:
        if _active["running"]:
            return jsonify(ok=False, error="ya corriendo")
        _active.update(running=True, tags=[], seen=set())
        _active_stop.clear()
    threading.Thread(target=_active_run, daemon=True).start()
    return jsonify(ok=True)


@app.route("/api/active_stop", methods=["POST"])
def api_active_stop():
    _active_stop.set()
    return jsonify(ok=True)


@app.route("/api/active_tags")
def api_active_tags():
    return jsonify(running=_active["running"], tags=_active["tags"])


def _active_run():
    try:
        r = _get_reader()
        from ru5600_core import DEV_ADDR, PARAM_WORKMODE, WORKMODE_ACTIVE
        r._pace()
        r._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                      WORKMODE_ACTIVE)
        r._pace(settle=True)
        r._lib.CFHid_ClearTagBuf()
        r._pace()
        r._lib.CFHid_StartRead()
        r._pace(settle=True)
        try:
            while not _active_stop.is_set():
                import time as _t
                _t.sleep(0.1)
                for tag in r.poll_tags():
                    raw = (bytes(tag.raw_id) if tag.raw_id
                           else bytes(tag.epc))
                    key = raw.hex().upper()
                    if key in _active["seen"]:
                        for row in _active["tags"]:
                            if row["raw"] == key:
                                row["n"] += 1
                        continue
                    _active["seen"].add(key)
                    epc, chip = _carve(raw)
                    _active["tags"].append({
                        "raw": key, "epc": epc or key[:24],
                        "tid": "", "chip": chip or "interpreter pending",
                        "ant": tag.ant, "rssi": tag.rssi, "n": 1})
        finally:
            try:
                r._lib.CFHid_StopRead()
            except Exception:  # noqa: BLE001
                pass
            r._pace(settle=True)
    except Exception as exc:  # noqa: BLE001
        _active["tags"].append({"raw": "", "epc": "ERROR: " + str(exc),
                                "tid": "", "chip": "", "ant": "",
                                "rssi": "", "n": 0})
    finally:
        _active["running"] = False


_measure = {"running": False, "result": None, "error": None,
            "seconds": 0.0, "started": 0.0}
_measure_stop = threading.Event()


@app.route("/api/measure", methods=["POST"])
def api_measure():
    """Medicion de antena en segundo plano: se puede cortar con /api/measure_stop."""
    cfg = request.get_json(silent=True) or {}
    try:
        seconds = max(1.0, min(120.0, float(cfg.get("seconds", 10))))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="segundos invalidos")
    with _lock:
        if _measure["running"]:
            return jsonify(ok=False, error="ya esta midiendo")
        _measure.update(running=True, result=None, error=None,
                        seconds=seconds, started=time.time())
        _measure_stop.clear()

    def _run():
        try:
            r = _need_usb()
            st = r.measure(seconds, stop=_measure_stop)
            _measure["result"] = {
                "det": st["detections"],
                "dps": round(st["detections_per_s"], 2),
                "rssi": round(st["rssi_avg"], 1),
                "elapsed": round(st["elapsed"], 1),
                "stopped": _measure_stop.is_set(),
            }
        except Exception as exc:  # noqa: BLE001
            _measure["error"] = str(exc)
        finally:
            _measure["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify(ok=True, seconds=seconds)


@app.route("/api/measure_status")
def api_measure_status():
    left = 0.0
    if _measure["running"]:
        left = max(0.0, _measure["seconds"] - (time.time() - _measure["started"]))
    return jsonify(running=_measure["running"], result=_measure["result"],
                   error=_measure["error"], left=round(left, 1))


@app.route("/api/measure_stop", methods=["POST"])
def api_measure_stop():
    if _measure["running"]:
        _measure_stop.set()
    return jsonify(ok=True)


def _risk_ok(cfg):
    return bool((cfg or {}).get("ack"))


@app.route("/api/lock", methods=["POST"])
def api_lock():
    cfg = request.get_json(silent=True) or {}
    if not _risk_ok(cfg):
        return jsonify(ok=False, error="confirmá el riesgo")
    try:
        data = bytes.fromhex(str(cfg.get("data", "")).replace(" ", ""))
        if len(data) != 4:
            return jsonify(ok=False, error="lock data: 4 bytes hex")
        action = int(str(cfg.get("action", "0")), 16)
        r = _need_usb()
        ok = r.lock_card(data, action, confirm=True)
        return jsonify(ok=True, accepted=bool(ok))
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


_BLOCKED = {0x2B, 0x2C, 0x2D, 0x65, 0x40, 0x41}


@app.route("/api/raw", methods=["POST"])
def api_raw():
    cfg = request.get_json(silent=True) or {}
    if not _risk_ok(cfg):
        return jsonify(ok=False, error="confirmá el riesgo")
    try:
        cmd = int(str(cfg.get("cmd", "0")), 16)
        if cmd in _BLOCKED:
            return jsonify(ok=False, error="comando bloqueado (0x%02X)" % cmd)
        payload = bytes.fromhex(str(cfg.get("payload", "") or "").replace(" ", ""))
        timeout = int(cfg.get("timeout", 400) or 400)
        r = _need_usb()
        ok, body = r.raw_command(cmd, payload, timeout, want_payload=True)
        return jsonify(ok=True, answered=bool(ok),
                       hex=(bytes(body or b"").hex().upper()))
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/cryptwrite", methods=["POST"])
def api_cryptwrite():
    cfg = request.get_json(silent=True) or {}
    if not _risk_ok(cfg):
        return jsonify(ok=False, error="confirmá el riesgo")
    try:
        mode = int(str(cfg.get("mode", "0")), 16)
        pwd = str(cfg.get("pwd", "")).encode()
        r = _need_usb()
        res = r.raw_command(0x2F, bytes([mode & 0xFF, 0x55]) + pwd,
                            want_payload=True)
        good = res[0] if isinstance(res, tuple) else res
        return jsonify(ok=bool(good))
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/wl/list")
def api_wl_list():
    def _do():
        r = _need_usb()
        n = r.whitelist_count()
        out = []
        for i in range(n):
            try:
                out.append(r.whitelist_read(i).hex().upper())
            except Exception:  # noqa: BLE001
                break
        return out
    try:
        return jsonify(entries=_guarded(_do))
    except Exception as exc:  # noqa: BLE001
        return jsonify(entries=[], error=str(exc))


@app.route("/api/wl/add", methods=["POST"])
def api_wl_add():
    cfg = request.get_json(silent=True) or {}
    try:
        data = bytes.fromhex(str(cfg.get("hex", "")).replace(" ", ""))
        # el vendor carga EPC de 12 bytes (24 hex); se acepta 32 por compatibilidad
        if len(data) not in (12, 32):
            return jsonify(ok=False, error="EPC de 24 caracteres hex (12 bytes)")
        _need_usb().whitelist_write(data)
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/wl/del", methods=["POST"])
def api_wl_del():
    cfg = request.get_json(silent=True) or {}
    try:
        _need_usb().whitelist_delete(
            bytes.fromhex(str(cfg.get("hex", "")).replace(" ", "")))
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/wl/clear", methods=["POST"])
def api_wl_clear():
    try:
        _need_usb().whitelist_clear()
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/wl/import", methods=["POST"])
def api_wl_import():
    cfg = request.get_json(silent=True) or {}
    n = 0
    try:
        r = _need_usb()
        for line in str(cfg.get("text", "")).splitlines():
            line = line.strip().replace(" ", "")
            if not line:
                continue
            data = bytes.fromhex(line)
            if len(data) != 32:
                return jsonify(ok=False,
                                 error=f"linea invalida (no 32B): {line[:16]}...")
            r.whitelist_write(data)
            n += 1
        return jsonify(ok=True, n=n)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/batch_start", methods=["POST"])
def api_batch_start():
    """Escribe EPC secuenciales: base + step * n por cada tag que aparece."""
    cfg = request.get_json(silent=True) or {}
    with _lock:
        if _batch["running"]:
            return jsonify(ok=False, error="ya corriendo")
        raw = str(cfg.get("base") or cfg.get("epc") or "")
        raw = raw.replace(" ", "").replace("0x", "").replace("0X", "")
        try:
            base = bytes.fromhex(raw)
        except ValueError:
            return jsonify(ok=False, error="EPC hex invalido")
        if not base:
            return jsonify(ok=False, error="EPC vacio")
        if len(base) % 2:
            return jsonify(ok=False, error="EPC par (multiplo de 2 bytes)")
        raw_step = str(cfg.get("step", "1")).strip().replace("0x", "") or "1"
        try:
            step = int(raw_step, 16)
        except ValueError:
            return jsonify(ok=False, error="incremento hex invalido")
        if step < 0:
            return jsonify(ok=False, error="el incremento no puede ser negativo")
        try:
            total = int(cfg.get("total", 10) or 10)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="cantidad invalida")
        if total < 1:
            return jsonify(ok=False, error="cantidad invalida")
        limit = (1 << (8 * len(base))) - 1
        if int.from_bytes(base, "big") + step * (total - 1) > limit:
            return jsonify(ok=False,
                           error="la serie se desborda: achicá la cantidad o el incremento")
        _batch.update(running=True, done=0, total=total, rows=[])
        _batch_stop.clear()
    threading.Thread(target=_batch_run,
                     args=(base, step, bool(cfg.get("check", True))),
                     kwargs={"lock_note": bool(cfg.get("lock", False))},
                     daemon=True).start()
    return jsonify(ok=True)


@app.route("/api/batch_status")
def api_batch_status():
    return jsonify(running=_batch["running"], done=_batch["done"],
                   total=_batch["total"], rows=_batch["rows"])


@app.route("/api/batch_stop", methods=["POST"])
def api_batch_stop():
    _batch_stop.set()
    return jsonify(ok=True)


def _batch_run(base, step, check, lock_note=False):
    base_int = int.from_bytes(base, "big")
    width = len(base)
    try:
        r = _get_reader()
        seen = set()
        while not _batch_stop.is_set() and _batch["done"] >= 0:
            if _batch["done"] >= _batch["total"]:
                break
            try:
                tags = r.scan()
            except Exception:  # noqa: BLE001
                break
            progressed = False
            for tag in tags:
                key = (tag.epc_hex, tag.ant)
                if key in seen:
                    continue
                seen.add(key)
                epc = (base_int + step * _batch["done"]).to_bytes(width, "big")
                try:
                    r.write_epc(epc)
                    res = "written"
                    if check:
                        back = r.read_card(1, 0,
                                          max(4, (len(epc) + 4 + 1) // 2),
                                          b"\x00" * 4, retries=4)
                        if back[4:4 + len(epc)] != epc:
                            res = "verify FAILED"
                    if lock_note and res == "written":
                        res += " (lock: hacelo en Peligroso)"
                    _batch["done"] += 1
                    progressed = True
                except Exception as exc:  # noqa: BLE001
                    res = "ERROR: " + str(exc)[:60]
                _batch["rows"].append(
                    {"tag": key[0], "epc": epc.hex().upper(),
                     "result": res, "rssi": tag.rssi})
            if not progressed:
                import time as _t
                _t.sleep(0.5)
    except Exception as exc:  # noqa: BLE001
        _batch["rows"].append({"epc": "", "result": "ERROR: " + str(exc),
                               "rssi": ""})
    finally:
        _batch["running"] = False


_map = {"running": False, "done": 0, "total": 0, "rows": []}
_map_stop = threading.Event()


def _norm_epc(v):
    s = str(v if v is not None else "").strip().upper()
    s = s.replace(" ", "").replace(":", "").replace("-", "")
    if s.startswith("0X"):
        s = s[2:]
    return s


def _map_problems(a_hex, b_hex):
    probs = []
    for label, v in (("columna A", a_hex), ("columna B", b_hex)):
        if not v:
            probs.append(label + " vacia")
        elif not re.fullmatch(r"[0-9A-F]+", v):
            probs.append(label + " no es hex")
        elif len(v) % 2:
            probs.append(label + " tiene largo impar")
        elif not 12 <= len(v) <= 64:
            probs.append(label + " fuera de 12..64 caracteres hex")
    if not probs:
        if len(a_hex) != len(b_hex):
            probs.append("A y B tienen distinto largo")
        elif a_hex == b_hex:
            probs.append("A y B son iguales")
    return probs


@app.route("/api/export/xlsx", methods=["POST"])
def api_export_xlsx():
    """Recibe headers + rows y devuelve un .xlsx (para las tablas de salida)."""
    cfg = request.get_json(silent=True) or {}
    headers = [str(h) for h in (cfg.get("headers") or [])]
    rows = cfg.get("rows") or []
    if not rows:
        return jsonify(ok=False, error="no hay filas para exportar"), 400
    if len(rows) > 20000:
        rows = rows[:20000]
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook()
        ws = wb.active
        ws.title = (cfg.get("sheet") or "datos")[:31]
        if headers:
            ws.append(headers)
            for c in ws[1]:
                c.font = Font(bold=True)
        for r in rows:
            ws.append(["" if v is None else v for v in r])
        for i, h in enumerate(headers, start=1):
            ancho = max(len(str(h)), *(len(str(r[i - 1])) for r in rows if len(r) >= i)) if rows else len(str(h))
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(48, max(10, ancho + 2))
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        name = (cfg.get("name") or "export").replace("/", "-")[:60]
        if not name.lower().endswith(".xlsx"):
            name += ".xlsx"
        return send_file(buf, as_attachment=True, download_name=name,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)), 500


@app.route("/api/export/txt", methods=["POST"])
def api_export_txt():
    """Recibe texto libre y devuelve un .txt (para informes y lecturas)."""
    cfg = request.get_json(silent=True) or {}
    text = cfg.get("text") or ""
    if not str(text).strip():
        return jsonify(ok=False, error="no hay texto para exportar"), 400
    name = (cfg.get("name") or "export").replace("/", "-")[:60]
    if not name.lower().endswith(".txt"):
        name += ".txt"
    buf = io.BytesIO(str(text).encode("utf-8"))
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype="text/plain; charset=utf-8")


@app.route("/api/map_template")
def api_map_template():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "cambios"
    ws.append(["EPC actual", "EPC nuevo"])
    ws.append(["E2004704D9A06820068E0001", "E2004704D9A06820068E0101"])
    ws.append(["E2004704D9A06820068E0002", "E2004704D9A06820068E0102"])
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 32
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name="plantilla-cambio-epc.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/api/map_parse", methods=["POST"])
def api_map_parse():
    """Lee la planilla (A = EPC actual, B = EPC nuevo) y valida todo antes de escribir."""
    up = request.files.get("file")
    if up is None:
        return jsonify(ok=False, error="falta el archivo")
    name = (up.filename or "").lower()
    raw_rows = []
    try:
        if name.endswith((".xlsx", ".xlsm")):
            from openpyxl import load_workbook
            wb = load_workbook(up, data_only=True, read_only=True)
            for row in wb.active.iter_rows(values_only=True):
                if row and any(v not in (None, "") for v in row):
                    raw_rows.append((row[0], row[1] if len(row) > 1 else None))
        else:
            import csv as _csv
            text = up.read().decode("utf-8-sig", "replace")
            for row in _csv.reader(io.StringIO(text)):
                if row and any(v.strip() for v in row):
                    raw_rows.append((row[0], row[1] if len(row) > 1 else None))
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error="no pude leer la planilla: " + str(exc))

    if raw_rows:
        head_a = str(raw_rows[0][0] or "").strip().lower()
        head_b = str(raw_rows[0][1] or "").strip().lower()
        if head_a in ("epc actual", "epc", "tag", "actual") and head_b in ("epc nuevo", "nuevo", "epc nuevo ", ""):
            raw_rows = raw_rows[1:]

    plan, errors = [], []
    seen_a, seen_b = {}, {}
    for i, (va, vb) in enumerate(raw_rows, start=1):
        a_hex, b_hex = _norm_epc(va), _norm_epc(vb)
        if not a_hex and not b_hex:
            continue
        probs = _map_problems(a_hex, b_hex)
        if not probs:
            if a_hex in seen_a:
                probs.append("A repetido (fila %d)" % seen_a[a_hex])
            if b_hex in seen_b:
                probs.append("B repetido (fila %d)" % seen_b[b_hex])
        if probs:
            errors.append({"row": i, "a": a_hex, "b": b_hex,
                           "error": "; ".join(probs)})
        else:
            seen_a[a_hex] = i
            seen_b[b_hex] = i
            plan.append({"row": i, "a": a_hex, "b": b_hex})

    valid = bool(plan) and not errors
    return jsonify(ok=True, plan=plan, errors=errors, valid=valid,
                   total=len(plan) + len(errors),
                   ignored="las demás columnas se ignoran")


@app.route("/api/map_start", methods=["POST"])
def api_map_start():
    cfg = request.get_json(silent=True) or {}
    plan = cfg.get("plan") or []
    if not plan:
        return jsonify(ok=False, error="la planilla no tiene filas para escribir")
    clean = []
    for e in plan:
        a_hex, b_hex = _norm_epc(e.get("a")), _norm_epc(e.get("b"))
        if _map_problems(a_hex, b_hex):
            return jsonify(ok=False, error="fila invalida: " + a_hex)
        clean.append({"a": a_hex, "b": b_hex})
    with _lock:
        if _map["running"]:
            return jsonify(ok=False, error="ya corriendo")
        _map.update(running=True, done=0, total=len(clean), rows=[])
        _map_stop.clear()
    threading.Thread(target=_map_run, args=(clean,), daemon=True).start()
    return jsonify(ok=True, total=len(clean))


@app.route("/api/map_status")
def api_map_status():
    return jsonify(running=_map["running"], done=_map["done"],
                   total=_map["total"], rows=_map["rows"])


@app.route("/api/map_stop", methods=["POST"])
def api_map_stop():
    _map_stop.set()
    return jsonify(ok=True)


def _map_run(plan):
    pending = {e["a"]: e["b"] for e in plan}
    targets = {e["b"]: e["a"] for e in plan}   # ya modificado -> ignorar
    try:
        r = _get_reader()
        seen = set()
        while not _map_stop.is_set() and pending:
            try:
                tags = r.scan()
            except Exception:  # noqa: BLE001
                break
            progressed = False
            for tag in tags:
                key = (tag.epc_hex, tag.ant)
                if key in seen:
                    continue
                seen.add(key)
                cur = (tag.epc_hex or "").upper()
                if cur not in pending:
                    # si ya tiene el EPC nuevo, la fila se da por cumplida y no se escribe
                    if cur in targets:
                        origen = targets.pop(cur)
                        pending.pop(origen, None)
                        _map["done"] += 1
                        _map["rows"].append({"tag": cur, "new": cur,
                                             "result": "ya tenía el EPC nuevo (sin escribir)"})
                        progressed = True
                    continue
                new_hex = pending[cur]
                new_bytes = bytes.fromhex(new_hex)
                try:
                    r.write_epc(new_bytes)
                    back = r.read_card(1, 0, max(4, (len(new_bytes) + 4 + 1) // 2),
                                      b"\x00" * 4, retries=4)
                    res = "escrito" if back[4:4 + len(new_bytes)] == new_bytes else "verificación FALLÓ"
                except Exception as exc:  # noqa: BLE001
                    res = "ERROR: " + str(exc)[:60]
                _map["done"] += 1
                _map["rows"].append({"tag": cur, "new": new_hex, "result": res})
                pending.pop(cur, None)
                progressed = True
            if not progressed:
                time.sleep(0.4)
    except Exception as exc:  # noqa: BLE001
        _map["rows"].append({"tag": "", "new": "",
                             "result": "ERROR: " + str(exc)[:80]})
    finally:
        _map["running"] = False


@app.route("/pruebas")
def pruebas():
    return render_template("pruebas.html")


@app.route("/barrido")
def barrido():
    return render_template("barrido.html")


@app.route("/distancia")
def distancia():
    return render_template("distancia.html")


def _rssi_stats(values):
    vals = [v for v in values if v]
    if not vals:
        return None, None, None
    return (round(sum(vals) / float(len(vals)), 1), min(vals), max(vals))


def _chip_from_prefix(tid_hex):
    prefix = (tid_hex or "")[:8].upper()
    if not prefix:
        return None
    entry = TID_PREFIX_DB.get(prefix)
    if isinstance(entry, tuple) and len(entry) >= 2:
        return ("%s %s" % (entry[0], entry[1])).strip()
    return prefix


def _stream_sweep(r, seconds):
    """Barrido de stream en ActiveMode: lecturas y RSSI por EPC.

    Es la misma secuencia del stream de la app (ActiveMode + StartRead +
    poll_tags), pero acumulando por tag en vez de quedarse con el primero:
    asi cada punto de la curva tiene sus propias lecturas y su RSSI.
    Devuelve ``(filas, elapsed)`` con rssi_avg/min/max ya calculados.
    """
    from ru5600_core import (DEV_ADDR, PARAM_WORKMODE, WORKMODE_ACTIVE,
                             WORKMODE_ANSWER)
    r._pace()
    r._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE, WORKMODE_ACTIVE)
    r._pace(settle=True)
    r._lib.CFHid_ClearTagBuf()
    r._pace()
    r._lib.CFHid_StartRead()
    r._pace(settle=True)
    t0 = time.monotonic()
    stats: dict = {}
    elapsed = 0.0
    try:
        while time.monotonic() - t0 < seconds:
            for tag in r.poll_tags():
                epc = (getattr(tag, "epc_hex", "") or "").upper()
                chip = None
                tid = bytes(getattr(tag, "tid", b"") or b"")
                if tid:
                    chip = _chip_from_prefix(tid.hex().upper())
                if not epc:
                    epc, carved = _carve(bytes(tag.raw_id) if tag.raw_id
                                         else bytes(tag.epc))
                    chip = chip or carved
                if not epc:
                    continue
                row = stats.setdefault(epc, {"epc": epc, "chip": chip,
                                             "lecturas": 0, "rssi": []})
                row["lecturas"] += 1
                if tag.rssi:
                    row["rssi"].append(int(tag.rssi))
                if chip and not row["chip"]:
                    row["chip"] = chip
            time.sleep(0.1)
    finally:
        # el tiempo de medicion termina con el loop: el StopRead y la vuelta a
        # AnswerMode son saneamiento, no cuentan para las lecturas por segundo
        elapsed = max(time.monotonic() - t0, 0.001)
        try:
            r._lib.CFHid_StopRead()
        except Exception:  # noqa: BLE001
            pass
        r._pace(settle=True)
        try:
            r._lib.CFHid_ClearTagBuf()
            r._pace()
            r._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                           WORKMODE_ANSWER)
            r._pace(settle=True)
        except Exception:  # noqa: BLE001
            pass
    rows = []
    for row in stats.values():
        avg, lo, hi = _rssi_stats(row.pop("rssi"))
        row.update(dps=round(row["lecturas"] / elapsed, 2),
                   rssi_avg=avg, rssi_min=lo, rssi_max=hi)
        rows.append(row)
    rows.sort(key=lambda x: x["lecturas"], reverse=True)
    return rows, elapsed


@app.route("/api/distance_point", methods=["POST"])
def api_distance_point():
    """Mide un punto de la curva: seconds de stream y guarda una fila por EPC."""
    cfg = request.get_json(silent=True) or {}
    label = str(cfg.get("label") or "").strip()
    if not label:
        return jsonify(ok=False, error="poné una etiqueta (tag y altura)"), 400
    try:
        distance = float(cfg.get("distance", ""))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="distancia invalida (cm)"), 400
    if distance < 0:
        return jsonify(ok=False, error="la distancia no puede ser negativa"), 400
    try:
        seconds = float(cfg.get("seconds", 5))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="segundos invalidos"), 400
    seconds = max(1.0, min(seconds, 120.0))
    try:
        r = _need_usb()
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))
    try:
        measured, elapsed = _stream_sweep(r, seconds)
    except Exception as exc:  # noqa: BLE001
        _log("error", "distance_point: %s" % exc)
        return jsonify(ok=False, error="barrido falló: " + str(exc))
    rows = []
    save_error = None
    for row in measured:
        try:
            saved = store.add_medicion(label, distance, row["epc"],
                                       row["chip"], row["lecturas"],
                                       row["dps"], row["rssi_avg"],
                                       row["rssi_min"], row["rssi_max"])
        except Exception as exc:  # noqa: BLE001
            save_error = str(exc)
            _log("warn", "distance_point: no pude guardar: %s" % exc)
            break
        rows.append(saved)
    if measured and not rows:
        return jsonify(ok=False,
                       error="no pude guardar las mediciones: " +
                             (save_error or "error desconocido"))
    if rows:
        best = rows[0]
        _log("ok", "distancia %s @ %s cm: %d tags, %d lecturas, %.1f/s"
             % (label, distance, len(rows), best["lecturas"], best["dps"] or 0))
    else:
        _log("warn", "distancia %s @ %s cm: 0 tags en el stream"
             % (label, distance))
    return jsonify(ok=True, label=label, distance=distance,
                   seconds=round(elapsed, 1), count=len(rows), rows=rows)


@app.route("/api/distance_points")
def api_distance_points():
    try:
        limit = int(request.args.get("limit", 500) or 500)
    except (TypeError, ValueError):
        limit = 500
    label = request.args.get("label", "")
    try:
        points = store.list_mediciones(limit, label)
        labels = store.list_medicion_labels()
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))
    return jsonify(ok=True, count=len(points), labels=labels, points=points)


@app.route("/api/status")
def status():
    now = time.monotonic()
    ts = [x for x in _state.get("hits", []) if now - x <= 10.0]
    _state["hits"] = ts
    last = (now - ts[-1]) if ts else None
    running = _state["running"]
    return jsonify(running=running, error=_state["error"],
                   count=len(_state["tags"]), tags=_state["tags"],
                   target=_state.get("target", 0),
                   reason=_state.get("reason", ""),
                   stabilized=_state.get("stabilized", 0),
                   mode_used=_state.get("mode_used", "stream"),
                   dps=round(len(ts) / 10.0, 1),
                   last_seen=None if last is None else round(last, 1),
                   stalled=bool(running and (last is None or last > 5)),
                   elapsed=round(now - _state["started"], 1) if running else 0)


@app.route("/api/start", methods=["POST"])
def start():
    cfg = request.get_json(silent=True) or {}
    try:
        seconds = float(cfg.get("seconds", 60))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="segundos invalidos"), 400
    try:
        target = int(cfg.get("target", 0) or 0)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="tope de tags invalido"), 400
    if target < 0:
        target = 0
    # el stream de tags solo existe en ActiveMode; AnswerMode es para comandos dirigidos
    mode = (cfg.get("mode") or "active").lower()
    with _lock:
        if _state["running"]:
            return jsonify(ok=False, error="ya corriendo"), 409
        _state.update(running=True, tags=[], error="", target=target,
                      reason="", started=time.monotonic(), hits=[])
        _state["seen"] = {}
    _stop.clear()
    threading.Thread(target=_run, args=(seconds, target, mode),
                     daemon=True).start()
    return jsonify(ok=True, target=target, mode=mode)


@app.route("/api/stop", methods=["POST"])
def stop():
    _stop.set()
    return jsonify(ok=True)


def _run(seconds, target=0, mode="answer"):
    reason = ""
    try:
        idx, codes = _excel()
    except Exception:  # noqa: BLE001 - excel es bonus
        idx, codes = None, set()
    try:
        r = _get_reader()
        from ru5600_core import (DEV_ADDR, PARAM_WORKMODE, WORKMODE_ACTIVE,
                                 WORKMODE_ANSWER)
        r._pace()
        r._lib.CFHid_SetDeviceOneParam(
            DEV_ADDR, PARAM_WORKMODE,
            WORKMODE_ACTIVE if mode == "active" else WORKMODE_ANSWER)
        r._pace(settle=True)
        r._lib.CFHid_ClearTagBuf()
        r._pace()
        r._lib.CFHid_StartRead()
        r._pace(settle=True)
        deadline = None if seconds <= 0 else time.monotonic() + seconds
        poll = 0.1
        last_hit = time.monotonic()
        stabilized = 0
        _state["stabilized"] = 0
        t_start = time.monotonic()
        direct_mode = False
        # con ventanas cortas, esperar 6 s deja casi sin tiempo al modo dirigido
        fallback_after = float(_state.get("fallback_after") or
                               (min(6.0, max(2.0, seconds * 0.35)) if seconds > 0 else 6.0))
        stream_saw = False
        _state["mode_used"] = "stream"
        try:
            while ((deadline is None or time.monotonic() < deadline)
                    and not _stop.is_set()):
                if target and len(_state["tags"]) >= target:
                    reason = "tope"
                    break
                # poll adaptativo: rapido cuando entrega, mas lento cuando no
                time.sleep(poll)
                got = False
                # --- fallback: tags que no entran al stream (H10, UCODE8) ---
                if (not stream_saw and not _state["tags"] and not direct_mode
                        and (time.monotonic() - t_start) > fallback_after):
                    direct_mode = True
                    _state["mode_used"] = "dirigido"
                    _log("warn", "el stream no ve tags: paso a barrido dirigido")
                    try:
                        r.stop_read()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        from ru5600_core import WORKMODE_ANSWER as _WS
                        r._pace(); r._lib.CFHid_SetDeviceOneParam(
                            DEV_ADDR, PARAM_WORKMODE, _WS); r._pace(settle=True)
                    except Exception:  # noqa: BLE001
                        pass
                if direct_mode:
                    res, epc_d, hex_d, det = _direct_read(r)
                    if res != "ok" or not epc_d:
                        time.sleep(poll)
                        continue
                    info = {"epc": epc_d.upper(), "chip": "", "xl": "-",
                            "conf": "candidato",
                            "code": epc_d[6:14] if len(epc_d) >= 14 else ""}
                    try:
                        tid = r.read_card(2, 0, 2, b"\x00" * 4, retries=2)
                        prefix = tid.hex().upper()[:8]
                        ent = TID_PREFIX_DB.get(prefix)
                        info["chip"] = ("%s %s" % (ent[0], ent[1])).strip() if isinstance(ent, tuple) and len(ent) >= 2 else prefix
                        if isinstance(ent, tuple):
                            info["conf"] = "confirmado"
                    except Exception:  # noqa: BLE001
                        pass
                    if idx is not None:
                        hit = idx.get(info["epc"])
                        if hit is not None:
                            info["xl"] = "%s #%s" % (hit[0], hit[1])
                    key = info["epc"]
                    prev = _state["seen"].get(key)
                    if prev is not None:
                        prev["n"] = prev.get("n", 1) + 1
                        _state.setdefault("hits", []).append(time.monotonic())
                        got = True
                        last_hit = time.monotonic()
                        continue
                    info.update(raw=hex_d[:48], ant=0, rssi=None, n=1)
                    _state["seen"][key] = info
                    _state["tags"].append(info)
                    _state.setdefault("hits", []).append(time.monotonic())
                    got = True
                    last_hit = time.monotonic()
                    continue
                stream_tags = r.poll_tags()
                if stream_tags:
                    stream_saw = True
                for tag in stream_tags:
                    # el parser nuevo ya entrega EPC/TID/RSSI: no re-parsear el blob
                    epc = (getattr(tag, "epc_hex", "") or "").upper()
                    info = {"epc": epc, "chip": "", "xl": "-",
                            "conf": "candidato",
                            "code": epc[6:14] if len(epc) >= 14 else ""}
                    tid = getattr(tag, "tid", b"") or b""
                    if tid:
                        prefix = bytes(tid).hex().upper()[:8]
                        entry = TID_PREFIX_DB.get(prefix)
                        if isinstance(entry, tuple) and len(entry) >= 2:
                            info["chip"] = ("%s %s" % (entry[0], entry[1])).strip()
                            info["conf"] = "confirmado"
                        else:
                            info["chip"] = prefix
                    if idx is not None:
                        hit = idx.get(epc)
                        if hit is not None:
                            info["xl"] = "%s #%s" % (hit[0], hit[1])
                    blob_key = bytes(tag.raw_id or tag.epc)
                    key = epc or blob.hex().upper()
                    prev = _state["seen"].get(key)
                    if prev is not None:
                        prev["n"] = prev.get("n", 1) + 1
                        _state.setdefault("hits", []).append(time.monotonic())
                        continue
                    info.update(raw=blob_key.hex().upper()[:48], ant=tag.ant,
                                rssi=tag.rssi, n=1)
                    _state["seen"][key] = info
                    _state["tags"].append(info)
                    _state.setdefault("hits", []).append(time.monotonic())
                    got = True
                    last_hit = time.monotonic()
                now2 = time.monotonic()
                if got:
                    poll = 0.1
                else:
                    poll = min(0.3, poll + 0.02)
                    # prevencion: si dejo de entregar 6 s, rearmar el stream (una vez cada 6 s)
                    if now2 - last_hit > 6 and stabilized < 40:
                        try:
                            r.stop_read()
                            r._lib.CFHid_ClearTagBuf()
                            r._pace(settle=True)
                            r._lib.CFHid_StartRead()
                            r._pace(settle=True)
                            stabilized += 1
                            _state["stabilized"] = stabilized
                            last_hit = time.monotonic()
                        except Exception:  # noqa: BLE001
                            pass
        finally:
            try:
                r._lib.CFHid_StopRead()
            except Exception:  # noqa: BLE001
                pass
            r._pace(settle=True)
            # volver a AnswerMode (el stream deja el lector en Active)
            try:
                r._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                               WORKMODE_ANSWER)
                r._pace(settle=True)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        _state["error"] = str(exc)
    finally:
        if not reason:
            reason = "usuario" if _stop.is_set() else "tiempo"
        _state["reason"] = reason
        _state["running"] = False
        # persistir el barrido: la app es la dueña de los datos, no el lector
        try:
            for t in _state["tags"]:
                if t.get("epc"):
                    store.upsert_tag(t["epc"], t.get("chip"), t.get("rssi"),
                                     t.get("n", 1))
            if _state["tags"]:
                store.add_log("ok", "barrido: %d tags (%s)" % (len(_state["tags"]), reason))
        except Exception as exc:  # noqa: BLE001
            _log("warn", "no pude guardar el barrido: %s" % exc)


@app.route("/api/recover", methods=["POST"])
def api_recover():
    """Destraba el lector: corta el stream, borra el buffer, vuelve a AnswerMode
    y reabre el dispositivo. No escribe nada en los tags."""
    steps = []
    _stop.set()
    _active_stop.set()
    time.sleep(0.4)
    try:
        r = _get_reader()
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error="no pude abrir el lector: " + str(exc))
    try:
        from ru5600_core import (DEV_ADDR, PARAM_WORKMODE, WORKMODE_ANSWER)
        try:
            r.stop_read()
            steps.append("stream detenido")
        except Exception as exc:  # noqa: BLE001
            steps.append("stop_read: " + str(exc)[:40])
        try:
            r._lib.CFHid_ClearTagBuf()
            steps.append("buffer borrado")
        except Exception as exc:  # noqa: BLE001
            steps.append("clear: " + str(exc)[:40])
        try:
            r._pace()
            r._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                           WORKMODE_ANSWER)
            r._pace(settle=True)
            steps.append("AnswerMode")
        except Exception as exc:  # noqa: BLE001
            steps.append("workmode: " + str(exc)[:40])
        try:
            r.reconnect()
            steps.append("dispositivo reabierto")
        except Exception as exc:  # noqa: BLE001
            steps.append("reconnect: " + str(exc)[:40])
        info = {}
        try:
            info = {"fw": r.read_param(0x00) and None}
        except Exception:  # noqa: BLE001
            info = {}
        return jsonify(ok=True, steps=steps)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc), steps=steps)


@app.route("/api/log")
def api_log():
    since = float(request.args.get("since", 0) or 0)
    items = [x for x in _log_ring if x["ts"] > since]
    return jsonify(entries=items, now=time.time(), total=len(_log_ring))


@app.route("/api/log/clear", methods=["POST"])
def api_log_clear():
    _log_ring.clear()
    return jsonify(ok=True)


@app.route("/api/probe/state")
def api_probe_state():
    """Estado crudo para la pestaña Pruebas."""
    out = {"open": False}
    if _reader is not None and _reader.is_open:
        out["open"] = True
        try:
            out["callback"] = _reader.callback_stats()
        except Exception as exc:  # noqa: BLE001
            out["callback"] = {"error": str(exc)}
        try:
            out["rf"] = _reader.rf_power()
            out["mode"] = _reader.work_mode()
            out["filter"] = _reader.filter_time()
            out["beep"] = _reader.beep()
        except Exception as exc:  # noqa: BLE001
            out["params_error"] = str(exc)
        try:
            fr = _reader.read_freq()
            out["freq"] = fr.hex().upper() if isinstance(fr, (bytes, bytearray)) else fr
        except Exception:  # noqa: BLE001
            pass
    return jsonify(out)


@app.route("/api/probe", methods=["POST"])
def api_probe():
    """Pruebas guiadas, sólo lectura. Nunca escribe en tags."""
    cfg = request.get_json(silent=True) or {}
    what = (cfg.get("what") or "").lower()
    try:
        r = _need_usb()
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))
    try:
        if what == "read_epc":
            t0 = time.monotonic()
            data = r.read_card(1, 0, 6, b"\x00" * 4, retries=3)
            ms = round((time.monotonic() - t0) * 1000)
            _log("ok", "lectura dirigida EPC OK (%d ms): %s" % (ms, data.hex().upper()))
            return jsonify(ok=True, ms=ms, hex=data.hex().upper())
        if what == "scan":
            t0 = time.monotonic()
            tags = r.scan()
            ms = round((time.monotonic() - t0) * 1000)
            _log("ok" if tags else "warn",
                 "scan(): %d tags en %d ms%s" % (len(tags), ms,
                                                 "" if tags else " (sin tags)"))
            return jsonify(ok=True, ms=ms, count=len(tags),
                           tags=[{"epc": t.epc_hex, "ant": t.ant, "rssi": t.rssi} for t in tags])
        if what == "inventory":
            t0 = time.monotonic()
            tags = r.inventory(rounds=8, delay=0.03)
            ms = round((time.monotonic() - t0) * 1000)
            _log("ok" if tags else "warn",
                 "InventoryG2: %d tags en %d ms%s" % (len(tags), ms,
                                                      "" if tags else " (sin respuesta)"))
            return jsonify(ok=True, ms=ms, count=len(tags),
                           tags=[{"epc": t.epc_hex, "ant": t.ant, "rssi": t.rssi} for t in tags])
        if what == "startread":
            r.start_read()
            _log("tx", "StartRead enviado")
            time.sleep(float(cfg.get("wait", 3) or 3))
            tags = r.poll_tags()
            r.stop_read()
            _log("ok" if tags else "warn",
                 "StartRead %ss + poll: %d tags" % (cfg.get("wait", 3), len(tags)))
            return jsonify(ok=True, count=len(tags),
                           callback=r.callback_stats(),
                           tags=[{"epc": t.epc_hex, "ant": t.ant, "rssi": t.rssi} for t in tags])
        if what == "stream_raw":
            # diagnostico crudo del stream: StartRead + N polls de GetTagBuf
            import ctypes as _ct
            from ru5600_core import (DEV_ADDR as _DA, PARAM_WORKMODE as _PW,
                                     WORKMODE_ACTIVE as _WA, WORKMODE_ANSWER as _WS)
            n = int(cfg.get("polls", 20) or 20)
            r._pace()
            r._lib.CFHid_SetDeviceOneParam(_DA, _PW, _WA)
            r._pace(settle=True)
            r._lib.CFHid_ClearTagBuf()
            r.start_read()
            samples = []
            got_tags = []
            for i in range(n):
                time.sleep(0.1)
                buf = (_ct.c_ubyte * 131072)()
                total = _ct.c_uint32(0)
                count = _ct.c_uint32(0)
                ret = r._lib.CFHid_GetTagBuf(buf, _ct.byref(total), _ct.byref(count))
                smp = {"i": i, "ret": int(ret), "total": int(total.value), "count": int(count.value)}
                if count.value and "hex0" not in smp and len(samples) < 40:
                    smp["hex0"] = bytes(buf[:total.value]).hex().upper()
                samples.append(smp)
                if count.value:
                    from ru5600_core import parse_tag_buffer as _ptb
                    for t in _ptb(bytes(buf[:total.value]), count.value):
                        got_tags.append(t.epc_hex)
            r.stop_read()
            try:
                r._pace(); r._lib.CFHid_SetDeviceOneParam(_DA, _PW, _WS)
            except Exception:  # noqa: BLE001
                pass
            with_data = [s2 for s2 in samples if s2["count"]]
            _log("ok" if with_data else "warn",
                 "stream_raw: %d polls, %d con datos, %d tags" % (n, len(with_data), len(got_tags)))
            return jsonify(ok=True, start_ok=getattr(r, "last_start_ok", None),
                           polls=n, con_datos=len(with_data),
                           tags=got_tags[:20], samples=samples[:n])
        if what == "checkant":
            try:
                code = r._lib.CFHid_CheckAnt()
            except Exception as exc:  # noqa: BLE001
                return jsonify(ok=False, error="CheckAnt: " + str(exc))
            _log("info", "CheckAnt() -> %s" % code)
            return jsonify(ok=True, code=code)
        if what == "maintenance":
            # limpieza barata: cortar stream, vaciar buffer y volver a AnswerMode
            steps = []
            try:
                r.stop_read(); steps.append("StopRead")
            except Exception as exc:  # noqa: BLE001
                steps.append("stop: " + str(exc)[:30])
            try:
                r.clear_tag_buf(); steps.append("ClearTagBuf")
            except Exception:  # noqa: BLE001
                steps.append("clear: error")
            try:
                from ru5600_core import (DEV_ADDR as _DA, PARAM_WORKMODE as _PW,
                                         WORKMODE_ANSWER as _WA)
                r._pace(settle=True)
                r._lib.CFHid_SetDeviceOneParam(_DA, _PW, _WA)
                r._pace(settle=True)
                steps.append("AnswerMode")
            except Exception:  # noqa: BLE001
                steps.append("mode: error")
            _log("info", "mantenimiento: " + ", ".join(steps))
            return jsonify(ok=True, steps=steps)
        if what == "callback":
            stats = r.callback_stats()
            _log("info", "callback: %s" % stats)
            return jsonify(ok=True, callback=stats)
        return jsonify(ok=False, error="prueba desconocida: " + what)
    except Exception as exc:  # noqa: BLE001
        _log("error", "prueba %s: %s" % (what, exc))
        return jsonify(ok=False, error=str(exc))


def _direct_read(r):
    """Una sola tirada de lectura dirigida al banco EPC (ReadCardG2).

    La usan ``/api/read_until`` (una tirada por intento) y ``/api/bench``
    (una muestra por intento), asi el benchmark mide exactamente el mismo
    camino que la lectura repetida.  Devuelve ``(resultado, epc, hex, detalle)``
    con ``resultado`` en ``ok | stale | error``; ``detalle`` es el texto que
    el llamador pone en el log (o ``None`` cuando no hay nada que loguear).
    """
    try:
        # 8 palabras = CRC(2) + PC(2) + hasta 12 bytes de EPC
        data = r.read_card(1, 0, 8, b"\x00" * 4, retries=1)
        raw = data.hex().upper()
        # el PC declara el largo del EPC: bits 15-11 = palabras
        if len(data) >= 4:
            pc = (data[2] << 8) | data[3]
            epc_words = (pc >> 11) & 0x1F
            epc_len = epc_words * 2
            if 2 <= epc_len <= len(data) - 4:
                epc = data[4:4 + epc_len].hex().upper()
            else:
                epc = data[4:].hex().upper()
        else:
            epc = raw
        return "ok", epc, raw, None
    except ReaderError as exc:
        label = "stale" if "stale" in str(exc).lower() else "error"
        return label, None, None, label
    except Exception as exc:  # noqa: BLE001
        return "error", None, None, "error: " + str(exc)[:120]


@app.route("/api/read_until", methods=["POST"])
def api_read_until():
    """Lectura dirigida repetida hasta enganchar el tag del EPC.

    Los fallos del camino dirigido son en su mayoria ``stale``: el lector
    contesta con el frame de una orden anterior.  Por eso entre intento e
    intento se vacia el buffer del lector y se espera ``pause``.
    """
    cfg = request.get_json(silent=True) or {}
    try:
        attempts = int(cfg.get("attempts", 20))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="attempts invalido"), 400
    attempts = max(1, min(attempts, 100))
    try:
        pause = float(cfg.get("pause", 0.1))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="pause invalido"), 400
    pause = max(0.0, min(pause, 2.0))
    expected = re.sub(r"\s+", "", str(cfg.get("expected") or "")).upper() or None
    try:
        r = _need_usb()
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))

    t0 = time.monotonic()
    tries = []
    last_epc = None
    last_hex = None
    found = False
    for n in range(1, attempts + 1):
        a0 = time.monotonic()
        result, epc, raw, detail = _direct_read(r)
        if raw is not None:
            last_hex = raw
        if epc is not None:
            last_epc = epc
        if result == "ok":
            _log("ok", "read_until %d/%d ok: %s" % (n, attempts, last_hex))
        else:
            _log("warn", "read_until %d/%d %s" % (n, attempts, detail))
        tries.append({"n": n, "result": result,
                      "ms": round((time.monotonic() - a0) * 1000)})
        if result == "ok" and (expected is None or last_epc == expected):
            found = True
            break
        if n < attempts:
            try:
                r.clear_tag_buf()
            except Exception:  # noqa: BLE001
                pass
            if pause:
                time.sleep(pause)

    elapsed_ms = round((time.monotonic() - t0) * 1000)
    out = {"ok": True, "found": found, "epc": last_epc, "hex": last_hex,
           "rssi": None, "attempts_used": len(tries),
           "elapsed_ms": elapsed_ms, "tries": tries}
    if expected is not None:
        out["match"] = bool(found and last_epc == expected)
    # --- tarea 10: chip por TID (4 bytes de prefijo) ---
    if found:
        try:
            # TID completo: 12 bytes (modelo + serial unico)
            tid = r.read_card(2, 0, 6, b"\x00" * 4, retries=2)
            tid_hex = tid.hex().upper()
            out["tid"] = tid_hex
            # coherencia: en este lote el EPC termina con el serial del TID
            if len(tid_hex) >= 8 and last_epc and len(last_epc) >= 8:
                out["epc_tid_ok"] = (tid_hex[-8:] == last_epc[-8:])
                if not out["epc_tid_ok"]:
                    _log("warn", "EPC y TID no coinciden: epc=%s tid=%s (tag mal grabado o frame viejo)"
                         % (last_epc, tid_hex))
            prefix = tid_hex[:8]
            entry = TID_PREFIX_DB.get(prefix)
            if isinstance(entry, tuple) and len(entry) >= 2:
                chip = ("%s %s" % (entry[0], entry[1])).strip()
            else:
                chip = prefix
            out["chip"] = chip
            out["tid_prefix"] = prefix
        except Exception as exc:  # noqa: BLE001
            out["chip"] = None
            out["chip_error"] = str(exc)[:80]
    # --- tarea 9: RSSI (solo lo entrega el stream; se intenta si lo piden) ---
    if found and cfg.get("with_rssi"):
        try:
            tags = r.scan()
            hit = next((t for t in tags if (t.epc_hex or "").upper() == last_epc), None)
            if hit is not None:
                out["rssi"] = hit.rssi
        except Exception as exc:  # noqa: BLE001
            out["rssi_error"] = str(exc)[:80]

    # guardar recien ahora, con chip y rssi ya resueltos
    _store_result(last_epc if found else None, found, len(tries), elapsed_ms,
                  chip=out.get("chip"), rssi=out.get("rssi"))

    _log("ok" if found else "warn",
         "read_until: %s en %d/%d intentos (%d ms)"
         % (last_epc if found else "sin tag", len(tries), attempts, elapsed_ms))
    return jsonify(out)


# --- clonacion de EPC: el TID manda, el EPC obedece ------------------------
# El TID viene grabado de fabrica y no se puede cambiar: un clon se detecta
# por TID, no por EPC.  Por eso se compara el TID antes de escribir.
_clone_src = None


def _clone_probe(r, attempts=25, pause=0.1):
    """Lectura dirigida hasta enganchar un tag. ``(epc, raw)`` o ``(None, None)``."""
    for n in range(1, attempts + 1):
        result, epc, raw, _detail = _direct_read(r)
        if result == "ok" and epc:
            return epc, raw
        if n < attempts:
            try:
                r.clear_tag_buf()
            except Exception:  # noqa: BLE001
                pass
            if pause:
                time.sleep(pause)
    return None, None


def _read_tid(r):
    """TID completo y estable, leído dos veces.

    Medido en la tanda de H10: los 12 bytes son el TID real
    (E28038132000662B + serial 03A00BB1).  Los primeros 8 son iguales en todo
    el lote; el serial único está en los últimos 4.  Leer sólo 8 bytes hacía
    que dos tags distintos parecieran el mismo (bug detectado 2026-10-26).
    Se prueban 6, 4 y 2 palabras para tags con TID corto.
    """
    for words in (6, 4, 2):
        reads = []
        for _ in range(2):
            try:
                tid = r.read_card(2, 0, words, b"\x00" * 4, retries=3)
            except Exception:  # noqa: BLE001
                tid = None
            if not tid:
                reads = []
                break
            reads.append(bytes(tid).hex().upper())
            time.sleep(0.05)
        if reads and reads[0] == reads[1]:
            return reads[0]
    return None


def _tid_compare(src, dst):
    """``True``/``False`` si se puede decidir; ``None`` si el TID es muy corto."""
    if not src or not dst or len(src) < 15 or len(dst) < 15:
        return None
    n = min(len(src), len(dst))
    return src[:n] == dst[:n]


@app.route("/api/clone_read", methods=["POST"])
def api_clone_read():
    """Lee el tag origen (EPC + TID) y lo deja en memoria para clonar."""
    global _clone_src
    try:
        r = _need_usb()
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))
    try:
        epc, raw = _clone_probe(r)
        if not epc:
            _log("warn", "clone_read: sin tag a la vista")
            return jsonify(ok=False,
                           error="sin tag a la vista: poné el tag origen sobre la antena")
        tid = _read_tid(r)
        if not tid:
            return jsonify(ok=False,
                           error="leí el EPC %s pero no el TID: no puedo clonar sin TID" % epc)
        chip = _chip_from_prefix(tid)
        with _lock:
            _clone_src = {"epc": epc, "tid": tid, "chip": chip,
                          "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        _log("ok", "clone_read: EPC %s / TID %s (%s)" % (epc, tid, chip))
        return jsonify(ok=True, epc=epc, tid=tid, chip=chip, rssi=None,
                       raw=raw, note="rssi: la lectura dirigida no lo entrega")
    except Exception as exc:  # noqa: BLE001
        _log("error", "clone_read: %s" % exc)
        return jsonify(ok=False, error="no pude leer el origen: " + str(exc))


@app.route("/api/clone_write", methods=["POST"])
def api_clone_write():
    """Escribe el EPC del origen en el tag destino, previa verificacion de TID."""
    src = _clone_src
    if not src:
        return jsonify(ok=False, error="primero leé el tag origen")
    try:
        r = _need_usb()
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))
    try:
        cur_epc, _raw = _clone_probe(r)
        if not cur_epc:
            return jsonify(ok=False,
                           error="sin tag a la vista: poné el tag destino sobre la antena")
        tid_dst = _read_tid(r)
        if not tid_dst:
            return jsonify(ok=False,
                           error="no pude leer el TID del tag destino: no escribo sin verificar")
        same = _tid_compare(src["tid"], tid_dst)
        if same is None:
            return jsonify(ok=False,
                           error="el TID del destino es demasiado corto para distinguirlo del origen (%s): no escribo a ciegas" % tid_dst)
        if same:
            _log("warn", "clone_write: el tag presente es el origen (TID %s)" % tid_dst)
            return jsonify(ok=False,
                           error="el tag presente es el ORIGEN (mismo TID %s): poné el tag destino" % tid_dst)
        epc_src = bytes.fromhex(src["epc"])
        try:
            r.write_epc(epc_src, b"\x00" * 4, verify=True)
        except Exception as exc:  # noqa: BLE001
            _log("error", "clone_write: %s" % exc)
            try:
                store.add_trabajo("clone", "error", total=1, hechos=0,
                                  extra={"origen": src, "tid_destino": tid_dst,
                                         "error": str(exc)[:200]})
            except Exception:  # noqa: BLE001
                pass
            return jsonify(ok=False, escrito=False, verificado=False,
                           tid_destino=tid_dst,
                           error="no pude escribir el EPC: " + str(exc))
        epc_after, _raw2 = _clone_probe(r, attempts=10)
        verificado = (epc_after or "").upper() == src["epc"].upper()
        _log("ok" if verificado else "warn",
             "clone_write: EPC %s en TID %s (%s)"
             % (src["epc"], tid_dst, "verificado" if verificado else "NO verificado"))
        try:
            store.add_trabajo("clone", "ok" if verificado else "warn",
                              total=1, hechos=1 if verificado else 0,
                              extra={"origen": src, "tid_destino": tid_dst,
                                     "epc_destino_previo": cur_epc,
                                     "epc_leido": epc_after,
                                     "verificado": verificado,
                                     "nota": "solo EPC: no se tocó USER ni contraseñas"})
            store.add_log("ok", "clone: %s -> TID %s (%s)"
                          % (src["epc"], tid_dst,
                             "verificado" if verificado else "no verificado"))
        except Exception as exc:  # noqa: BLE001
            _log("warn", "clone_write: no pude guardar en SQLite: %s" % exc)
        return jsonify(ok=True, escrito=True, verificado=verificado,
                       tid_destino=tid_dst, epc_origen=src["epc"],
                       epc_destino_previo=cur_epc, epc_leido=epc_after,
                       origen=src)
    except Exception as exc:  # noqa: BLE001
        _log("error", "clone_write: %s" % exc)
        return jsonify(ok=False, error="clone_write falló: " + str(exc))


@app.route("/api/bench", methods=["POST"])
def api_bench():
    """Benchmark de antena: N lecturas dirigidas, una sola tirada cada una.

    Cada muestra usa el mismo camino que ``/api/read_until`` con
    ``attempts=1``: si engancha, cuenta como acierto.  La tasa de enganche es
    el indicador de calidad de antena/posicion (el SDK no expone RSSI en la
    lectura dirigida).  Una fila en ``trabajos`` deja la medicion en SQLite.
    """
    cfg = request.get_json(silent=True) or {}
    try:
        trials = int(cfg.get("trials", 20))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="trials invalido"), 400
    trials = max(1, min(trials, 200))
    try:
        pause = float(cfg.get("pause", 0.1))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="pause invalido"), 400
    pause = max(0.0, min(pause, 2.0))
    label = str(cfg.get("label") or "").strip()[:80]
    try:
        r = _need_usb()
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))

    ms_list = []
    epcs = {}
    hits = 0
    for n in range(1, trials + 1):
        a0 = time.monotonic()
        result, epc, _raw, _detail = _direct_read(r)
        ms_list.append(round((time.monotonic() - a0) * 1000))
        if result == "ok":
            hits += 1
            if epc:
                epcs[epc] = epcs.get(epc, 0) + 1
        if n < trials:
            try:
                r.clear_tag_buf()
            except Exception:  # noqa: BLE001
                pass
            if pause:
                time.sleep(pause)

    ordered = sorted(ms_list)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        ms_median = ordered[mid]
    else:
        ms_median = round((ordered[mid - 1] + ordered[mid]) / 2.0, 1)
    tasa = round(hits / trials, 4)
    top_epc = max(epcs, key=epcs.get) if epcs else None
    out = {"ok": True, "label": label, "trials": trials, "hits": hits,
           "tasa": tasa, "ms_min": ordered[0], "ms_mediana": ms_median,
           "ms_max": ordered[-1], "epc": top_epc, "epcs": epcs,
           "intentos_por_hit": 1}
    _log("ok" if hits else "warn",
         "bench %s: %d/%d enganchados (%.1f%%), %s / %s / %s ms"
         % (label or "sin etiqueta", hits, trials, tasa * 100,
            ordered[0], ms_median, ordered[-1]))
    try:
        store.add_trabajo("bench", "ok", total=trials, hechos=hits,
                          extra={"label": label, "tasa": tasa,
                                 "ms_min": ordered[0],
                                 "ms_mediana": ms_median,
                                 "ms_max": ordered[-1],
                                 "epc": top_epc, "epcs": epcs,
                                 "pause": pause})
    except Exception as exc:  # noqa: BLE001
        _log("warn", "bench: no pude guardar en SQLite: %s" % exc)
    return jsonify(out)


def _store_result(epc, found, attempts, elapsed_ms, chip=None, rssi=None):
    """Guarda el resultado; si SQLite falla, la lectura sigue igual."""
    try:
        if found:
            store.upsert_tag(epc, chip, rssi, attempts)
            store.add_lectura(epc, rssi, "", attempts, elapsed_ms, "read_until")
        store.add_log("ok" if found else "warn",
                      "read_until %s (%d intentos, %d ms)"
                      % (epc if found else "sin tag", attempts, elapsed_ms))
    except Exception as exc:  # noqa: BLE001
        _log("warn", "no pude guardar en SQLite: %s" % exc)


@app.route("/api/tags")
def api_tags():
    """Tags guardados, el mas reciente primero."""
    try:
        limit = int(request.args.get("limit", 200) or 200)
    except (TypeError, ValueError):
        limit = 200
    try:
        rows = store.list_tags(limit)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))
    return jsonify(ok=True, count=len(rows), tags=rows)


@app.route("/api/tags/stats")
def api_tags_stats():
    try:
        out = store.stats()
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))
    out["ok"] = True
    return jsonify(out)


@app.route("/api/tags/clear", methods=["POST"])
def api_tags_clear():
    try:
        deleted = store.clear_tags()
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))
    _log("info", "tags guardados vaciados: %d tags, %d lecturas"
         % (deleted["tags"], deleted["lecturas"]))
    return jsonify(ok=True, deleted=deleted)


@app.route("/api/scan_now")
def api_scan_now():
    """Un escaneo puntual (answer mode, sin escritura). Sirve para diagnostico."""
    try:
        r = _need_usb()
        tags = r.scan()
        return jsonify(ok=True, count=len(tags),
                       tags=[{"epc": t.epc_hex, "ant": t.ant, "rssi": t.rssi}
                             for t in tags])
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/compare", methods=["POST"])
def api_compare():
    """Compara los tags del ultimo barrido contra una planilla y devuelve solo los que estan."""
    up = request.files.get("file")
    if up is None:
        return jsonify(ok=False, error="falta la planilla")
    try:
        col = int(request.form.get("column", "0") or 0)
    except (TypeError, ValueError):
        col = 0
    mode = (request.form.get("mode") or "auto").lower()
    tags = list(_state["tags"])
    if not tags:
        return jsonify(ok=False, error="primero hacé un barrido")

    name = (up.filename or "").lower()
    raw_rows = []
    try:
        if name.endswith((".xlsx", ".xlsm")):
            from openpyxl import load_workbook
            wb = load_workbook(up, data_only=True, read_only=True)
            for row in wb.active.iter_rows(values_only=True):
                if row and any(v not in (None, "") for v in row):
                    raw_rows.append(row)
        else:
            import csv as _csv
            for row in _csv.reader(io.StringIO(up.read().decode("utf-8-sig", "replace"))):
                if row and any(str(v).strip() for v in row):
                    raw_rows.append(row)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error="no pude leer la planilla: " + str(exc))

    values = []
    for i, row in enumerate(raw_rows, start=1):
        if col < len(row):
            val = _norm_epc(row[col])
            if val:
                values.append((i, val))

    def hit(epc, val):
        w34 = epc[6:14] if len(epc) >= 14 else ""
        if mode in ("epc", "auto") and val == epc:
            return True
        if mode in ("w34", "auto") and w34 and (val == w34 or val.endswith(w34)):
            return True
        return False

    matches, seen = [], set()
    for t in tags:
        epc = (t.get("epc") or "").upper()
        if not epc or epc in seen:
            continue
        for row_n, val in values:
            if hit(epc, val):
                seen.add(epc)
                matches.append({"epc": epc, "chip": t.get("chip"),
                                "veces": t.get("n", 1),
                                "fila": row_n, "valor": val})
                break

    return jsonify(ok=True, total=len(tags), sheet_rows=len(values),
                   matches=matches, found=len(matches))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, threaded=True)
