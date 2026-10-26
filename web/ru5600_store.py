"""Persistencia local del piloto web RU5600 (SQLite, solo lectura de tags).

La base vive en ``web/data/ru5600.db`` y no se versiona.  Cada funcion abre
su propia conexion: el servidor Flask corre con ``threaded=True`` y una
conexion compartida entre hilos de ``sqlite3`` no es segura.

Un ``tags`` es un tag fisico identificado por su EPC.  ``lecturas`` guarda
cada lectura confirmada (una fila por sesion de lectura, no por intento) y
``log`` los eventos que tienen que sobrevivir al reinicio del servidor.
``trabajos`` guarda los procesos por lote y los benchmarks de antena.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "ru5600.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tags (
    id            INTEGER PRIMARY KEY,
    epc           TEXT NOT NULL UNIQUE,
    chip          TEXT,
    rssi          INTEGER,
    veces         INTEGER NOT NULL DEFAULT 1,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    attempts_avg  REAL
);

CREATE TABLE IF NOT EXISTS lecturas (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    epc       TEXT NOT NULL,
    rssi      INTEGER,
    distance  TEXT,
    attempts  INTEGER,
    ms        INTEGER,
    source    TEXT
);

CREATE TABLE IF NOT EXISTS trabajos (
    id      INTEGER PRIMARY KEY,
    tipo    TEXT NOT NULL,
    estado  TEXT NOT NULL,
    total   INTEGER,
    hechos  INTEGER,
    creado  TEXT NOT NULL,
    extra   TEXT
);

CREATE TABLE IF NOT EXISTS log (
    id     INTEGER PRIMARY KEY,
    ts     TEXT NOT NULL,
    level  TEXT,
    msg    TEXT
);

CREATE TABLE IF NOT EXISTS mediciones (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    label        TEXT NOT NULL,
    distance_cm  REAL NOT NULL,
    epc          TEXT NOT NULL,
    chip         TEXT,
    lecturas     INTEGER NOT NULL DEFAULT 0,
    dps          REAL,
    rssi_avg     REAL,
    rssi_min     INTEGER,
    rssi_max     INTEGER
);
"""

