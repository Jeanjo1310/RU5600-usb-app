"""Barrido web P2 — piloto Flask + Flowbite/Tailwind (Creador 2026-10-26).

Reutiliza ru5600_core tal cual (USB abre en el servidor, misma maquina).
Sin npm por ahora: Tailwind Play CDN + Flowbite CDN (piloto dev;
produccion compila con @tailwindcss/cli segun la guia oficial).
"""
from __future__ import annotations

import io
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from flask import Flask, jsonify, render_template, request, send_file

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "src"))

from ru5600_chip import TID_PREFIX_DB  # noqa: E402
from ru5600_core import Ru5600Reader, interpret_active_blob  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
EXCEL = os.path.normpath(os.path.join(BASE, "..", "..", "CONOCIMIENTO",
                                      "TAGS EPC.xlsx"))

_pool = ThreadPoolExecutor(max_workers=8)


def _guarded(fn, timeout=40):
    """Hardware con techo: en vez de colgar el navegador, error util."""
    fut = _pool.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except TimeoutError:
        raise RuntimeError("timeout: el lector no responde, reboot + reintentar")


app = Flask(__name__)
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
    return render_template("avanzado.html")


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
    cfg = request.get_json(silent=True) or {}
    try:
        r = _need_usb()
        if "rf" in cfg:
            r.set_rf_power(int(cfg["rf"]))
        if "mode" in cfg:
            r.set_work_mode(int(cfg["mode"]))
        if "beep" in cfg:
            r.set_beep(bool(cfg["beep"]))
        if "filter" in cfg:
            r.set_filter_time(int(cfg["filter"]))
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


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
    try:
        _guarded(_do, timeout=120)
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


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
        if t == "net":
            from ru5600_net import NetReader
            _net_reader = NetReader(cfg.get("ip", "192.168.1.250"),
                                    int(cfg.get("port", 60000)))
            _net_reader.open()
            _net_info["mode"] = "net"
        else:
            _net_info["mode"] = t
            _get_reader()
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


def _get_reader():
    global _reader
    if _reader is None or not _reader.is_open:
        _reader = Ru5600Reader()
        _reader.open()
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
        r._lib.CFHid_StartRead(DEV_ADDR)
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
                r._lib.CFHid_StopRead(DEV_ADDR)
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
        return jsonify(ok=True,
                       answered=bool(r.raw_command(cmd, payload, timeout)))
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
        if len(data) != 32:
            return jsonify(ok=False, error="32 bytes hex")
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


@app.route("/barrido")
def barrido():
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
    app.run(host="127.0.0.1", port=8000, threaded=True)
