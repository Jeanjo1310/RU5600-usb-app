#!/usr/bin/env bash
# Build the RU5600 Reader WEB AppImage (Flask + browser UI).
#
#   ./build_appimage_web.sh [output_dir]
#
# Environment:
#   APP_VERSION   override the version (default: packaging/VERSION)
#   APPIMAGETOOL  path to appimagetool (default: PATH or the extracted copy
#                 in ../../deliverables/squashfs-root)
#
# Produces:
#   ru5600-web-<ver>-x86_64.AppImage   the image (self-contained: bundles
#                                      flask+openpyxl; the host only needs
#                                      python3)
#   latest.json                        release manifest for the in-app update
#                                      check (drop it NEXT TO the AppImage on
#                                      the USB stick, or serve it via
#                                      RU5600_UPDATE_URL)
#   SHA256SUMS.txt                     checksums
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
OUT="${1:-$ROOT/dist}"
APP_NAME="ru5600-web"
APP_VERSION="${APP_VERSION:-$(tr -d '[:space:]' < "$HERE/VERSION")}"
APPIMAGE="${APP_NAME}-${APP_VERSION}-x86_64.AppImage"

APPIMAGETOOL="${APPIMAGETOOL:-}"
if [ -z "$APPIMAGETOOL" ]; then
    if command -v appimagetool >/dev/null 2>&1; then
        APPIMAGETOOL="$(command -v appimagetool)"
    elif [ -x "$ROOT/../deliverables/squashfs-root/usr/bin/appimagetool" ]; then
        APPIMAGETOOL="$ROOT/../deliverables/squashfs-root/usr/bin/appimagetool"
    else
        echo "appimagetool not found. Set APPIMAGETOOL=/path/to/appimagetool" >&2
        exit 1
    fi
fi

