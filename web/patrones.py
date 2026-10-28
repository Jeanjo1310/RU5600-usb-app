"""Patrones EPC: analisis determinista de planillas (offline, sin IA).

Reglas (spec 2026-10-28-patrones-epc-design):
- Autodeteccion de columna EPC: encabezados {epc,tag,epc_code,codigo}
  case-insensitive; si no, regex ^[0-9A-Fa-f]{12,64}$ por columna (la de
  mas coincidencias, minimo 1); si nada, error "no se encontro columna EPC".
- Cortes: short = pos 19-24 (6 hex), tagid = pos 17-24, w34 = pos 7-14
  (indices 0-based 6:14, 16:24, 18:24). Decimales con ceros a la izquierda
  (10 digitos w34/tagid, 8 short).
- EPCs de largo != 24 o no-hex -> rareza (igual van a la tabla con los
  campos derivados vacios).
- Familia probable por encabezado (sin TID), siempre " (candidato)".
- Choques: EPC exacto (upper) O W34 numerico (int, sin ceros) contra
  ambos padrones. Veredicto compatible / con choques.
"""
from __future__ import annotations

import csv
import io
import re

HEADER_NAMES = {"epc", "tag", "epc_code", "codigo"}
_HEX_COL = re.compile(r"^[0-9A-Fa-f]{12,64}$")


def norm_epc(v) -> str:
    s = str(v if v is not None else "").strip().upper()
    return s.replace(" ", "").replace(":", "").replace("-", "")


def familia_de(epc: str) -> str:
    # Patron auto-detectado por prefijo: cualquier familia (H10, H9, ...)
    # emerge sola con su etiqueta; lo desconocido usa su propio prefijo.
    p = (epc or "")[:6]
    if p == "E20047":
        return "H10 (candidato)"
    if p == "AA0004":
        return "ZKTECO AA (candidato)"
    if p in ("E22004", "B22004"):
        return "ZKTECO E2/B2 (candidato)"
    return ("%s (patrón)" % p) if p else "pendiente (candidato)"


def derivar(epc: str) -> dict:
    """Cortes + decimales. Solo para EPCs validos (24 hex)."""
    w34 = epc[6:14]
    tagid = epc[16:24]
    short = epc[18:24]
    return {
        "w34hex": w34,
        "w34dec": str(int(w34, 16)).zfill(10),
        "tagidhex": tagid,
        "tagiddec": str(int(tagid, 16)).zfill(10),
        "shorthex": short,
        "shortdec": str(int(short, 16)).zfill(8),
        "familia": familia_de(epc),
    }


def _find_epc_col(headers) -> int | None:
    for i, h in enumerate(headers):
        if str(h or "").strip().lower() in HEADER_NAMES:
            return i
    return None


def _extract_sheet(rows) -> tuple[list[str], bool]:
    """Devuelve (epcs, tiene_columna). Filas vacias se saltean."""
    data = [r for r in rows if any(v not in (None, "") and str(v).strip() for v in r)]
    if not data:
        return [], False
    col = _find_epc_col(data[0])
    if col is not None:
        body = data[1:]
        if not any(col < len(r) and str(r[col] or "").strip() for r in body):
            # el encabezado coincidio pero la columna esta vacia: probar regex
            col = None
        else:
            out = []
            for r in body:
                v = norm_epc(r[col] if col < len(r) else "")
                if v:
                    out.append(v)
            return out, True
    best, best_n = None, 0
    width = max(len(r) for r in data)
    for i in range(width):
        n = sum(1 for r in data
                if i < len(r) and _HEX_COL.match(str(r[i] or "").strip()))
        if n > best_n:
            best, best_n = i, n
    if best is None:
        return [], False
    return ([norm_epc(r[best]) for r in data
             if best < len(r) and str(r[best] or "").strip()], True)


def extract_epcs_xlsx(raw: bytes) -> list[str]:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    epcs: list[str] = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        found, ok = _extract_sheet(rows)
        if ok:
            epcs.extend(found)
    if not epcs:
        # ninguna hoja con columna EPC: distinguir vacio de indetectable
        raise ValueError("no se encontró columna EPC")
    return epcs


def extract_epcs_csv(raw: bytes) -> list[str]:
    text = raw.decode("utf-8-sig", "replace")
    rows = [r for r in csv.reader(io.StringIO(text))]
    epcs, ok = _extract_sheet(rows)
    if not ok or not epcs:
        raise ValueError("no se encontró columna EPC")
    return epcs


