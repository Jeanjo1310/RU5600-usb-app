"""Barrido web P2 — piloto Flask + Flowbite/Tailwind (Creador 2026-10-26).

Reutiliza ru5600_core tal cual (USB abre en el servidor, misma maquina).
Sin npm por ahora: Tailwind Play CDN + Flowbite CDN (piloto dev;
produccion compila con @tailwindcss/cli segun la guia oficial).
"""
from __future__ import annotations

import collections
import io
import json
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

# Modo de lectura (Conexión): ritmo del CAMINO DIRIGIDO hasta que exista la dirección
# de ReadTime (TAREA 1 del backlog). Balanceado = el ritmo medido 2026-10-26 (120 ms
# entre lecturas). Ráfaga acelera y Confiable suma reintentos: son pausas NUESTRAS,
# no un parámetro del lector.
_READ_MODES = {
    "burst":    {"label": "Ráfaga",     "pause": 0.03, "retries": 1},
    "balanced": {"label": "Balanceado", "pause": 0.12, "retries": 1},
    "reliable": {"label": "Confiable",  "pause": 0.25, "retries": 2},
}
_read_cfg: dict = {"mode": "balanced"}


def _read_pace() -> dict:
    return _READ_MODES.get(_read_cfg.get("mode"), _READ_MODES["balanced"])


# Perfiles de lectura (2026-10-26): combinaciones MEDIDAS entre el parametro real del
# lector (WorkMode) y el camino de la app (modo de lectura + camino del barrido).
# Un perfil cambia las tres cosas juntas para que lector e interfaz coincidan.
PROFILES = {
    "rafaga": {
        "label": "Inventario rápido",
        "desc": "Stream a máxima velocidad (ActiveMode + 30 ms entre tiradas). Para barrer muchos tags que streamean. Medido: 10/10 tags en 24,1 s.",
        "workmode": 1,           # ActiveMode: el lector streamea solo
        "read_mode": "burst",    # 30 ms entre tiradas dirigidas
        "sweep": "active",       # barrido por stream
        "evidence": "10/10 tags en 24,1 s (mejor corrida medida)",
    },
    "general": {
        "label": "Uso general",
        "desc": "AnswerMode con stream (120 ms). El equilibrio de todos los días: el modo lo elegís vos (perfil o Barrido, en caliente).",
        "workmode": 0,           # AnswerMode
        "read_mode": "balanced",  # 120 ms (el ritmo medido)
        "sweep": "active",       # stream; el cambio a dirigido es manual
        "evidence": "modo stream por defecto; cambio en caliente desde Barrido",
    },
    "dedicado": {
        "label": "Lectura dedicada",
        "desc": "Fuerza la lectura dirigida, sin stream: para H10, UCODE 8/9 y tags de a uno. Medido: 10/10 en las 5 familias de tags (39-108 ms).",
        "workmode": 0,
        "read_mode": "balanced",
        "sweep": "answer",       # fuerza el camino dirigido (sin stream)
        "evidence": "dirigido 10/10 en las 5 familias de tags (39-108 ms)",
    },
    "continuo": {
        "label": "Continuo",
        "desc": "ActiveMode permanente: para Lectura continua, Distancia y mover tags con el stream corriendo (12-13 lecturas/s, RSSI 45).",
        "workmode": 1,           # ActiveMode, para Lectura continua / Distancia
        "read_mode": "balanced",
        "sweep": "active",
        "evidence": "stream 12-13 lecturas/s con RSSI 45 en tags pegados",
    },
    "pausado": {
        "label": "Pausado (cuidado del lector)",
        "desc": "El más lento: 250 ms y 2 reintentos por tirada, la menor carga de comandos. Para escritura y verificación largas.",
        "workmode": 0,
        "read_mode": "reliable",  # 250 ms + 2 reintentos: la menor carga
        "sweep": "answer",
        "evidence": "escritura y verificacion largas sin martillar al lector",
    },
}
_sweep_cfg: dict = {"mode": "active"}
_profile_cfg: dict = {"current": "general", "base": "general", "changed": {}}
# etiquetas legibles de los parametros que un perfil NO maneja (para el estado "personalizado")
_PARAM_LBL = {"rf": "RF Power", "mode": "WorkMode", "beep": "Buzzer", "filter": "FilterTime"}
CUSTOM = {
    "id": "custom",
    "label": "Personalizado",
    "desc": ("Mezcla propia: tus cambios encima de un perfil, sin perder el resto de la "
             "configuracion. Se marca solo cuando Aplicar cambia algo que el perfil no "
             "define (por ejemplo RF Power para leer mas cerca). Elegir un perfil de la "
             "lista lo reemplaza."),
    "applyable": False,
}
_lock = threading.Lock()
_stop = threading.Event()
_state = {"running": False, "tags": [], "seen": {}, "started": 0.0,
          "error": ""}   # "seen" es dict epc->info: el bucle hace .get() y [key]=

# --- reconexión automática en procesos largos --------------------------------
# Si el lector se cae EN MEDIO de un barrido / lote / dist / lectura continua,
# el proceso NO se corta: espera la reconexión dentro de la ventana "Sin tags
# nuevos" y sigue. Al detectar la caída marcamos _open=False enseguida: el
# estado del lector nunca miente (Pruebas > Estado del lector muestra
# open:false + reconnect mientras espera).
_reconn = {"active": False, "since": 0.0, "left": 0.0, "tries": 0,
           "last_ok": 0.0, "since_h": ""}

# avisos que el SDK del vendor imprime con puts() a STDOUT (medidos en
# libCFHidApi.so 2026-10-26): "open device success" / "unable to open device" /
# "Hid Disconnect". Son la voz del lector — el watcher las capta en el momento
# y las registra en el log de la app (antes caian al logfile y nadie las leia).
_VENDOR_MSGS = {
    "Hid Disconnect": ("error", "el vendor avisa: Hid Disconnect "
                                "(el hilo de recepcion detectó que el USB se fue)"),
    "unable to open device": ("warn", "el vendor avisa: unable to open device"),
    "open device success": ("ok", "el vendor avisa: open device success"),
}
_hid_events = []    # ultimos avisos del vendor con timestamp (para el JSON)


def _line_buffer_stdout():
    """glibc bufferea stdout COMPLETO cuando el fd 1 es un pipe (no una tty):
    los puts() del vendor (ej. "Hid Disconnect") se quedarian pegados en el
    buffer de C hasta 4 KB o hasta cerrar el proceso. Poner stdout en modo
    line-buffered hace que cada puts (que agrega \n) se flushee al instante
    y el watcher lo vea en el momento."""
    import ctypes as _ct
    try:
        libc = _ct.CDLL(None)
        stdout_var = _ct.c_void_p.in_dll(libc, "stdout")   # FILE* de glibc
        libc.setvbuf.argtypes = [_ct.c_void_p, _ct.c_void_p,
                                 _ct.c_int, _ct.c_size_t]
        libc.setvbuf(stdout_var.value, None, 2, 0)          # 2 = _IOLBF
    except Exception:  # noqa: BLE001 - sin glibc o sin el simbolo: best-effort
        pass


def _install_vendor_stdout_watcher():
    """Captura los puts() del SDK del vendor sin perder el log: fd 1 -> pipe,
    un hilo lee, reenvia al fd original y registra los avisos conocidos."""
    try:
        _line_buffer_stdout()
        r_fd, w_fd = os.pipe()
        orig_fd = os.dup(1)
        os.dup2(w_fd, 1)
        os.close(w_fd)

        def _loop():
            buf = b""
            try:
                while True:
                    chunk = os.read(r_fd, 4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            os.write(orig_fd, line + b"\n")
                        except OSError:
                            pass
                        text = line.decode("utf-8", "replace").strip()
                        for key, (lvl, msg) in _VENDOR_MSGS.items():
                            if key in text:
                                _log(lvl, msg)
                                _hid_events.append(
                                    {"ts": time.time(), "msg": key})
                                del _hid_events[:-20]
                                break
            except OSError:
                pass

        threading.Thread(target=_loop, daemon=True).start()
        _log("info", "watcher del SDK activo: los avisos del vendor "
                     "(Hid Disconnect, etc.) se registran en este log")
    except Exception as exc:  # noqa: BLE001
        _log("warn", "no pude instalar el watcher del SDK: %s" % exc)


def _reconn_window():
    """Ventana de espera = la misma 'Sin tags nuevos' del Barrido (120 default)."""
    try:
        return max(5.0, min(600.0, float(_state.get("stall_after") or 120.0)))
    except (TypeError, ValueError):
        return 120.0


def _link_dead(r):
    """True cuando el lector dejó de contestar. NO confundir con "no hay tags":
    un campo vacío sigue contestando; un USB muerto no.

    Tres senales del PROPIO vendor (ingenieria inversa del libCFHidApi.so,
    2026-10-26), de mas barata a mas cara:
      1. handle interno NULL: el hilo de recepcion del .so lo pone en NULL
         cuando hid_read() falla (el "LED apagado" leido del byte que
         SendCommand chequea antes de cada comando). Cero costo, sin USB.
      2. trans_state() (cmd 0x2D): comando de estado — 0 si no contesta.
      3. read_param(0x00): el test classico, con doble intento (300 ms) para
         no cortar un stream sano por un transitorio del firmware."""
    if r is None:
        return True
    try:
        if not r.is_open:
            return True
    except Exception:  # noqa: BLE001
        return True
    # 1. handle interno del vendor (instantaneo, sin trafico USB)
    try:
        if r.hid_handle_ptr() == 0:
            return True
    except Exception:  # noqa: BLE001
        pass
    # 2. comando de estado del firmware: solo cuenta como muerte si NO
    #    contesta (None). Un byte 0 es estado legal del firmware — confundirlos
    #    declararia muerto a un lector sano (bug corregido el 2026-10-26).
    try:
        if r.trans_state() is None:
            return True
    except Exception:  # noqa: BLE001
        pass
    # 3. sonda de parametro con reintento
    for _i in range(2):
        try:
            r.read_param(0x00)          # ReadDeviceOneParam: sonda barata
            return False
        except Exception:               # noqa: BLE001
            time.sleep(0.3)
    return True


def _await_reconnect(stop_ev=None):
    """Espera de reconexión: reintentá cada ~1.5 s hasta agotar la ventana
    'Sin tags nuevos'. Tras un reinicio el lector vuelve con OTRO número USB,
    y open() ya lo reubica por VID:PID. Devuelve True si volvió."""
    global _reader
    window = _reconn_window()
    deadline = time.monotonic() + window
    import datetime as _dt
    _reconn.update(active=True, since=time.time(), left=window, tries=0,
                   since_h=_dt.datetime.now().strftime("%H:%M:%S"))
    _log("warn", "lector desconectado: esperando reconexión (ventana %.0f s)"
         % window)
    # el estado no mienta mientras espera: cae open y se suelta el dispositivo
    if _reader is not None:
        try:
            _reader._open = False
            with _reader._lock:
                try:
                    _reader._lib.CFHid_CloseDevice()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    ok = False
    try:
        while time.monotonic() < deadline:
            if stop_ev is not None and stop_ev.is_set():
                return False
            _reconn["left"] = max(0.0, deadline - time.monotonic())
            _reconn["tries"] += 1
            try:
                if _reader is None:
                    _reader = Ru5600Reader()
                _reader.open(attempts=1, retry_delay=0)
                ok = True
                break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5)
    finally:
        _reconn.update(active=False, left=0.0, last_ok=time.time())
    if ok:
        _log("ok", "lector reconectado tras %d intentos" % _reconn["tries"])
    return ok