# the protocol self test must pass before anything is packaged
echo "== protocol self test =="
( cd "$ROOT/src" && python3 ru5600_selftest.py ) || {
    echo "self test FAILED - not packaging" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

install -D -m 755 "$HERE/AppRun.web"             "$STAGE/AppRun"
install -D -m 644 "$HERE/$APP_NAME.desktop"      "$STAGE/$APP_NAME.desktop"
install -D -m 644 "$HERE/ru5600-reader.png"      "$STAGE/$APP_NAME.png"
install -D -m 644 "$HERE/ru5600-reader.png"      "$STAGE/.DirIcon"

# --- python sources + web app ------------------------------------------------
install -D -m 644 "$ROOT/src/ru5600_core.py"     "$STAGE/usr/opt/$APP_NAME/ru5600_core.py"
install -D -m 644 "$ROOT/src/ru5600_chip.py"     "$STAGE/usr/opt/$APP_NAME/ru5600_chip.py"
install -D -m 644 "$ROOT/src/ru5600_net.py"      "$STAGE/usr/opt/$APP_NAME/ru5600_net.py"
install -D -m 644 "$ROOT/src/ru5600_i18n.py"     "$STAGE/usr/opt/$APP_NAME/ru5600_i18n.py"
install -D -m 644 "$ROOT/web/app.py"             "$STAGE/usr/opt/$APP_NAME/app.py"
install -D -m 644 "$ROOT/web/ru5600_store.py"    "$STAGE/usr/opt/$APP_NAME/ru5600_store.py"
install -D -m 644 "$HERE/VERSION"                "$STAGE/usr/opt/$APP_NAME/VERSION"
cp -r "$ROOT/web/templates"                      "$STAGE/usr/opt/$APP_NAME/templates"
cp -r "$ROOT/web/static"                         "$STAGE/usr/opt/$APP_NAME/static"
# node_modules es solo de build (Tailwind): no viaja
rm -rf "$STAGE/usr/opt/$APP_NAME/static/node_modules"

# --- vendor USB library ------------------------------------------------------
install -D -m 755 "$ROOT/vendor/libCFHidApi.so"  "$STAGE/usr/opt/$APP_NAME/vendor/libCFHidApi.so"
if [ -f "$ROOT/vendor/libusb-1.0.so.0" ]; then
    install -D -m 755 "$ROOT/vendor/libusb-1.0.so.0" \
        "$STAGE/usr/opt/$APP_NAME/vendor/libusb-1.0.so.0"
fi

# --- bundled python deps (flask + openpyxl + tree), no network at run time ---
echo "== bundling python deps =="
python3 - "$STAGE/usr/opt/$APP_NAME/pylibs" <<'PY'
import importlib, shutil, sys, zipfile
from pathlib import Path
target = Path(sys.argv[1]); target.mkdir(parents=True, exist_ok=True)
# paquetes de third party que la app importa (el stdlib lo pone el python del host)
roots = ["flask", "openpyxl", "werkzeug", "jinja2", "click", "itsdangerous",
         "markupsafe", "blinker", "et_xmlfile"]
seen = set()
for name in roots:
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        continue
    if spec is None or not spec.submodule_search_locations:
        continue
    for loc in spec.submodule_search_locations:
        dst = target / Path(loc).name
        if Path(loc).resolve() in seen:
            continue
        seen.add(Path(loc).resolve())
        shutil.copytree(loc, dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
print("bundled:", ", ".join(sorted(p.name for p in target.iterdir())))
PY
echo "== bundling pywebview (native window) =="
python3 - "$STAGE/usr/opt/$APP_NAME/pylibs" "$HERE/vendor/python/pywebview-6.2.1-py3-none-any.whl" "$HERE/vendor/python/proxy_tools-0.1.0.tar.gz" "$HERE/vendor/python/bottle-0.13.4-py2.py3-none-any.whl" <<'PYINNER'
import sys, zipfile, tarfile, shutil
from pathlib import Path
target = Path(sys.argv[1])
whl = Path(sys.argv[2])
tar = Path(sys.argv[3])
bottle = Path(sys.argv[4])
with zipfile.ZipFile(whl) as z:
    z.extractall(target)
with tarfile.open(tar) as t:
    for m in t.getmembers():
        if m.name.startswith("proxy_tools-0.1.0/proxy_tools/"):
            rel = m.name[len("proxy_tools-0.1.0/"):]
            if m.isdir():
                (target / rel).mkdir(parents=True, exist_ok=True)
            else:
                (target / rel).parent.mkdir(parents=True, exist_ok=True)
                with t.extractfile(m) as src, open(target / rel, "wb") as dst:
                    dst.write(src.read())
with zipfile.ZipFile(bottle) as z:
    z.extractall(target)
for p in list(target.rglob("__pycache__")):
    shutil.rmtree(p, ignore_errors=True)
print("pywebview + proxy_tools + bottle extraidos")
PYINNER

mkdir -p "$OUT"
OUT="$(realpath "$OUT")"
ARCH=x86_64 "$APPIMAGETOOL" "$STAGE" "$OUT/$APPIMAGE"
chmod +x "$OUT/$APPIMAGE"

SHA="$(sha256sum "$OUT/$APPIMAGE" | cut -d' ' -f1)"
BYTES="$(stat -c%s "$OUT/$APPIMAGE")"
NOTES="${RELEASE_NOTES:-}"
python3 - "$OUT" "$APPIMAGE" "$APP_VERSION" "$SHA" "$BYTES" "$NOTES" <<'PY'
import json, sys, datetime
out, name, ver, sha, size, notes = sys.argv[1:7]
manifest = {
    "name": "ru5600-web",
    "version": ver,
    "file": name,
    "sha256": sha,
    "bytes": int(size),
    "date": datetime.date.today().isoformat(),
    "notes": notes,
}
with open(f"{out}/latest.json", "w") as fh:
    json.dump(manifest, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print("manifest:", f"{out}/latest.json")
PY

( cd "$OUT" && sha256sum "$APPIMAGE" latest.json > SHA256SUMS.txt )
echo "Built: $OUT/$APPIMAGE"
echo "Manifest: $OUT/latest.json (put it next to the AppImage, or serve it)"
