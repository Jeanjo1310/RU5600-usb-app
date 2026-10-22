#!/bin/sh
# Install the dependencies of this AppImage and make it runnable.
#
#   ./install.sh            install what is missing and prepare the app
#   ./install.sh --check    only report what is missing (no changes)
#
# The AppImage itself carries the application and the vendor's USB
# library.  What it cannot carry is the Python/GTK stack of the system
# and the udev rule that gives access to the reader without root.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
APPIMAGE="$(ls "$HERE"/*.AppImage 2>/dev/null | head -1)"
CHECK_ONLY=0
[ "$1" = "--check" ] && CHECK_ONLY=1

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[0;32mok\033[0m      %s\n' "$*"; }
warn() { printf '    \033[0;33mfalta\033[0m   %s\n' "$*"; }
err()  { printf '    \033[0;31merror\033[0m  %s\n' "$*" >&2; }

if [ -z "$APPIMAGE" ]; then
    err "no hay ninguna AppImage en $HERE"
    exit 1
fi
say "AppImage: $(basename "$APPIMAGE")"

# ---------------------------------------------------------------- distro
PKG=""
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; else
        err "hace falta root o sudo para instalar paquetes"
        exit 1
    fi
fi

if command -v apt-get >/dev/null 2>&1; then
    PKG="apt"
    DEBS="python3-gi gir1.2-gtk-3.0 libusb-1.0-0 python3-serial"
elif command -v dnf >/dev/null 2>&1; then
    PKG="dnf"
    DEBS="python3-gobject gtk3 libusb python3-pyserial"
elif command -v pacman >/dev/null 2>&1; then
    PKG="pacman"
    DEBS="python-gobject gtk3 libusb python-pyserial"
elif command -v zypper >/dev/null 2>&1; then
    PKG="zypper"
    DEBS="python3-gobject gtk3 libusb python3-pyserial"
else
    warn "no se reconoce la distro: instalá a mano python3-gi, gtk3 y libusb"
    PKG=""
fi

# ------------------------------------------------------------- chequeo
say "Chequeando dependencias"
MISSING=""
python3 -c 'import gi; gi.require_version("Gtk","3.0")' >/dev/null 2>&1 \
    && ok "PyGObject con GTK 3" || { warn "PyGObject con GTK 3"; MISSING="$MISSING gtk"; }
python3 -c 'import serial' >/dev/null 2>&1 \
    && ok "pyserial (antenas por RS-232)" \
    || { warn "pyserial (antenas por RS-232)"; MISSING="$MISSING serial"; }
ldconfig -p 2>/dev/null | grep -q libusb-1.0.so.0 \
    && ok "libusb-1.0" \
    || { warn "libusb-1.0 (viene de respaldo dentro de la AppImage)"; }
[ -n "$PKG" ] && ok "gestor de paquetes: $PKG" || warn "sin gestor de paquetes"

# ------------------------------------------------------------- udev
say "Permisos del lector USB"
RULE_SRC=""
for cand in "$HERE"/rules/99-inveton-ru5600.rules \
            "$HERE"/99-inveton-ru5600.rules; do
    [ -f "$cand" ] && RULE_SRC="$cand" && break
done
RULE_DST="/etc/udev/rules.d/99-inveton-ru5600.rules"
if [ -f "$RULE_DST" ]; then
    ok "regla udev instalada"
else
    warn "regla udev ausente ($RULE_DST)"
    if [ -n "$RULE_SRC" ] && [ "$CHECK_ONLY" -eq 0 ]; then
        $SUDO install -m 644 "$RULE_SRC" "$RULE_DST" && ok "regla instalada"
        $SUDO udevadm control --reload-rules && $SUDO udevadm trigger || true
    fi
fi

# --------------------------------------------------------- ejecutable
say "Permisos de ejecución"
if [ "$CHECK_ONLY" -eq 0 ]; then
    chmod +x "$APPIMAGE" 2>/dev/null && ok "ejecutable"
    mkdir -p "$HOME/.local/bin" 2>/dev/null && {
        ln -sf "$APPIMAGE" "$HOME/.local/bin/$(basename "$APPIMAGE" .AppImage)" \
            2>/dev/null && ok "enlace en ~/.local/bin" || true
    }
else
    [ -x "$APPIMAGE" ] && ok "ejecutable" || warn "sin permiso de ejecucion"
fi

# ------------------------------------------------------------- resumen
if [ "$CHECK_ONLY" -eq 1 ]; then
    say "Solo chequeo. Falta:${MISSING:- nada}"
    exit 0
fi

if [ -n "$MISSING" ]; then
    say "Instalando: $MISSING"
    case "$PKG" in
        apt)    $SUDO apt-get update -qq && $SUDO apt-get install -y $DEBS ;;
        dnf)    $SUDO dnf install -y $DEBS ;;
        pacman) $SUDO pacman -Sy --noconfirm $DEBS ;;
        zypper) $SUDO zypper -n install $DEBS ;;
        *)      err "instalá a mano: $MISSING" ;;
    esac
fi

say "Listo. Para abrir la AppImage:"
printf '    %s\n' "$APPIMAGE"
say "O desde el menu de aplicaciones (se instaló el .desktop)."