def _rearm_after_reconnect(r, active):
    """Tras volver, repone el modo del proceso interrumpido: stream
    (ActiveMode + Start) o AnswerMode para el camino dirigido."""
    from ru5600_core import (DEV_ADDR, PARAM_WORKMODE, WORKMODE_ACTIVE,
                             WORKMODE_ANSWER)
    r._pace()
    with r._lock:
        r._lib.CFHid_SetDeviceOneParam(
            DEV_ADDR, PARAM_WORKMODE,
            WORKMODE_ACTIVE if active else WORKMODE_ANSWER)
    r._pace(settle=True)
    with r._lock:
        r._lib.CFHid_ClearTagBuf()
    r._pace()
    if active:
        with r._lock:
            r._lib.CFHid_StartRead()
        r._pace(settle=True)


def _reconn_or_die(r, direct, stop_ev=None):
    """Rutina del barrido al detectar la caída: espera la reconexión y repone
    el modo. Devuelve el reader vivo, o None si no volvió (o si el usuario
    cortó) — el caller debe adoptar la referencia devuelta."""
    if not _await_reconnect(stop_ev):
        return None
    rr = _reader
    try:
        _rearm_after_reconnect(rr, not direct)
    except Exception as exc:  # noqa: BLE001
        _log("warn", "no pude re-armear tras reconectar: %s" % exc)
        return None
    return rr


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


_install_vendor_stdout_watcher()


@app.route("/conexion")
def conexion():
    return render_template("conexion.html")


@app.after_request
def _sin_cache_paginas(resp):
    """Nada de cache en el navegador para HTML/CSS: los templates se editan en
    caliente y la pagina quedaba vieja (contador viejo, 2026-10-26)."""
    ct = resp.content_type or ""
    if "text/html" in ct or "text/css" in ct:
        resp.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/conn_state")
def conn_state():
    global _reader, _net_reader
    if _reader is not None and _reader.is_open:
        info = {}
        if _reader._lock.acquire(blocking=False):
            try:
                si = _reader.system_info()
                info = {"sn": si.device_sn, "fw": si.soft_ver,
                        "hw": si.hard_ver}
            except Exception:  # noqa: BLE001
                info = {}
            finally:
                _reader._lock.release()
        else:
            info = {"busy": True}
        return jsonify(open=True, mode="usb", info=info,
                       read_mode=_read_cfg.get("mode", "balanced"),
                       profile=_profile_cfg.get("current", "general"),
                       reconnect=dict(_reconn),
                       sweep=_sweep_cfg.get("mode", "active"))
    if _net_reader is not None and _net_reader.is_open():
        return jsonify(open=True, mode="net", info={})
    return jsonify(open=False, mode=_net_info.get("mode", "usb"), info={},
                   read_mode=_read_cfg.get("mode", "balanced"),
                   profile=_profile_cfg.get("current", "general"),
                   reconnect=dict(_reconn),
                   sweep=_sweep_cfg.get("mode", "active"))


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
    # valores ANTES de escribir: lo que quede distinto a esto lo vuelve "Personalizado"
    # sin perder el resto de la configuracion (ej.: bajar RF Power para leer de cerca)
    pre = {}
    for key, getter in (("rf", r.rf_power), ("mode", r.work_mode),
                        ("beep", r.beep), ("filter", r.filter_time)):
        try:
            pre[key] = getter()
        except Exception:  # noqa: BLE001 - si no se lee, no se compara
            pass
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
    # cambios manuales: cualquier campo escrito distinto de lo que habia = Personalizado
    changed = {}
    for key, info in applied.items():
        if key in pre and int(pre[key]) != int(info.get("got", pre[key])):
            changed[_PARAM_LBL.get(key, key)] = {"de": pre[key], "a": info.get("got")}
    prof_state = None
    if changed:
        _profile_cfg["current"] = "custom"
        _profile_cfg["changed"] = changed
        _log("info", "perfil personalizado: cambiaste %s" %
             ", ".join("%s %s->%s" % (k, v["de"], v["a"]) for k, v in changed.items()))
        prof_state = {"id": "custom", "base": _profile_cfg.get("base"), "changed": changed}
    return jsonify(ok=not failed, applied=applied, failed=failed, profile=prof_state)


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
        with r._lock:
            r._lib.CFHid_RebootDevice(0xFF)
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/open", methods=["POST"])
def api_open():
    global _reader, _net_reader
    cfg = request.get_json(silent=True) or {}
    t = cfg.get("transport", "usb")
    rm = str(cfg.get("read_mode") or "").lower()
    if rm in _READ_MODES:
        _read_cfg["mode"] = rm
        # apertura con un modo distinto al del perfil: la mezcla pasa a Personalizado
        # (mismo criterio que POST /api/read_mode, para que el perfil nunca mienta)
        base_prof = PROFILES.get(_profile_cfg.get("base") or "")
        if base_prof and base_prof["read_mode"] != rm:
            _profile_cfg["current"] = "custom"
            _profile_cfg["changed"] = dict(_profile_cfg.get("changed") or {}, **{
                "lectura": {"de": _READ_MODES[base_prof["read_mode"]]["label"],
                            "a": _READ_MODES[rm]["label"]}})
        _log("info", "modo de lectura: %s (pausa %.0f ms, reintentos %d)" % (
            _READ_MODES[rm]["label"], _READ_MODES[rm]["pause"] * 1000,
            _READ_MODES[rm]["retries"]))
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


@app.route("/api/profiles")
def api_profiles():
    """Lista de perfiles de lectura y cual esta activo."""
    cur = _profile_cfg.get("current", "general")
    base = _profile_cfg.get("base", "general")
    changed = _profile_cfg.get("changed", {}) or {}
    return jsonify(ok=True, current=cur, base=base, changed=changed,
                   base_label=(PROFILES.get(base) or {}).get("label", base),
                   sweep=_sweep_cfg.get("mode", "active"),
                   read_mode=_read_cfg.get("mode", "balanced"),
                   profiles=[{"id": pid, "applyable": True, **cfg}
                             for pid, cfg in PROFILES.items()] + [CUSTOM])


@app.route("/api/profiles/apply", methods=["POST"])
def api_profile_apply():
    """Aplica un perfil: WorkMode del lector + camino de lectura de la app."""
    cfg = request.get_json(silent=True) or {}
    pid = str(cfg.get("id") or "").lower()
    if pid not in PROFILES:
        return jsonify(ok=False, error="perfil desconocido: " + pid), 400
    prof = PROFILES[pid]
    out = {"profile": pid, "reader": None, "app": {}}

    # lector: WorkMode (unico param real que toca un perfil; verificar releyendo)
    try:
        r = _need_usb()
        r.set_work_mode(int(prof["workmode"]))
        got = r.work_mode()
        out["reader"] = {"want": int(prof["workmode"]), "got": int(got),
                         "ok": int(got) == int(prof["workmode"])}
    except Exception as exc:  # noqa: BLE001
        out["reader"] = {"error": str(exc)[:120]}

    # app: modo de lectura (pausas) + camino por defecto del barrido
    _read_cfg["mode"] = prof["read_mode"]
    _sweep_cfg["mode"] = prof["sweep"]
    _profile_cfg.update(current=pid, base=pid, changed={})
    out["app"] = {"read_mode": prof["read_mode"], "sweep": prof["sweep"],
                  "pause_ms": _READ_MODES[prof["read_mode"]]["pause"] * 1000,
                  "retries": _READ_MODES[prof["read_mode"]]["retries"]}
    _log("info", "perfil '%s' aplicado: WorkMode=%s, modo de lectura=%s, barrido=%s" % (
        pid, prof["workmode"], prof["read_mode"], prof["sweep"]))
    return jsonify(ok=True, **out)


@app.route("/api/read_mode", methods=["POST"])
def api_read_mode():
    """Cambia el modo de lectura (Ráfaga/Balanceado/Confiable) sin reabrir el lector."""
    cfg = request.get_json(silent=True) or {}
    rm = str(cfg.get("mode") or "").lower()
    if rm not in _READ_MODES:
        return jsonify(ok=False, error="modo desconocido: " + rm), 400
    _read_cfg["mode"] = rm
    pace = _READ_MODES[rm]
    # si el modo elegido no es el del perfil base, la mezcla pasa a Personalizado
    # (y si ya era personalizado, el cambio se anota a lo que ya tenia)
    base_prof = PROFILES.get(_profile_cfg.get("base") or "")
    if base_prof and base_prof["read_mode"] != rm:
        _profile_cfg["current"] = "custom"
        _profile_cfg["changed"] = dict(_profile_cfg.get("changed") or {}, **{
            "lectura": {"de": _READ_MODES[base_prof["read_mode"]]["label"],
                        "a": _READ_MODES[rm]["label"]}})
    _log("info", "modo de lectura: %s (pausa %.0f ms, reintentos %d)" % (
        pace["label"], pace["pause"] * 1000, pace["retries"]))
    return jsonify(ok=True, read_mode=rm, label=pace["label"],
                   pause_ms=pace["pause"] * 1000, retries=pace["retries"])


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


_active = {"running": False, "tags": [], "seen": set(), "mode": "active"}
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
    cfg = request.get_json(silent=True) or {}
    mode = (cfg.get("mode") or "active").lower()
    if mode not in ("active", "answer"):
        mode = "active"
    with _lock:
        if _active["running"]:
            return jsonify(ok=False, error="ya corriendo")
        # El listado NO se borra al parar/leer de nuevo: se acumula hasta
        # que el usuario aprieta Limpiar (boton dedicado).
        _active.update(running=True, mode=mode)
        _active_stop.clear()
    threading.Thread(target=_active_run, args=(mode,), daemon=True).start()
    return jsonify(ok=True, mode=mode)


@app.route("/api/active_clear", methods=["POST"])
def api_active_clear():
    with _lock:
        if _active["running"]:
            return jsonify(ok=False, error="pará la lectura primero"), 409
        _active.update(tags=[], seen=set())
    return jsonify(ok=True)


@app.route("/api/active_stop", methods=["POST"])
def api_active_stop():
    _active_stop.set()
    return jsonify(ok=True)


