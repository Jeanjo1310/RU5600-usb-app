#!/usr/bin/env bash
# Build the RU5600 Reader AppImage.
#
#   ./build_appimage.sh [output_dir]
#
# Requires: appimagetool (set APPIMAGETOOL or put it on PATH), mksquashfs.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
OUT="${1:-$ROOT/dist}"
APP_NAME="ru5600-reader"
APP_VERSION="${APP_VERSION:-1.0.0}"
APPIMAGE="${APP_NAME}-${APP_VERSION}-x86_64.AppImage"

APPIMAGETOOL="${APPIMAGETOOL:-}"
if [ -z "$APPIMAGETOOL" ]; then
    if command -v appimagetool >/dev/null 2>&1; then
        APPIMAGETOOL="$(command -v appimagetool)"
    elif [ -x "$ROOT/../deliverables/tools_appimgetool" ]; then
        APPIMAGETOOL="$ROOT/../deliverables/tools_appimgetool"
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

install -D -m 755 "$HERE/AppRun"                "$STAGE/AppRun"
install -D -m 644 "$HERE/$APP_NAME.desktop"     "$STAGE/$APP_NAME.desktop"
install -D -m 644 "$HERE/$APP_NAME.png"         "$STAGE/$APP_NAME.png"
install -D -m 644 "$HERE/$APP_NAME.png"         "$STAGE/.DirIcon"

install -D -m 644 "$ROOT/src/ru5600_core.py"    "$STAGE/usr/opt/$APP_NAME/ru5600_core.py"
install -D -m 644 "$ROOT/src/ru5600_chip.py"    "$STAGE/usr/opt/$APP_NAME/ru5600_chip.py"
install -D -m 644 "$ROOT/src/ru5600_net.py"     "$STAGE/usr/opt/$APP_NAME/ru5600_net.py"
install -D -m 644 "$ROOT/src/ru5600_gui.py"     "$STAGE/usr/opt/$APP_NAME/ru5600_gui.py"
install -D -m 644 "$ROOT/src/ru5600_cli.py"     "$STAGE/usr/opt/$APP_NAME/ru5600_cli.py"
install -D -m 644 "$ROOT/src/ru5600_selftest.py" "$STAGE/usr/opt/$APP_NAME/ru5600_selftest.py"
install -D -m 644 "$ROOT/PROTOCOL.md"           "$STAGE/usr/opt/$APP_NAME/PROTOCOL.md"
install -D -m 644 "$ROOT/README.md"             "$STAGE/usr/opt/$APP_NAME/README.md"

# Vendor USB library (cross platform wrapper shipped with the RU5600 SDK).
install -D -m 755 "$ROOT/vendor/libCFHidApi.so" "$STAGE/usr/opt/$APP_NAME/vendor/libCFHidApi.so"

# Fallback libusb: libCFHidApi.so has no DT_NEEDED entry for libusb, so we
# preload it ourselves.  The system copy is preferred at runtime.
if [ -f "$ROOT/vendor/libusb-1.0.so.0" ]; then
    install -D -m 755 "$ROOT/vendor/libusb-1.0.so.0" "$STAGE/usr/opt/$APP_NAME/vendor/libusb-1.0.so.0"
fi

# udev rule, offered in-app for one click install (needs admin rights).
install -D -m 644 "$ROOT/packaging/99-inveton-ru5600.rules" \
    "$STAGE/usr/opt/$APP_NAME/99-inveton-ru5600.rules"

mkdir -p "$OUT"
OUT="$(realpath "$OUT")"

ARCH=x86_64 "$APPIMAGETOOL" "$STAGE" "$OUT/$APPIMAGE"
chmod +x "$OUT/$APPIMAGE"

( cd "$OUT" && sha256sum "$APPIMAGE" > "SHA256SUMS.txt" )
echo "Built: $OUT/$APPIMAGE"