def analyze_epcs(epcs: list[str], pad_h10=None, pad_zk=None) -> dict:
    pad_h10 = pad_h10 or {"epcs": set(), "w34": set()}
    pad_zk = pad_zk or {"epcs": set(), "w34": set()}
    rows, rarezas = [], []
    seen: dict[str, int] = {}
    for i, epc in enumerate(epcs, start=1):
        seen[epc] = seen.get(epc, 0) + 1
        raro = None
        if (len(epc) != 24 or any(c not in "0123456789ABCDEF" for c in epc)
                or set(epc) == {"0"}):
            motivos = []
            if len(epc) != 24:
                motivos.append("largo %d (!=24)" % len(epc))
            if any(c not in "0123456789ABCDEF" for c in epc):
                motivos.append("no-hex")
            if set(epc) <= {"0"}:
                motivos.append("ceros (sin programar)")
            raro = "; ".join(motivos)
            rarezas.append({"fila": i, "epc": epc, "motivo": raro})
            rows.append([i, epc, "", "", "", "", "", "", ""])
        else:
            d = derivar(epc)
            rows.append([i, epc, d["w34hex"], d["w34dec"], d["tagidhex"],
                         d["tagiddec"], d["shorthex"], d["shortdec"],
                         d["familia"]])
    duplicados = sorted(e for e, n in seen.items() if n > 1)
    familias: dict[str, int] = {}
    for epc in epcs:
        familias[epc[:6] or "?"] = familias.get(epc[:6] or "?", 0) + 1
    validos = [r for r in rows if r[2]]
    marcadores = {}
    tramos = {"pos01-06": lambda e: e[0:6], "pos07-14": lambda e: e[6:14],
              "pos15-16": lambda e: e[14:16], "pos17-24": lambda e: e[16:24],
              "pos19-24": lambda e: e[18:24]}
    nval = len(validos)
    for nombre, fn in tramos.items():
        vals: dict[str, int] = {}
        for r in validos:
            v = fn(r[1])
            vals[v] = vals.get(v, 0) + 1
        if 1 <= len(vals) < 5:
            marcadores[nombre] = ", ".join(
                "%s (%d/%d)" % (v, c, nval)
                for v, c in sorted(vals.items(), key=lambda kv: -kv[1]))
    choques = {"h10": [], "zkteco": []}
    for r in validos:
        epc, w = r[1], int(r[2], 16)
        if epc in pad_h10["epcs"] or w in pad_h10["w34"]:
            choques["h10"].append(epc)
        if epc in pad_zk["epcs"] or w in pad_zk["w34"]:
            choques["zkteco"].append(epc)
    veredicto = "compatible" if not choques["h10"] and not choques["zkteco"] else "con choques"
    return {"rows": rows,
            "summary": {"total": len(epcs), "familias": familias,
                        "marcadores": marcadores, "duplicados": duplicados,
                        "rarezas": rarezas, "choques": choques,
                        "veredicto": veredicto}}


def padron_sets(epcs: list[str]) -> dict:
    out = {"epcs": set(), "w34": set()}
    for e in epcs:
        if len(e) == 24 and all(c in "0123456789ABCDEF" for c in e):
            out["epcs"].add(e)
            out["w34"].add(int(e[6:14], 16))
    return out


def observaciones_lines(payload: dict, filename: str) -> list[str]:
    s = payload["summary"]
    lines = ["Análisis de %s (%d filas)" % (filename, s["total"]),
             "Veredicto: %s" % s["veredicto"],
             "Familias: " + (", ".join("%s=%d" % kv for kv in sorted(s["familias"].items())) or "-")]
    for tramo, texto in s["marcadores"].items():
        lines.append("Marcador %s: %s" % (tramo, texto))
    lines.append("Duplicados internos (%d): %s"
                 % (len(s["duplicados"]), ", ".join(s["duplicados"]) or "-"))
    for r in s["rarezas"]:
        lines.append("Rareza fila %d: %s (%s)" % (r["fila"], r["epc"], r["motivo"]))
    for kind in ("h10", "zkteco"):
        lines.append("Choques %s (%d): %s"
                     % (kind, len(s["choques"][kind]),
                        ", ".join(s["choques"][kind]) or "-"))
    for w in payload.get("warnings", []):
        lines.append("Aviso: " + w)
    return lines


EJEMPLOS_FORMULAS = [
    ("EPC ejemplo (celda B2)", None),
    ("W34 = pos 7-14", "'=MID(B2,7,8)"),
    ("Card 10 digitos", "'=TEXT(HEX2DEC(MID(B2,7,8)),\"0000000000\")"),
    ("Tag ID = pos 17-24", "'=MID(B2,17,8)"),
    ("Tag ID decimal", "'=TEXT(HEX2DEC(MID(B2,17,8)),\"0000000000\")"),
    ("Short = pos 19-24", "'=MID(B2,19,6)"),
    ("Short decimal", "'=TEXT(HEX2DEC(MID(B2,19,6)),\"00000000\")"),
]


def build_report(payload: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "DATOS"
    header = ["id", "epc", "w34hex", "w34dec", "tagidhex", "tagiddec",
              "shorthex", "shortdec", "familia"]
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in payload["rows"]:
        ws.append(r)
    for i, h in enumerate(header, start=1):
        col = ws.cell(row=1, column=i).column_letter
        ancho = max(len(h), *(len(str(r[i - 1])) for r in payload["rows"])) if payload["rows"] else len(h)
        ws.column_dimensions[col].width = min(32, max(12, ancho + 2))
    ws2 = wb.create_sheet("OBSERVACIONES")
    for line in observaciones_lines(payload, payload.get("filename", "")):
        ws2.append([line])
    ws2.column_dimensions["A"].width = 100
    ws3 = wb.create_sheet("EJEMPLOS")
    ws3.append(["uso", "formula (texto, no activa)"])
    for c in ws3[1]:
        c.font = Font(bold=True)
    first = next((r[1] for r in payload["rows"] if len(r[1]) == 24), "")
    ws3.append(["EPC ejemplo (celda B2)", first])
    for uso, formula in EJEMPLOS_FORMULAS[1:]:
        ws3.append([uso, formula])
    ws3.column_dimensions["A"].width = 28
    ws3.column_dimensions["B"].width = 48
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