@app.route("/api/active_tags")
def api_active_tags():
    return jsonify(running=_active["running"], tags=_active["tags"])


def _active_run_directed(r):
    """Lectura dirigida continua: de a un tag hasta Parar (H10, UCODE...).

    Sin objetivos ni topes: cada tag nuevo crea fila, el repetido suma.
    El ritmo lo decide el modo de lectura (Conexion): Rafaga 30 ms,
    Balanceado 120 ms, Confiable 250 ms + reintentos.
    """
    from ru5600_chip import TID_PREFIX_DB
    last_chk_d = 0.0
    try:
        while not _active_stop.is_set():
            res, epc_d, hex_d, det = _direct_read(r)
            if res != "ok" or not epc_d:
                if res == "error" and time.monotonic() - last_chk_d > 3.0:
                    last_chk_d = time.monotonic()
                    if _link_dead(r):
                        if not _await_reconnect(_active_stop):
                            return
                        continue
                time.sleep(_read_pace()["pause"])
                continue
            key = epc_d.upper()
            if key in _active["seen"]:
                for row in _active["tags"]:
                    if row.get("key", row.get("epc")) == key:
                        row["n"] += 1
                time.sleep(_read_pace()["pause"])
                continue
            info = {"key": key, "raw": (hex_d or "")[:48], "epc": key,
                    "tid": "", "chip": "", "ant": 0, "rssi": None, "n": 1}
            for _attempt in range(2):
                try:
                    tid = r.read_card(2, 0, 2, b"\x00" * 4, retries=2)
                    prefix = tid.hex().upper()[:8]
                    ent = TID_PREFIX_DB.get(prefix)
                    info["chip"] = (("%s %s" % (ent[0], ent[1])).strip()
                                     if isinstance(ent, tuple) and len(ent) >= 2
                                     else prefix)
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(0.12)
            if not info["chip"]:
                info["chip"] = "interpreter pending"
            _active["seen"].add(key)
            _active["tags"].append(info)
            time.sleep(_read_pace()["pause"])
    finally:
        _active["running"] = False


def _active_run(mode="active"):
    try:
        r = _get_reader()
        from ru5600_core import DEV_ADDR, PARAM_WORKMODE, WORKMODE_ACTIVE, WORKMODE_ANSWER
        want_active = (mode != "answer")
        r._pace()
        with r._lock:
            r._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                            WORKMODE_ACTIVE if want_active else WORKMODE_ANSWER)
        r._pace(settle=True)
        if not want_active:
            _active_run_directed(r)
            return
        with r._lock:
            r._lib.CFHid_ClearTagBuf()
        r._pace()
        with r._lock:
            r._lib.CFHid_StartRead()
        r._pace(settle=True)
        try:
            last_hit_a = time.monotonic()
            last_chk_a = 0.0
            while not _active_stop.is_set():
                import time as _t
                _t.sleep(0.1)
                tags_a = r.poll_tags()
                if tags_a:
                    last_hit_a = time.monotonic()
                # lector caido en medio de la lectura continua: esperar la
                # reconexion (mismo window del Barrido) y seguir, sin cortar
                elif (time.monotonic() - last_hit_a > 6
                      and time.monotonic() - last_chk_a > 3.0):
                    last_chk_a = time.monotonic()
                    if _link_dead(r):
                        if not _await_reconnect(_active_stop):
                            raise RuntimeError(
                                "lector desconectado: no volvió en la ventana "
                                "de reconexión")
                        r = _reader
                        _rearm_after_reconnect(r, True)
                        last_hit_a = time.monotonic()
                for tag in tags_a:
                    raw = (bytes(tag.raw_id) if tag.raw_id
                           else bytes(tag.epc))
                    # EPC por el parser nuevo (igual que el Barrido stream):
                    # el tallador viejo no entiende la cabecera 5B0301 y
                    # devolvia vacio -> se mostraba el crudo como EPC.
                    epc = (getattr(tag, "epc_hex", "") or "").upper()
                    chip = ""
                    if not epc:
                        epc, chip = _carve(raw)
                    tid = bytes(getattr(tag, "tid", b"") or b"")
                    if tid:
                        prefix = tid.hex().upper()[:8]
                        ent = TID_PREFIX_DB.get(prefix)
                        if isinstance(ent, tuple) and len(ent) >= 2:
                            chip = ("%s %s" % (ent[0], ent[1])).strip()
                        else:
                            chip = prefix
                    key = (epc or raw.hex().upper())
                    if key in _active["seen"]:
                        for row in _active["tags"]:
                            if row.get("key", row["raw"]) == key:
                                row["n"] += 1
                                row["rssi"] = tag.rssi
                                row["ant"] = tag.ant
                        continue
                    _active["seen"].add(key)
                    _active["tags"].append({
                        "key": key, "raw": raw.hex().upper(),
                        "epc": epc or raw.hex().upper()[:24],
                        "tid": "", "chip": chip or "interpreter pending",
                        "ant": tag.ant, "rssi": tag.rssi, "n": 1})
        finally:
            try:
                with r._lock:
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
                # el lector se cayo en medio del lote: esperar reconexion y
                # seguir (no se pierde ni se repite lo ya escrito)
                if not _link_dead(r):
                    break
                if not _await_reconnect(_batch_stop):
                    _batch["rows"].append(
                        {"epc": "", "result": "ERROR: lector desconectado "
                                              "(no volvió en la ventana)",
                         "rssi": ""})
                    break
                r = _reader
                try:
                    _rearm_after_reconnect(r, False)
                except Exception:  # noqa: BLE001
                    pass
                continue
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


EXPORTES_DIR = None  # se resuelve bajo demanda: RU5600_USB_RW_ZK/EXPORTES


def _exportes_dir():
    """Carpeta fija de exportes: _data_root()/EXPORTES (dentro del
    contenedor unico RU5600_Data), salvo override explicito legacy
    RU5600_EXPORT_DIR (se respeta tal cual)."""
    global EXPORTES_DIR
    if EXPORTES_DIR is None:
        from pathlib import Path
        override = os.environ.get("RU5600_EXPORT_DIR")
        if override:
            # override explicito legacy: se respeta tal cual
            EXPORTES_DIR = Path(override).expanduser()
        else:
            EXPORTES_DIR = _data_root() / "EXPORTES"
        EXPORTES_DIR.mkdir(parents=True, exist_ok=True)
    return EXPORTES_DIR


def _guardar_copia(data: bytes, filename: str) -> str:
    """Guarda una copia del export en EXPORTES/ con nombre fechado. Best-effort."""
    try:
        destino = _exportes_dir() / filename
        i = 1
        while destino.exists():                 # nunca pisar: sufijo -2, -3…
            destino = _exportes_dir() / (destino.stem + "-%d" % i + destino.suffix)
            i += 1
        destino.write_bytes(data)
        return str(destino)
    except Exception:  # noqa: BLE001 - el export al navegador sigue funcionando
        return ""


def _nombre_fechado(name: str, ext: str) -> str:
    """barrido + .xlsx -> barrido-2026-10-26-211530.xlsx"""
    import time as _t
    base = (name or "export").replace("/", "-")
    if base.lower().endswith(ext):
        base = base[:-len(ext)]
    return "%s-%s%s" % (base[:50].strip("-"), _t.strftime("%Y-%m-%d-%H%M%S"), ext)


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
        ws.title = ((cfg.get("sheet") or cfg.get("name") or "datos")).replace("/", "-")[:31]
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
        datos = buf.getvalue()
        name = _nombre_fechado(cfg.get("name") or "export", ".xlsx")
        guardado = _guardar_copia(datos, name)   # copia en EXPORTES/ (AppImage)
        resp = send_file(io.BytesIO(datos), as_attachment=True, download_name=name,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp.headers["X-Export-Ruta"] = guardado or "no-guardado"
        return resp
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)), 500


@app.route("/api/export/txt", methods=["POST"])
def api_export_txt():
    """Recibe texto libre y devuelve un .txt (para informes y lecturas)."""
    cfg = request.get_json(silent=True) or {}
    text = cfg.get("text") or ""
    if not str(text).strip():
        return jsonify(ok=False, error="no hay texto para exportar"), 400
    name = _nombre_fechado(cfg.get("name") or "export", ".txt")
    datos = str(text).encode("utf-8")
    guardado = _guardar_copia(datos, name)
    resp = send_file(io.BytesIO(datos), as_attachment=True, download_name=name,
                     mimetype="text/plain; charset=utf-8")
    resp.headers["X-Export-Ruta"] = guardado or "no-guardado"
    return resp


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
                # mismo tratamiento del lote: reconectar y seguir
                if not _link_dead(r):
                    break
                if not _await_reconnect(_map_stop):
                    _map["rows"].append(
                        {"tag": "", "new": "",
                         "result": "ERROR: lector desconectado "
                                   "(no volvió en la ventana)"})
                    break
                r = _reader
                try:
                    _rearm_after_reconnect(r, False)
                except Exception:  # noqa: BLE001
                    pass
                continue
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


def _stream_sweep(r, seconds, stop_ev=None):
    """Barrido de stream en ActiveMode: lecturas y RSSI por EPC.

    Es la misma secuencia del stream de la app (ActiveMode + StartRead +
    poll_tags), pero acumulando por tag en vez de quedarse con el primero:
    asi cada punto de la curva tiene sus propias lecturas y su RSSI.
    Devuelve ``(filas, elapsed)`` con rssi_avg/min/max ya calculados.
    """
    from ru5600_core import (DEV_ADDR, PARAM_WORKMODE, WORKMODE_ACTIVE,
                             WORKMODE_ANSWER)
    r._pace()
    with r._lock:
        r._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE, WORKMODE_ACTIVE)
    r._pace(settle=True)
    with r._lock:
        r._lib.CFHid_ClearTagBuf()
    r._pace()
    with r._lock:
        r._lib.CFHid_StartRead()
    r._pace(settle=True)
    t0 = time.monotonic()
    stats: dict = {}
    elapsed = 0.0
    try:
        while time.monotonic() - t0 < seconds and not (
                stop_ev is not None and stop_ev.is_set()):
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
            with r._lock:
                r._lib.CFHid_StopRead()
        except Exception:  # noqa: BLE001
            pass
        r._pace(settle=True)
        try:
            with r._lock:
                r._lib.CFHid_ClearTagBuf()
            r._pace()
            with r._lock:
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


@app.route("/api/distance_clear", methods=["POST"])
def api_distance_clear():
    """Borra los puntos de una etiqueta (limpiar la prueba en curso)."""
    cfg = request.get_json(silent=True) or {}
    label = str(cfg.get("label") or "").strip()
    if not label:
        return jsonify(ok=False, error="poné la etiqueta a limpiar"), 400
    try:
        n = store.clear_mediciones(label)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))
    _log("ok", "distancia: %d punto(s) borrados (%s)" % (n, label))
    return jsonify(ok=True, deleted=n, label=label)