UPSERT_TAG = """
INSERT INTO tags (epc, chip, rssi, veces, first_seen, last_seen, attempts_avg)
VALUES (?, ?, ?, 1, ?, ?, ?)
ON CONFLICT(epc) DO UPDATE SET
    veces = tags.veces + 1,
    chip = COALESCE(excluded.chip, tags.chip),
    rssi = COALESCE(excluded.rssi, tags.rssi),
    last_seen = excluded.last_seen,
    attempts_avg = CASE
        WHEN excluded.attempts_avg IS NULL THEN tags.attempts_avg
        WHEN tags.attempts_avg IS NULL THEN excluded.attempts_avg
        ELSE ROUND((tags.attempts_avg * tags.veces + excluded.attempts_avg)
                   / (tags.veces + 1), 2)
    END
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    """Crea la carpeta y las tablas si faltan. Idempotente."""
    os.makedirs(DATA_DIR, exist_ok=True)
    con = _connect()
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def _clean_epc(epc: str) -> str:
    value = "".join(str(epc or "").split()).upper()
    if not value:
        raise ValueError("epc vacio")
    return value


def _clean_chip(chip) -> str | None:
    value = str(chip or "").strip()
    return value or None


def _clean_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def upsert_tag(epc: str, chip=None, rssi=None, attempts=None) -> dict:
    """Suma una lectura del tag ``epc`` y recalcula su promedio de intentos.

    ``veces`` cuenta relecturas del mismo EPC.  ``attempts_avg`` es el
    promedio de intentos que costo encontrarlo en cada sesion confirmada.
    """
    epc = _clean_epc(epc)
    now = _now()
    avg = None if attempts is None else round(float(attempts), 2)
    con = _connect()
    try:
        con.execute(UPSERT_TAG, (epc, _clean_chip(chip), _clean_int(rssi),
                                 now, now, avg))
        con.commit()
        row = con.execute("SELECT * FROM tags WHERE epc = ?", (epc,)).fetchone()
    finally:
        con.close()
    return dict(row) if row is not None else {}


MEDICION_COLS = ("id", "ts", "label", "distance_cm", "epc", "chip",
                 "lecturas", "dps", "rssi_avg", "rssi_min", "rssi_max")


def add_medicion(label: str, distance_cm, epc: str, chip=None, lecturas=0,
                 dps=None, rssi_avg=None, rssi_min=None,
                 rssi_max=None) -> dict:
    """Guarda un punto de la curva distancia/RSSI. Devuelve la fila creada.

    ``label`` agrupa los puntos de una misma tanda (tag y altura) y
    ``distance_cm`` es la distancia del tag a la antena en esa medicion.
    """
    epc = _clean_epc(epc)
    name = str(label or "").strip()[:120]
    if not name:
        raise ValueError("label vacio")
    dist = _clean_float(distance_cm)
    if dist is None:
        raise ValueError("distance_cm invalida")
    con = _connect()
    try:
        cur = con.execute(
            "INSERT INTO mediciones (ts, label, distance_cm, epc, chip,"
            " lecturas, dps, rssi_avg, rssi_min, rssi_max)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), name, dist, epc, _clean_chip(chip),
             _clean_int(lecturas) or 0, _clean_float(dps),
             _clean_float(rssi_avg), _clean_int(rssi_min),
             _clean_int(rssi_max)))
        con.commit()
        row = con.execute("SELECT * FROM mediciones WHERE id = ?",
                          (cur.lastrowid,)).fetchone()
    finally:
        con.close()
    return dict(row) if row is not None else {}


def list_mediciones(limit: int = 500, label: str = "") -> list[dict]:
    """Ultimos ``limit`` puntos, en orden cronologico (para dibujar la curva)."""
    try:
        limit = max(1, min(int(limit), 5000))
    except (TypeError, ValueError):
        limit = 500
    sql = ("SELECT id, ts, label, distance_cm, epc, chip, lecturas, dps,"
           " rssi_avg, rssi_min, rssi_max FROM mediciones")
    args: list = []
    name = str(label or "").strip()
    if name:
        sql += " WHERE label = ?"
        args.append(name)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    con = _connect()
    try:
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    return [dict(row) for row in reversed(rows)]


def list_medicion_labels() -> list[str]:
    """Etiquetas presentes en ``mediciones``, de la mas nueva a la mas vieja."""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT label, MAX(id) AS ultimo FROM mediciones"
            " GROUP BY label ORDER BY ultimo DESC").fetchall()
    finally:
        con.close()
    return [row["label"] for row in rows]


def add_lectura(epc: str, rssi=None, distance=None, attempts=None,
                ms=None, source: str = "") -> int:
    """Guarda una lectura confirmada. Devuelve el id de la fila."""
    epc = _clean_epc(epc)
    con = _connect()
    try:
        cur = con.execute(
            "INSERT INTO lecturas (ts, epc, rssi, distance, attempts, ms, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), epc, _clean_int(rssi),
             None if distance in (None, "") else str(distance),
             _clean_int(attempts), _clean_int(ms), str(source or "")))
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def add_log(level: str, msg: str) -> int:
    """Guarda una linea de log que sobrevive al reinicio del servidor."""
    con = _connect()
    try:
        cur = con.execute("INSERT INTO log (ts, level, msg) VALUES (?, ?, ?)",
                          (_now(), str(level or ""), str(msg or "")[:400]))
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def add_trabajo(tipo: str, estado: str, total=None, hechos=None,
                extra=None) -> int:
    """Registra un trabajo (lote o benchmark) y devuelve el id de la fila.

    ``extra`` se guarda como JSON; si ya viene como texto, se respeta tal cual.
    """
    if extra is None:
        extra_txt = None
    elif isinstance(extra, str):
        extra_txt = extra
    else:
        extra_txt = json.dumps(extra, ensure_ascii=False)
    con = _connect()
    try:
        cur = con.execute(
            "INSERT INTO trabajos (tipo, estado, total, hechos, creado, extra)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (str(tipo or ""), str(estado or ""), _clean_int(total),
             _clean_int(hechos), _now(), extra_txt))
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def list_tags(limit: int = 200) -> list[dict]:
    """Tags guardados, el mas reciente primero."""
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    con = _connect()
    try:
        rows = con.execute(
            "SELECT id, epc, chip, rssi, veces, first_seen, last_seen,"
            " attempts_avg FROM tags ORDER BY last_seen DESC, id DESC"
            " LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    return [dict(row) for row in rows]


def stats() -> dict:
    """Totales y tasa de acierto global.

    La tasa es ``lecturas confirmadas / intentos usados``: cada fila de
    ``lecturas`` costo ``attempts`` lecturas dirigidas, asi que la suma de
    ``attempts`` es el denominador honesto.
    """
    con = _connect()
    try:
        total_tags = con.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        total_lecturas = con.execute("SELECT COUNT(*) FROM lecturas").fetchone()[0]
        intentos = con.execute(
            "SELECT COALESCE(SUM(attempts), 0) FROM lecturas").fetchone()[0]
        ultima = con.execute("SELECT MAX(ts) FROM lecturas").fetchone()[0]
    finally:
        con.close()
    intentos = int(intentos or 0)
    tasa = round(total_lecturas / intentos * 100, 1) if intentos else 0.0
    return {"tags": int(total_tags), "lecturas": int(total_lecturas),
            "intentos": intentos, "tasa_acierto": tasa,
            "ultima_lectura": ultima}


def clear_tags() -> dict:
    """Vacia tags y lecturas. El log no se toca (tiene su propio vaciado)."""
    con = _connect()
    try:
        lecturas = con.execute("DELETE FROM lecturas").rowcount
        tags = con.execute("DELETE FROM tags").rowcount
        con.commit()
    finally:
        con.close()
    return {"tags": int(tags), "lecturas": int(lecturas)}