@app.route("/api/status")
def status():
    now = time.monotonic()
    # Ventana corta (3 s): el dps tiene que reflejar el AHORA, no el
    # promedio de los ultimos 10 s (tras un silencio, la media de 10 s
    # tardaba ~6-10 s en subir aunque la entrega ya iba a fondo y se
    # percibia como "arranque lento").
    ts = [x for x in _state.get("hits", []) if now - x <= 3.0]
    _state["hits"] = ts
    last = (now - ts[-1]) if ts else None
    if ts:
        _state["last_seen_ts"] = ts[-1]
    running = _state["running"]
    # El umbral de "trabado" depende del modo: el dirigido lee de a un tag
    # (~1/s en el mejor caso, mas lento con varios tags y relecturas de TID),
    # asi que 5 s de silencio ahi es normal y no amerita sugerir Destrabar.
    # Antes el aviso saltaba igual en ambos modos y parecia que habia que
    # destrabar a cada rato aunque el barrido avanzara (falsa alarma medida
    # con 5 H10: 10 lecturas en modo dirigido con cartel de destrabe).
    _silence = now - _state.get("last_seen_ts", now)
    _stall_limit = 20.0 if _state.get("mode_used") == "dirigido" else 5.0
    return jsonify(running=running, error=_state["error"],
                   count=len(_state["tags"]), tags=_state["tags"],
                   target=_state.get("target", 0),
                   reason=_state.get("reason", ""),
                   stabilized=_state.get("stabilized", 0),
                   mode_used=_state.get("mode_used", "stream"),
                   stall=_state.get("stall_after", 30.0),
                   last_rssi=_state.get("last_rssi"),
                   last_rssi_t=_state.get("last_rssi_t", 0.0),
                   flags=_flags_safe(),
                   dps=round(len(ts) / 3.0, 1),
                   last_seen=None if last is None else round(last, 1),
                   stalled=bool(running and _silence > _stall_limit),
                   mode_error=_state.get("mode_error") or "",
                   reconnect=dict(_reconn),
                   elapsed=round(now - _state["started"], 1) if running else 0)


@app.route("/api/sweep_clear", methods=["POST"])
def api_sweep_clear():
    """Limpia el listado del barrido (solo se puede cuando esta pausado)."""
    with _lock:
        if _state.get("running"):
            return jsonify(ok=False, error="primero pausá la lectura"), 409
        _state.update(tags=[], seen={}, hits=[], reason="", error="",
                      last_rssi=None, last_rssi_t=0.0)
        _state["mode_used"] = "stream"
    _log("info", "barrido: listado limpiado")
    return jsonify(ok=True)


_sweep_mode_req = None  # cambio de modo pedido en caliente (active|answer)


@app.route("/api/sweep_mode", methods=["POST"])
def api_sweep_mode():
    """Cambia el modo del barrido en caliente (stream <-> dirigido).
    Con barrido corriendo: se aplica en la proxima iteracion del bucle
    (re-armea el hardware al modo pedido). Sin barrido corriendo: fija el
    modo por defecto del proximo (_sweep_cfg)."""
    global _sweep_mode_req
    cfg = request.get_json(silent=True) or {}
    m = str(cfg.get("mode") or "").lower()
    if m in ("stream", "dirigido"):
        m = {"stream": "active", "dirigido": "answer"}[m]
    if m not in ("active", "answer"):
        return jsonify(ok=False, error="modo invalido (active|answer)"), 400
    _sweep_cfg["mode"] = m
    if _state.get("running"):
        _sweep_mode_req = m
    return jsonify(ok=True, mode=m, aplicado=bool(_state.get("running")))


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
    # ventana de estancamiento: segundos sin tags NUEVOS antes de cortar (antes fijo en 30)
    try:
        stall = float(cfg.get("stall", 30) or 30)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="estancamiento invalido"), 400
    stall = max(5.0, min(stall, 600.0))
    # el stream de tags solo existe en ActiveMode; AnswerMode es para comandos dirigidos.
    # Sin "mode" explicito manda el perfil activo (perfiles de Parámetros).
    mode = (cfg.get("mode") or _sweep_cfg.get("mode") or "active").lower()
    # keep=true: CONTINUA el listado anterior (no borra tags ni contadores);
    # keep=false arranca de cero (lo usa el boton "Limpiar listado" + iniciar)
    keep = bool(cfg.get("keep"))
    with _lock:
        if _state["running"]:
            return jsonify(ok=False, error="ya corriendo"), 409
        prev_tags = list(_state.get("tags") or []) if keep else []
        prev_seen = dict(_state.get("seen") or {}) if keep else {}
        _state.update(running=True, tags=prev_tags, error="", target=target,
                      reason="", started=time.monotonic(), hits=[],
                      stall_after=stall, mode_error="",
                      last_rssi=None, last_rssi_t=0.0,
                      last_seen_ts=time.monotonic())
        # modo pedido desde el arranque (sin flash del modo anterior)
        _state["mode_used"] = "dirigido" if mode != "active" else "stream"
        _state["seen"] = prev_seen
    _stop.clear()
    threading.Thread(target=_run, args=(seconds, target, mode),
                     daemon=True).start()
    return jsonify(ok=True, target=target, mode=mode, stall=stall, keep=keep)


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
        with r._lock:
            r._lib.CFHid_SetDeviceOneParam(
                DEV_ADDR, PARAM_WORKMODE,
                WORKMODE_ACTIVE if mode == "active" else WORKMODE_ANSWER)
        r._pace(settle=True)
        # Verificar que el lector acepto el modo: si lo rechaza (firmware
        # trabado tras una corrida), el barrido quedaria SORDO contando cero
        # con tags enfrente y sin pitos. Mejor avisar que fingir que lee.
        try:
            want_wm = WORKMODE_ACTIVE if mode == "active" else WORKMODE_ANSWER
            got_wm = r.work_mode()
        except Exception:  # noqa: BLE001 - si no se puede leer, se sigue igual
            got_wm = None
        if got_wm is not None and int(got_wm) != int(want_wm):
            reason = "desconectado"
            _state.update(running=False, error="el lector no acepto el modo (reabri la conexion)", reason=reason)
            _log("error", "barrido sordo evitado: WorkMode quedo en %s (queria %s)" % (got_wm, want_wm))
            return
        with r._lock:
            r._lib.CFHid_ClearTagBuf()
        r._pace()
        with r._lock:
            r._lib.CFHid_StartRead()
        r._pace(settle=True)
        deadline = None if seconds <= 0 else time.monotonic() + seconds
        poll = 0.1
        last_hit = time.monotonic()
        stabilized = 0
        _state["stabilized"] = 0
        t_start = time.monotonic()
        last_new = time.monotonic()
        stall_after = float(_state.get("stall_after") or 30.0)
        # MODO MANUAL: lo elige el usuario (perfil o UI del Barrido, en
        # caliente con /api/sweep_mode). Sin transiciones automaticas.
        direct_mode = (mode != "active")
        # con ventanas cortas, esperar 6 s deja casi sin tiempo al modo dirigido
        last_link_chk = 0.0
        _state["mode_used"] = "dirigido" if direct_mode else "stream"
        try:
            while ((deadline is None or time.monotonic() < deadline)
                    and not _stop.is_set()):
                if target and len(_state["tags"]) >= target:
                    reason = "tope"
                    break
                if (target and len(_state["tags"]) < target
                        and (time.monotonic() - last_new) > stall_after):
                    reason = "estancado"
                    break
                # cambio de modo en caliente (lo pidio el usuario en la UI):
                # re-armea el hardware al modo pedido sin cortar el listado
                global _sweep_mode_req
                if _sweep_mode_req in ("active", "answer"):
                    _want_dir = (_sweep_mode_req == "answer")
                    _sweep_mode_req = None
                    if _want_dir != direct_mode:
                        direct_mode = _want_dir
                        _state["mode_used"] = "dirigido" if direct_mode else "stream"
                        try:
                            if direct_mode:
                                try:
                                    r.stop_read()
                                except Exception:  # noqa: BLE001
                                    pass
                                r._pace()
                                with r._lock:
                                    r._lib.CFHid_SetDeviceOneParam(
                                        DEV_ADDR, PARAM_WORKMODE, WORKMODE_ANSWER)
                                r._pace(settle=True)
                            else:
                                r._pace()
                                with r._lock:
                                    r._lib.CFHid_SetDeviceOneParam(
                                        DEV_ADDR, PARAM_WORKMODE, WORKMODE_ACTIVE)
                                r._pace(settle=True)
                                with r._lock:
                                    r._lib.CFHid_ClearTagBuf()
                                r._pace()
                                with r._lock:
                                    r._lib.CFHid_StartRead()
                                r._pace(settle=True)
                            # Verificar que el lector acepto el modo pedido:
                            # si lo rechaza, volver al anterior y avisar en
                            # vez de quedar "sordo" con modo mentido en la UI.
                            want_wm = WORKMODE_ANSWER if direct_mode else WORKMODE_ACTIVE
                            try:
                                got_wm = r.work_mode()
                            except Exception:  # noqa: BLE001
                                got_wm = None
                            if got_wm is not None and int(got_wm) != int(want_wm):
                                raise RuntimeError("lector no acepto WorkMode (quedo en %s)" % got_wm)
                        except Exception as exc:  # noqa: BLE001
                            direct_mode = not _want_dir
                            _state["mode_used"] = "dirigido" if direct_mode else "stream"
                            _state["mode_error"] = "el lector no acepto el modo (reabri la conexion)"
                            _log("error", "cambio de modo en caliente fallo: %s (vuelvo a %s)" % (exc, _state["mode_used"]))
                        else:
                            _state.pop("mode_error", None)
                            _log("ok", "modo en caliente: %s" % _state["mode_used"])
                # Ritmo CONSTANTE: una pausa por loop, siempre la misma.
                # La unica pausa valida es la que pide el usuario.
                time.sleep(poll)
                # MODO MANUAL: sin fallback automatico. Si el usuario quiere
                # el otro camino lo pide en caliente (/api/sweep_mode).
                if direct_mode:
                    res, epc_d, hex_d, det = _direct_read(r)
                    if res != "ok" or not epc_d:
                        # "error" aca casi siempre es tag ausente (campo vacio):
                        # solo una sonda decide si el LECTOR se fue. Cooldown
                        # 3 s para no martillar con comandos de sondeo.
                        if (res == "error"
                                and time.monotonic() - last_link_chk > 3.0):
                            last_link_chk = time.monotonic()
                            if _link_dead(r):
                                rr = _reconn_or_die(r, True, _stop)
                                if rr is None:
                                    reason = ("usuario" if _stop.is_set()
                                              else "desconectado")
                                    break
                                r = rr
                                last_new = last_hit = time.monotonic()
                        time.sleep(poll)
                        continue
                    key = epc_d.upper()
                    prev = _state["seen"].get(key)
                    if prev is not None:
                        # tag ya conocido: se cuenta y se sigue. Antes se releia el TID
                        # igual, o sea un comando extra por relectura (~50% mas de ordenes).
                        prev["n"] = prev.get("n", 1) + 1
                        _state.setdefault("hits", []).append(time.monotonic())
                        last_hit = time.monotonic()
                        continue
                    info = {"epc": key, "chip": "", "xl": "-",
                            "conf": "candidato",
                            "code": epc_d[6:14] if len(epc_d) >= 14 else ""}
                    # TID solo para tags nuevos; si contesta frame viejo, un reintento.
                    # IMPORTANTE (Test B 2026-10-26): en un barrido con objetivo >1 el
                    # TID se lee SIN Select y puede responder OTRO tag del campo — el
                    # chip queda como "candidato". En el stream NO pasa: el TID viene en
                    # el mismo registro del EPC (mismo tag), ahi es confiable.
                    for _attempt in range(2):
                        try:
                            tid = r.read_card(2, 0, 2, b"\x00" * 4, retries=2)
                            prefix = tid.hex().upper()[:8]
                            ent = TID_PREFIX_DB.get(prefix)
                            info["chip"] = ("%s %s" % (ent[0], ent[1])).strip() if isinstance(ent, tuple) and len(ent) >= 2 else prefix
                            # Candidato salvo barrido de UN solo tag (target == 1):
                            # el TID dirigido se lee SIN Select y con varios tags
                            # en campo puede responder OTRO tag. Con N=0 (sin tope)
                            # y varios tags presentes, el "confirmado" mentia
                            # (medido: 5 H10 con N=0, dos "confirmados" con el
                            # mismo riesgo de cruce que los "candidatos").
                            if target != 1:
                                info["chip"] = (info["chip"] or prefix) + " (candidato)"
                                info["conf"] = "candidato (TID sin Select)"
                            elif isinstance(ent, tuple):
                                info["conf"] = "confirmado"
                            break
                        except Exception:  # noqa: BLE001
                            time.sleep(0.12)
                    if idx is not None:
                        hit = idx.get(info["epc"])
                        if hit is not None:
                            info["xl"] = "%s #%s" % (hit[0], hit[1])
                    info.update(raw=hex_d[:48], ant=0, rssi=None, n=1)
                    _state["seen"][key] = info
                    last_new = time.monotonic()
                    _state["tags"].append(info)
                    _state.setdefault("hits", []).append(time.monotonic())
                    last_hit = time.monotonic()
                    # pausa entre lecturas dirigidas: la decide el modo de lectura
                    # (Conexión): Ráfaga 30 ms · Balanceado 120 ms · Confiable 250 ms
                    time.sleep(_read_pace()["pause"])
                    continue
                stream_tags = r.poll_tags()
                if stream_tags:
                    # CUALQUIER entrega cuenta como salud: antes solo los tags
                    # NUEVOS refrescaban last_hit, entonces con un solo tag en la
                    # lectora (todo repetido) el rearme se disparaba cada ~8 s y
                    # cortaba la lectura (pausas del buzzer, medido: 5 rearms en
                    # 40 s con 309 lecturas)
                    last_hit = time.monotonic()
                for tag in stream_tags:
                    # RSSI fresco en cada lectura (tambien en relecturas): es
                    # lo que permite medir intensidad por altura/posicion
                    if getattr(tag, "rssi", None) is not None:
                        _state["last_rssi"] = tag.rssi
                        _state["last_rssi_t"] = time.time()
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
                        # CUALQUIER entrega (nuevo o repetido) mantiene el poll
                        # rapido: el lector sigue viendo tags en campo y puede
                        # aparecer uno nuevo en cualquier momento (grupos).
                        got = True
                        continue
                    info.update(raw=blob_key.hex().upper()[:48], ant=tag.ant,
                                rssi=tag.rssi, n=1)
                    _state["seen"][key] = info
                    last_new = time.monotonic()
                    _state["tags"].append(info)
                    _state.setdefault("hits", []).append(time.monotonic())
                    last_hit = time.monotonic()
                now2 = time.monotonic()
                # Ritmo CONSTANTE 24/7: lo decide el modo de lectura
                # (perfil o Conexion: Rafaga 30 ms, Balanceado 120 ms,
                # Confiable 250 ms). Sin adaptaciones: la unica pausa
                # valida es la que pide el usuario (Pausar/Destrabar).
                poll = _read_pace()["pause"]
                # Sin patadas automaticas: si el lector se calla, no se lo
                # toca (nada de Stop/Start que interrumpa su ciclo). Solo se
                # verifica que siga VIVO con una sonda barata (max 1 cada
                # 3 s): si se cayo, flujo de reconexion; si esta vivo, se
                # sigue esperando en rafaga. La recuperacion manual es el
                # boton "Destrabar lector".
                if now2 - last_hit > 15 and _state.get("tags"):
                    if now2 - last_link_chk > 3.0:
                        last_link_chk = now2
                        if _link_dead(r):
                            rr = _reconn_or_die(r, direct_mode, _stop)
                            if rr is None:
                                reason = ("usuario" if _stop.is_set()
                                          else "desconectado")
                                break
                            r = rr
                            # relojes REINICIADOS: el tiempo desconectado
                            # no cuenta como "dejo de haber gente"
                            last_new = last_hit = time.monotonic()
        finally:
            try:
                with r._lock:
                    r._lib.CFHid_StopRead()
            except Exception:  # noqa: BLE001
                pass
            r._pace(settle=True)
            # volver a AnswerMode (el stream deja el lector en Active)
            try:
                with r._lock:
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
            with r._lock:
                r._lib.CFHid_ClearTagBuf()
            steps.append("buffer borrado")
        except Exception as exc:  # noqa: BLE001
            steps.append("clear: " + str(exc)[:40])
        try:
            r._pace()
            with r._lock:
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


@app.route("/api/js_error", methods=["POST"])
def api_js_error():
    """La ventana nativa (pywebview) no tiene consola visible: el frontend
    reporta aca sus errores (window.onerror) y quedan en el log de la app."""
    try:
        d = request.get_json(silent=True) or {}
        msg = str(d.get("msg") or "")[:300]
        where = "%s:%s" % (d.get("src", "?"), d.get("line", "?"))
        ua = (request.headers.get("User-Agent") or "")[:80]
        _log("warn", "JS en ventana: %s (%s) [%s]" % (msg, where, ua))
        return jsonify(ok=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


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
    out = {"open": False, "reconnect": dict(_reconn)}
    if _reader is not None and _reader.is_open:
        out["open"] = True
        # si hay un comando en vuelo, informar "busy" en vez de bloquear la
        # respuesta (o devolver un error falso: el lector NO esta desconectado)
        if not _reader._lock.acquire(blocking=False):
            out["busy"] = True
            return jsonify(out)
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
        # senales de salud del vendor (ver _link_dead): el LED apagado, en JSON
        try:
            hp = _reader.hid_handle_ptr()
            out["hid_handle"] = "ok" if hp else ("NULL (vendor detectó la "
                                                 "desconexión)" if hp == 0
                                                 else "no determinable")
        except Exception:  # noqa: BLE001
            out["hid_handle"] = "error"
        # null = el equipo no contesto el comando; entero = contesto
        try:
            out["trans_state"] = _reader.trans_state()
        except Exception:  # noqa: BLE001
            out["trans_state"] = None
        try:
            out["check_module"] = _reader.check_module()
        except Exception:  # noqa: BLE001
            out["check_module"] = None
        out["vendor_events"] = list(_hid_events[-5:])
        try:
            fr = _reader.read_freq()
            out["freq"] = fr.hex().upper() if isinstance(fr, (bytes, bytearray)) else fr
        except Exception:  # noqa: BLE001
            pass
        finally:
            _reader._lock.release()
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
        with r._lock:
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
            if what == "buffer_dump":
                # vaciar, leer dirigido y volcar TODO lo que quede en el buffer del lector
                import ctypes as _ct
                def drain(tag=""):
                    out = []
                    for _i in range(3):
                        buf = (_ct.c_ubyte * 131072)()
                        total = _ct.c_uint32(0); count = _ct.c_uint32(0)
                        ret = r._lib.CFHid_GetTagBuf(buf, _ct.byref(total), _ct.byref(count))
                        out.append({"ret": int(ret), "total": int(total.value),
                                    "count": int(count.value),
                                    "hex": bytes(buf[:total.value]).hex().upper() if total.value else ""})
                        time.sleep(0.08)
                    return {"when": tag, "drains": out}
                steps = []
                # primero: que habia ANTES de tocar nada (restos del tag anterior)
                steps.append(drain("antes de limpiar (restos)"))
                r._lib.CFHid_ClearTagBuf(); time.sleep(0.1)
                steps.append(drain("tras ClearTagBuf"))
                res, epc, hx, det = _direct_read(r)
                steps.append({"lectura": res, "epc": epc, "hex": hx})
                steps.append(drain("tras lectura dirigida"))
                try:
                    r.start_read(); time.sleep(0.6)
                    steps.append(drain("tras StartRead 0.6 s"))
                    r.stop_read()
                except Exception as exc:  # noqa: BLE001
                    steps.append({"startread_error": str(exc)[:80]})
                steps.append(drain("final"))
                _log("info", "buffer_dump: %s" % (epc or "sin tag"))
                return jsonify(ok=True, steps=steps)
            if what == "first_tag":
                # latencia hasta el primer tag, N rondas, con poll de 50 ms
                import ctypes as _ct
                from ru5600_core import (DEV_ADDR as _DA, PARAM_WORKMODE as _PW,
                                         WORKMODE_ACTIVE as _WA,
                                         WORKMODE_ANSWER as _WS)
                rounds = int(cfg.get("rounds", 5) or 5)
                samples = []
                r._pace()
                r._lib.CFHid_SetDeviceOneParam(_DA, _PW, _WA)
                r._pace(settle=True)
                for i in range(rounds):
                    r._lib.CFHid_ClearTagBuf()
                    t0 = time.monotonic()
                    r.start_read()
                    first = None
                    while time.monotonic() - t0 < 6.0:
                        buf = (_ct.c_ubyte * 131072)()
                        total = _ct.c_uint32(0); count = _ct.c_uint32(0)
                        r._lib.CFHid_GetTagBuf(buf, _ct.byref(total), _ct.byref(count))
                        if count.value:
                            first = round((time.monotonic() - t0) * 1000)
                            break
                        time.sleep(0.05)
                    r.stop_read()
                    samples.append(first)
                    time.sleep(0.3)
                try:
                    r._pace(); r._lib.CFHid_SetDeviceOneParam(_DA, _PW, _WS)
                except Exception:  # noqa: BLE001
                    pass
                good = [x for x in samples if x]
                _log("info", "first_tag: %s ms" % samples)
                return jsonify(ok=True, samples_ms=samples, mediana=(sorted(good)[len(good)//2] if good else None))
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
        data = r.read_card(1, 0, 8, b"\x00" * 4,
                           retries=_read_pace()["retries"])
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
        pause = float(cfg.get("pause", _read_pace()["pause"]))
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
    if not _ensure_answer_mode(r):
        return jsonify(ok=False,
                       error="el lector no acepto el modo dirigido: reabrí la conexión")
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


_clone_job = {"running": False, "result": None, "error": None,
               "seconds": 0, "started": 0.0}
_clone_stop = threading.Event()


_clone_job = {"running": False, "result": None, "error": None,
               "seconds": 0, "started": 0.0}
_clone_stop = threading.Event()
def _ensure_answer_mode(r):
    """Deja el lector en AnswerMode (lectura dirigida). Devuelve True si quedo."""
    try:
        from ru5600_core import DEV_ADDR, PARAM_WORKMODE, WORKMODE_ANSWER
        r._pace()
        with r._lock:
            try:
                r._lib.CFHid_StopRead()
            except Exception:  # noqa: BLE001 - si no habia stream, seguir
                pass
            r._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                            WORKMODE_ANSWER)
        r._pace(settle=True)
        return r.work_mode() == WORKMODE_ANSWER
    except Exception:  # noqa: BLE001
        return False


_clone_scan_job = {"running": False, "tags": [], "started": 0.0,
                   "seconds": 0, "error": None}
_clone_scan_stop = threading.Event()


def _clone_run_scan(r, seconds):
    """Escaner la muestra en stream con reporte en vivo (para elegir origen)."""
    from ru5600_core import (DEV_ADDR, PARAM_WORKMODE, WORKMODE_ACTIVE,
                             WORKMODE_ANSWER)
    from ru5600_chip import TID_PREFIX_DB
    deadline = time.monotonic() + seconds
    stats: dict = {}
    try:
        r._pace()
        with r._lock:
            r._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                            WORKMODE_ACTIVE)
        r._pace(settle=True)
        with r._lock:
            r._lib.CFHid_ClearTagBuf()
        r._pace()
        with r._lock:
            r._lib.CFHid_StartRead()
        r._pace(settle=True)
        while time.monotonic() < deadline and not _clone_scan_stop.is_set():
            for tag in r.poll_tags():
                epc = (getattr(tag, "epc_hex", "") or "").upper()
                chip = None
                tid = bytes(getattr(tag, "tid", b"") or b"")
                if tid:
                    prefix = tid.hex().upper()[:8]
                    ent = TID_PREFIX_DB.get(prefix)
                    chip = (("%s %s" % (ent[0], ent[1])).strip()
                            if isinstance(ent, tuple) and len(ent) >= 2
                            else prefix)
                if not epc:
                    epc, carved = _carve(bytes(tag.raw_id) if tag.raw_id
                                          else bytes(tag.epc))
                    chip = chip or carved
                if not epc:
                    continue
                row = stats.setdefault(epc, {"epc": epc, "chip": chip or "candidato"})
                if chip and (not row["chip"] or row["chip"] == "candidato"):
                    row["chip"] = chip
            with _lock:
                _clone_scan_job["tags"] = [{"epc": k, "chip": v["chip"]}
                                            for k, v in sorted(stats.items())]
            time.sleep(0.15)
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _clone_scan_job["error"] = "escaneo fallo: " + str(exc)
    finally:
        try:
            with r._lock:
                r._lib.CFHid_StopRead()
        except Exception:  # noqa: BLE001
            pass
        r._pace(settle=True)
        try:
            with r._lock:
                r._lib.CFHid_SetDeviceOneParam(DEV_ADDR, PARAM_WORKMODE,
                                                WORKMODE_ANSWER)
            r._pace(settle=True)
        except Exception:  # noqa: BLE001
            pass
        with _lock:
            _clone_scan_job["running"] = False


@app.route("/api/clone_scan", methods=["POST"])
def api_clone_scan():
    """Escaner la muestra en segundo plano: el estado se lee con status."""
    cfg = request.get_json(silent=True) or {}
    try:
        seconds = float(cfg.get("seconds", 8))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="segundos invalidos"), 400
    seconds = max(3.0, min(seconds, 20.0))
    with _lock:
        if _clone_scan_job["running"]:
            return jsonify(ok=False, error="ya escaneando"), 409
        _clone_scan_job.update(running=True, tags=[], started=time.monotonic(),
                               seconds=seconds, error=None)
        _clone_scan_stop.clear()
    try:
        r = _need_usb()
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _clone_scan_job["running"] = False
        return jsonify(ok=False, error=str(exc))
    threading.Thread(target=_clone_run_scan, args=(r, seconds),
                     daemon=True).start()
    return jsonify(ok=True, seconds=seconds)


@app.route("/api/clone_scan_status", methods=["GET"])
def api_clone_scan_status():
    with _lock:
        job = dict(_clone_scan_job)
        tags = list(job["tags"])
    left = 0.0
    if job["running"]:
        left = max(0.0, job["seconds"] - (time.monotonic() - job["started"]))
    return jsonify(ok=True, running=job["running"], count=len(tags),
                   tags=tags, left=round(left, 1), seconds=job["seconds"],
                   error=job["error"])


@app.route("/api/clone_scan_stop", methods=["POST"])
def api_clone_scan_stop():
    _clone_scan_stop.set()
    return jsonify(ok=True)


@app.route("/api/clone_pick", methods=["POST"])
def api_clone_pick():
    """Fija como origen el EPC elegido de la lista (verificado por lectura)."""
    global _clone_src
    cfg = request.get_json(silent=True) or {}
    want = str(cfg.get("epc") or "").strip().upper()
    if not want:
        return jsonify(ok=False, error="falta el EPC a fijar"), 400
    try:
        r = _need_usb()
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))
    try:
        t0 = time.monotonic()
        epc = None
        while time.monotonic() - t0 < 6.0:
            cur, _raw = _clone_probe(r, attempts=3, pause=0.1)
            if cur and cur.upper() == want:
                epc = cur
                break
            time.sleep(0.2)
        if not epc:
            return jsonify(ok=False,
                           error="ese tag no esta a la vista: presentalo solo y reintenta")
        tid = _read_tid(r)
        if not tid:
            return jsonify(ok=False,
                           error="lei el EPC pero no el TID: no puedo clonar sin TID")
        chip = _chip_from_prefix(tid)
        with _lock:
            _clone_src = {"epc": epc, "tid": tid, "chip": chip,
                          "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        _log("ok", "clone_pick: EPC %s / TID %s (%s)" % (epc, tid, chip))
        return jsonify(ok=True, epc=epc, tid=tid, chip=chip)
    except Exception as exc:  # noqa: BLE001
        _log("error", "clone_pick: %s" % exc)
        return jsonify(ok=False, error="no pude fijar el origen: " + str(exc))


@app.route("/api/clone_burst", methods=["POST"])
def api_clone_burst():
    """Rafaga de clonado con parada absoluta: N segundos y corta solo."""
    cfg = request.get_json(silent=True) or {}
    try:
        seconds = int(cfg.get("seconds", 5))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="segundos invalidos"), 400
    seconds = max(1, min(seconds, 10))
    if not _clone_src:
        return jsonify(ok=False, error="primero lee o elegi el tag origen"), 400
    with _lock:
        if _clone_job["running"]:
            return jsonify(ok=False, error="ya corriendo"), 409
        _clone_job.update(running=True, result=None, error=None,
                          seconds=seconds, started=time.monotonic())
        _clone_stop.clear()
    try:
        r = _need_usb()
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _clone_job["running"] = False
        return jsonify(ok=False, error=str(exc))
    threading.Thread(target=_clone_run_burst, args=(r, seconds),
                     daemon=True).start()
    return jsonify(ok=True, seconds=seconds)


@app.route("/api/clone_burst_status", methods=["GET"])
def api_clone_burst_status():
    with _lock:
        job = dict(_clone_job)
    left = 0.0
    if job["running"]:
        left = max(0.0, job["seconds"] - (time.monotonic() - job["started"]))
    return jsonify(ok=True, running=job["running"], left=round(left, 1),
                   result=job["result"], error=job["error"])


@app.route("/api/clone_stop", methods=["POST"])
def api_clone_stop():
    _clone_stop.set()
    return jsonify(ok=True)


def _clone_run_burst(r, seconds):
    """Loop de clonado con deadline: vencido el tiempo no se abre
    ninguna escritura nueva. El origen se omite siempre por TID."""
    src = _clone_src
    deadline = time.monotonic() + seconds
    done_tids: set = set()
    clonados = omitidos = errores = 0
    detalle: list = []
    if not _ensure_answer_mode(r):
        with _lock:
            _clone_job["error"] = "el lector no acepto el modo dirigido"
            _clone_job["running"] = False
        _log("error", "clone_burst: sin modo dirigido, no arranco")
        return
    try:
        while time.monotonic() < deadline and not _clone_stop.is_set():
            cur_epc, _raw = _clone_probe(r, attempts=3, pause=0.05)
            if not cur_epc:
                time.sleep(0.2)
                continue
            tid_dst = _read_tid(r)
            if not tid_dst:
                time.sleep(0.2)
                continue
            if tid_dst in done_tids:
                time.sleep(0.3)
                continue
            same = _tid_compare(src["tid"], tid_dst)
            if same or same is None:
                omitidos += 1
                done_tids.add(tid_dst)
                _log("warn", "clone_burst: origen a la vista, omitido")
                time.sleep(0.3)
                continue
            if time.monotonic() >= deadline:
                break
            try:
                r.write_epc(bytes.fromhex(src["epc"]), b"\x00" * 4,
                            verify=True)
            except Exception as exc:  # noqa: BLE001
                errores += 1
                _log("error", "clone_burst: no pude escribir: %s" % exc)
                time.sleep(0.3)
                continue
            epc_after, _raw2 = _clone_probe(r, attempts=5)
            if (epc_after or "").upper() == src["epc"].upper():
                clonados += 1
                done_tids.add(tid_dst)
                if len(detalle) < 200:
                    detalle.append({"tid": tid_dst})
                _log("ok", "clone_burst: EPC %s en TID %s (verificado)"
                     % (src["epc"], tid_dst))
            else:
                errores += 1
                _log("warn", "clone_burst: escribio pero no verifico (TID %s)"
                     % tid_dst)
            time.sleep(0.2)
        result = {"clonados": clonados, "omitidos_origen": omitidos,
                  "errores": errores, "seconds": seconds, "detalle": detalle,
                  "cortado": bool(_clone_stop.is_set())}
    except Exception as exc:  # noqa: BLE001
        result = None
        with _lock:
            _clone_job["error"] = "rafaga fallo: " + str(exc)
        _log("error", "clone_burst: %s" % exc)
    else:
        with _lock:
            _clone_job["error"] = None
    with _lock:
        _clone_job["result"] = result
        _clone_job["running"] = False
    try:
        store.add_trabajo("clone_burst", "ok", total=clonados + omitidos + errores,
                          hechos=clonados,
                          extra={"origen": src, "resultado": result})
    except Exception:  # noqa: BLE001
        pass


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
        pause = float(cfg.get("pause", _read_pace()["pause"]))
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


def _flags_safe():
    try:
        return store.tag_flags()
    except Exception:  # noqa: BLE001
        return {}


@app.route("/api/tags/flags")
def api_tag_flags():
    return jsonify(ok=True, flags=_flags_safe())


@app.route("/api/tags/flag", methods=["POST"])
def api_tag_flag():
    cfg = request.get_json(silent=True) or {}
    epc = (cfg.get("epc") or "").upper().strip()
    if not epc:
        return jsonify(ok=False, error="falta el EPC"), 400
    try:
        store.set_tag_flag(epc, cfg.get("motivo") or "")
        _log("warn", "tag marcado como problematico: %s (%s)" % (epc, cfg.get("motivo") or "sin motivo"))
        return jsonify(ok=True, flags=store.tag_flags())
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


@app.route("/api/tags/unflag", methods=["POST"])
def api_tag_unflag():
    cfg = request.get_json(silent=True) or {}
    epc = (cfg.get("epc") or "").upper().strip()
    try:
        store.clear_tag_flag(epc)
        _log("info", "tag desmarcado: %s" % epc)
        return jsonify(ok=True, flags=store.tag_flags())
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc))


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


# --- Patrones EPC (spec 2026-10-28-patrones-epc-design) ----------------------
# Analisis determinista de planillas, offline y sin IA. La logica pura vive
# en patrones.py; aca solo rutas + padrones en _data_root()/PADRONES.
import patrones as _patrones  # noqa: E402

_PATRONES: dict = {}  # token -> payload del ultimo analyze (para el informe)


@app.route("/patrones")
def patrones():
    return render_template("patrones.html")


def _padron_cargar(kind: str) -> dict:
    """Lee el padron guardado (si existe) y devuelve sets {epcs, w34}."""
    from pathlib import Path
    for ext in (".xlsx", ".xlsm", ".csv"):
        p = Path(str(_data_root())) / "PADRONES" / (kind + ext)
        if p.is_file():
            try:
                raw = p.read_bytes()
                if ext == ".csv":
                    epcs = _patrones.extract_epcs_csv(raw)
                else:
                    epcs = _patrones.extract_epcs_xlsx(raw)
                return _patrones.padron_sets(epcs)
            except Exception:  # noqa: BLE001 - padron ilegible = padron vacio
                return {"epcs": set(), "w34": set()}
    return {"epcs": set(), "w34": set()}


def _padron_hay(kind: str) -> bool:
    from pathlib import Path
    return any((Path(str(_data_root())) / "PADRONES" / (kind + ext)).is_file()
               for ext in (".xlsx", ".xlsm", ".csv"))


@app.route("/api/patrones/analyze", methods=["POST"])
def api_patrones_analyze():
    import uuid
    up = request.files.get("file")
    if up is None:
        return jsonify(ok=False, error="falta el archivo")
    name = (up.filename or "").lower()
    try:
        raw = up.read()
        if name.endswith((".xlsx", ".xlsm")):
            epcs = _patrones.extract_epcs_xlsx(raw)
        elif name.endswith(".csv"):
            epcs = _patrones.extract_epcs_csv(raw)
        else:
            return jsonify(ok=False,
                           error="formato no soportado (usá .xlsx o .csv)")
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error="no pude leer el archivo: " + str(exc))
    warnings = []
    hay_h10, hay_zk = _padron_hay("h10"), _padron_hay("zkteco")
    if not hay_h10 and not hay_zk:
        warnings.append("sin padrones cargados")
    else:
        if not hay_h10:
            warnings.append("sin padrón h10 cargado")
        if not hay_zk:
            warnings.append("sin padrón zkteco cargado")
    payload = _patrones.analyze_epcs(
        epcs, _padron_cargar("h10") if hay_h10 else None,
        _padron_cargar("zkteco") if hay_zk else None)
    payload["filename"] = up.filename or ""
    payload["warnings"] = warnings
    token = uuid.uuid4().hex[:8]
    _PATRONES[token] = payload
    if len(_PATRONES) > 20:  # memoria acotada: solo informes recientes
        _PATRONES.pop(next(iter(_PATRONES)))
    return jsonify(ok=True, filename=payload["filename"], token=token,
                   count=len(epcs), rows=payload["rows"],
                   summary=payload["summary"], warnings=warnings)


@app.route("/api/patrones/registry", methods=["POST"])
def api_patrones_registry():
    kind = str(request.form.get("kind") or "").lower()
    if kind not in ("h10", "zkteco"):
        return jsonify(ok=False, error="kind invalido (h10|zkteco)"), 400
    up = request.files.get("file")
    if up is None:
        return jsonify(ok=False, error="falta el archivo")
    name = (up.filename or "").lower()
    try:
        raw = up.read()
        if name.endswith((".xlsx", ".xlsm")):
            epcs = _patrones.extract_epcs_xlsx(raw)
        elif name.endswith(".csv"):
            epcs = _patrones.extract_epcs_csv(raw)
        else:
            return jsonify(ok=False,
                           error="formato no soportado (usá .xlsx o .csv)")
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error="no pude leer el archivo: " + str(exc))
    from pathlib import Path
    dest = Path(str(_data_root())) / "PADRONES"
    dest.mkdir(parents=True, exist_ok=True)
    ext = ".csv" if name.endswith(".csv") else ".xlsx"
    (dest / (kind + ext)).write_bytes(raw)
    # si cambia el formato, borrar el anterior para no leerlo dos veces
    for other in (".xlsx", ".csv"):
        if other != ext:
            try:
                (dest / (kind + other)).unlink()
            except OSError:
                pass
    validos = _patrones.padron_sets(epcs)
    return jsonify(ok=True, kind=kind, count=len(validos["epcs"]))


@app.route("/api/patrones/report")
def api_patrones_report():
    token = str(request.args.get("token") or "")
    payload = _PATRONES.get(token)
    if payload is None:
        return jsonify(ok=False, error="informe vencido: repetí el análisis"), 404
    try:
        datos = _patrones.build_report(payload)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)), 500
    base = (payload.get("filename") or "patrones").rsplit(".", 1)[0][:40]
    return send_file(io.BytesIO(datos), as_attachment=True,
                     download_name="patrones-%s.xlsx" % base,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _pick_port(preferred=8000):
    """Primer puerto libre: el preferido ($RU5600_PORT o --port), si no
    8000..8011. Evita que la app muera si el puerto esta ocupado (otra
    instancia, otro programa): autoroutea a uno libre."""
    import socket
    try:
        first = int(os.environ.get("RU5600_PORT") or preferred or 8000)
    except (TypeError, ValueError):
        first = 8000
    cands = [first] + [q for q in range(first + 1, first + 12)
                     if 1 <= q <= 65535]
    if first < 8000 or first > 8011:
        # ademas del vecindario del preferido, el rango habitual
        cands += [q for q in range(8000, 8012) if q not in cands]
    for q in cands:
        if not 1 <= q <= 65535:
            continue
        t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            t.bind(("127.0.0.1", q))
            return q
        except OSError:
            continue
        finally:
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass
    return first  # ultimo recurso: que falle con el error real


def _config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "ru5600-reader", "config.json")


def _load_config():
    try:
        import json as _j
        with open(_config_path()) as fh:
            d = _j.load(fh)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_config(patch):
    import json as _j
    path = _config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cur = _load_config()
        cur.update(patch or {})
        with open(path, "w") as fh:
            _j.dump(cur, fh, indent=2, ensure_ascii=False)
        return True
    except OSError:
        return False


_DATA_ROOT = None  # override en memoria (POST /api/data_dir)


def _data_root():
    """Carpeta contenedora de datos (una sola: RU5600_Data).
    Orden: override en memoria > RU5600_DATA_DIR (contenedor) >
    config.json (data_dir) > defecto XDG. Siempre existe al volver."""
    from pathlib import Path
    if _DATA_ROOT:
        root = Path(_DATA_ROOT).expanduser()
    else:
        override = os.environ.get("RU5600_DATA_DIR")
        cfg = (None if override else _load_config().get("data_dir"))
        if override:
            root = Path(override).expanduser()
        elif cfg:
            root = Path(cfg).expanduser()
        else:
            xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(
                os.path.expanduser("~"), ".local", "share")
            root = Path(xdg) / "ru5600-reader" / "RU5600_Data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _migrate_legacy_data():
    """Migracion unica desde el layout viejo (DATA/ y EXPORTES/ junto al
    AppImage): si el contenedor esta fresco y hay DB vieja, se copia (db +
    exportes). Best-effort, con log; nunca borra el origen."""
    try:
        from pathlib import Path
        import shutil
        appimg = os.environ.get("APPIMAGE")
        if not appimg:
            return
        sib = Path(appimg).resolve().parent
        root = _data_root()
        if (root / "ru5600.db").exists():
            return  # contenedor ya en uso: no tocar
        legacy_db = sib / "DATA" / "ru5600.db"
        if legacy_db.is_file():
            shutil.copy2(legacy_db, root / "ru5600.db")
            _log("ok", "datos migrados desde %s a %s" % (legacy_db, root))
        legacy_ex = sib / "EXPORTES"
        if legacy_ex.is_dir():
            dest = root / "EXPORTES"
            dest.mkdir(parents=True, exist_ok=True)
            for f in legacy_ex.iterdir():
                if f.is_file() and not (dest / f.name).exists():
                    shutil.copy2(f, dest / f.name)
    except Exception as exc:  # noqa: BLE001
        _log("warn", "migracion de datos viejos omitida: %s" % exc)


# --- version y actualizaciones ------------------------------------------------
# VERSION vive en packaging/VERSION (y viaja dentro de la AppImage). El chequeo
# de release lee latest.json AL LADO del AppImage (flujo USB, sin internet) o
# de RU5600_UPDATE_URL si esta seteada. El build es quien genera latest.json.


def _app_version() -> str:
    for cand in (os.path.join(os.path.dirname(__file__), "VERSION"),
                 os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "packaging", "VERSION")):
        try:
            with open(cand) as fh:
                return fh.read().strip()
        except OSError:
            continue
    return "dev"


def _version_tuple(v: str):
    """(1,2,0) para comparar; lo no numerico se ignora (1.2.0-rc1 -> (1,2,0))."""
    out = []
    for part in (v or "").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def _find_latest_json():
    """Busca latest.json: al lado del AppImage ($APPIMAGE), en
    RU5600_UPDATE_DIR (test/dev) o en RU5600_UPDATE_URL. Devuelve (dict, origen)."""
    cands = []
    appimg = os.environ.get("APPIMAGE")          # solo dentro de una AppImage
    if appimg:
        cands.append(("appimage", os.path.join(os.path.dirname(appimg),
                                               "latest.json")))
    upd_dir = os.environ.get("RU5600_UPDATE_DIR")
    if upd_dir:
        cands.append(("dir", os.path.join(upd_dir, "latest.json")))
    # en dev: el dist/ del repo (donde cae el build)
    cands.append(("repo", os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(__file__))), "dist", "latest.json")))
    for origen, path in cands:
        try:
            with open(path) as fh:
                return json.load(fh), origen + ":" + path
        except (OSError, ValueError):
            continue
    url = (os.environ.get("RU5600_UPDATE_URL")
           or "https://github.com/Jeanjo1310/RU5600-usb-app/releases/latest/download/latest.json")
    if url:
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8")), "url:" + url
        except Exception:  # noqa: BLE001
            return None, "url-failed:" + url
    return None, ""


def _data_stats():
    """Lo que hay en el contenedor: ruta, tags en la DB, archivos en EXPORTES."""
    from pathlib import Path
    root = _data_root()
    try:
        ntags = store.stats().get("tags")
    except Exception:  # noqa: BLE001
        ntags = None
    try:
        files = sorted(p.name for p in (root / "EXPORTES").iterdir()
                       if p.is_file()) if (root / "EXPORTES").is_dir() else []
    except OSError:
        files = []
    return {"path": str(root), "tags": ntags,
            "exportes": len(files), "ultimos": files[-5:]}


@app.route("/api/data_dir")
def api_data_dir():
    """Contenedor de datos actual (ruta + contenido)."""
    st = _data_stats()
    return jsonify(ok=True, **st)


@app.route("/api/data_dir", methods=["POST"])
def api_data_dir_set():
    """Cambia el contenedor de datos (elegir carpeta). Persiste en config y
    aplica en el acto (DB + exportes apuntan ahi)."""
    global _DATA_ROOT, EXPORTES_DIR
    cfg = request.get_json(silent=True) or {}
    raw = str(cfg.get("path") or "").strip()
    if not raw:
        return jsonify(ok=False, error="ruta vacia"), 400
    from pathlib import Path
    root = Path(raw).expanduser()
    if not root.is_absolute():
        return jsonify(ok=False, error="la ruta tiene que ser absoluta"), 400
    if str(root).startswith("/tmp/.mount"):
        return jsonify(ok=False,
                        error="ahi no: es el montaje temporal de la AppImage "
                              "(desaparece al cerrar)"), 400
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".ru5600_write_test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return jsonify(ok=False, error="no se puede escribir ahi: %s" % exc), 400
    _DATA_ROOT = str(root)
    EXPORTES_DIR = None  # que se re-resuelva dentro del contenedor nuevo
    try:
        store.set_data_root(str(root))
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error="no pude usar esa carpeta: %s" % exc), 400
    _save_config({"data_dir": str(root)})
    _log("ok", "contenedor de datos: %s" % root)
    st = _data_stats()
    return jsonify(ok=True, **st)


@app.route("/api/data_pick", methods=["POST"])
def api_data_pick():
    """Abre el dialogo NATIVO de carpetas (zenity) y devuelve la elegida.
    Sin zenity: error y la UI ofrece escribir la ruta a mano."""
    import shutil
    import subprocess
    if not shutil.which("zenity"):
        return jsonify(ok=False, error="sin dialogo (falta zenity)",
                       manual=True)
    try:
        r = subprocess.run(["zenity", "--file-selection", "--directory",
                            "--title=Carpeta de datos RU5600"],
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="se agoto la espera del dialogo")
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:120])
    path = (r.stdout or "").strip()
    if r.returncode != 0 or not path:
        return jsonify(ok=False, error="cancelado")
    return jsonify(ok=True, path=path)


@app.route("/api/data_open", methods=["POST"])
def api_data_open():
    """Abre el contenedor en el gestor de archivos (xdg-open)."""
    import shutil
    import subprocess
    root = str(_data_root())
    if not shutil.which("xdg-open"):
        return jsonify(ok=False, error="sin xdg-open", path=root)
    try:
        subprocess.Popen(["xdg-open", root],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:120], path=root)
    return jsonify(ok=True, path=root)


@app.route("/api/udev_install", methods=["POST"])
def api_udev_install():
    """Instala la regla USB en 1 clic: pkexec (dialogo de clave del sistema)
    con un script FIJO (sin input del usuario adentro: no hay inyeccion).
    Sin pkexec: indica el comando manual."""
    import shutil
    import subprocess
    if not shutil.which("pkexec"):
        return jsonify(ok=False, error="sin pkexec: usa el comando manual",
                       manual=True)
    rule = UDEV_RULE.rstrip("\n")
    script = ("set -e\n"
              "printf '%s\n' '" + rule + "' > "
              "/etc/udev/rules.d/99-inveton-ru5600.rules\n"
              "chmod 644 /etc/udev/rules.d/99-inveton-ru5600.rules\n"
              "udevadm control --reload-rules\n"
              "udevadm trigger --subsystem-match=usb --action=add\n")
    try:
        r = subprocess.run(["pkexec", "sh", "-c", script],
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="se agoto la espera del dialogo")
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:120])
    if r.returncode != 0:
        err = (r.stderr or "").strip().split("\n")[-1] if r.stderr else ""
        if r.returncode == 127:
            return jsonify(ok=False,
                            error="cancelado o clave incorrecta"), 200
        return jsonify(ok=False,
                        error=("pkexec fallo (%d): %s" % (r.returncode, err))[:160])
    _log("ok", "regla USB instalada via pkexec")
    return jsonify(ok=True, steps=["regla escrita en /etc/udev/rules.d/",
                                   "udev recargado",
                                   "si no conecta: desenchufa y enchufa el lector"])


@app.route("/api/version")
def api_version():
    """Version corriendo + estado del release mas nuevo (si hay con que comparar)."""
    cur = _app_version()
    try:
        _port = int(str(request.host or "").rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        _port = None
    out = {"version": cur, "port": _port, "update": None}
    latest, origen = _find_latest_json()
    if latest and isinstance(latest.get("version"), str):
        newer = _version_tuple(latest["version"]) > _version_tuple(cur)
        out["update"] = {
            "available": newer,
            "latest": latest["version"],
            "file": latest.get("file", ""),
            "sha256": latest.get("sha256", ""),
            "date": latest.get("date", ""),
            "notes": latest.get("notes", ""),
            "source": origen.split(":")[0],
        }
    return jsonify(out)


def _start_native(host="127.0.0.1", port=8000):
    """Ventana nativa con pywebview (usa WebKit2 del sistema).
    Flask corre en un hilo daemon; pywebview ocupa el hilo principal.
    Si pywebview no está disponible, cae al modo navegador."""
    import threading
    import time
    import urllib.request

    def _serve():
        app.run(host=host, port=port, threaded=True, use_reloader=False)

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    # esperar a que Flask esté listo
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://{host}:{port}/", timeout=0.5)
            break
        except Exception:
            time.sleep(0.2)

    try:
        import webview
    except ImportError:
        import sys as _sys
        print("RU5600 Reader: falta pywebview (ventana nativa). "
              "Esta app no abre navegadores.", file=_sys.stderr)
        _sys.exit(2)
    try:
        # Identidad de la ventana para el dock/barra: sin esto, GTK toma el
        # nombre del script (argv[0] = "app.py") como WM_CLASS y el dock
        # muestra "app.py" en vez del programa. Debe ir antes de start().
        # El icono de la ventana (barra de tareas sin integrar) sale del PNG
        # embebido en la AppImage.
        try:
            import gi
            gi.require_version('Gtk', '3.0')
            from gi.repository import GLib, Gtk
            GLib.set_prgname("RU5600 Reader")
            GLib.set_application_name("RU5600 Reader")
            _here = os.path.dirname(os.path.abspath(__file__))
            # app.py vive en $MOUNT/usr/opt/ru5600-web/ ; el icono esta en la
            # raiz del AppDir ($MOUNT/ru5600-reader.png)
            _root = _here
            for _ in range(3):
                _root = os.path.dirname(_root)
            for _cand in (os.path.join(_root, "ru5600-reader.png"),
                          os.path.join(_here, "ru5600-reader.png")):
                if os.path.isfile(_cand):
                    Gtk.Window.set_default_icon_from_file(_cand)
                    break
        except Exception:
            pass
        webview.create_window("RU5600 Reader", f"http://{host}:{port}",
                             width=1280, height=880, min_size=(800, 600))
        webview.start()
    except Exception as exc:
        # Sin ventana nativa no hay plan B (esta app no abre navegadores).
        import sys
        print(f"RU5600 Reader: no pude abrir la ventana nativa ({exc}). "
              f"Si falta WebKit2: sudo apt install gir1.2-webkit2-4.1",
              file=sys.stderr)
        sys.exit(1)


# Al arrancar (importar): el contenedor unico queda fijado y la DB
# apunta ahi. Migra una vez el layout viejo (DATA/ + EXPORTES/ junto al
# AppImage) si el contenedor esta fresco.
_migrate_legacy_data()
try:
    store.set_data_root(str(_data_root()))
except Exception as _exc:  # noqa: BLE001 - sin datos no hay app
    _log("warn", "contenedor de datos no usable: %s" % _exc)


if __name__ == "__main__":
    import argparse
    _p = argparse.ArgumentParser()
    _p.add_argument("--native", action="store_true",
                    help="abre una ventana nativa (pywebview) en vez de navegador")
    _p.add_argument("--no-browser", action="store_true",
                    help="no abre navegador (solo el servidor)")
    _p.add_argument("--port", type=int, default=8000)
    _args = _p.parse_args()
    _port = _pick_port(_args.port)
    if _args.native:
        _start_native(port=_port)
    else:
        # Sin --native: solo servir. NO abrir el navegador solo: las aperturas
        # automaticas (xdg-open en cada relanzamiento) dejaban pestanas
        # viejas abiertas y el Creador pensaba que la app corria en el
        # navegador en vez de la ventana nativa. Quien quiera navegador,
        # abre http://127.0.0.1:PUERTO a mano.
        if not _args.no_browser:
            print(f"Abrí http://127.0.0.1:{_port} en tu navegador",
                  flush=True)
        print(f"RU5600 web en http://127.0.0.1:{_port} (Ctrl+C para cortar)",
              flush=True)
        app.run(host="127.0.0.1", port=_port, threaded=True)
