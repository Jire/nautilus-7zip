#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# nautilus-7zip installer — the full 7-Zip right-click menu for GNOME Files.
#
#   ./install.sh                 install for the current user (~/.local), no sudo needed
#   sudo ./install.sh --system   install for every user (/usr/local)
#   ./install.sh --uninstall     remove it again (add --system for a system install)
#
# It also works straight from the web (it downloads the latest version itself):
#   curl -fsSL https://raw.githubusercontent.com/Jire/nautilus-7zip/main/install.sh | bash
#
set -euo pipefail

REPO="Jire/nautilus-7zip"
BRANCH="${NAUTILUS_7ZIP_BRANCH:-main}"
APP_ID="io.github.jire.Nautilus7Zip"

MODE="user"
ACTION="install"
PREFIX=""
DESTDIR="${DESTDIR:-}"
PYTHON="${PYTHON:-}"
QUIET=0
RESTART=0
case "$(basename "$0")" in
  uninstall.sh) ACTION="uninstall" ;;
esac

if [ -t 1 ]; then
  BOLD=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RESET=$'\e[0m'
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi
say()  { [ "$QUIET" = 1 ] || printf '%s\n' "$*"; }
step() { [ "$QUIET" = 1 ] || printf '%s→%s %s\n' "$BOLD" "$RESET" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%s✗%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: install.sh [OPTIONS]

Installs the 7-Zip context menu for GNOME Files (Nautilus).

Options:
  --user          Install for the current user only (default; no sudo needed)
  --system        Install for all users (default prefix /usr/local; needs root)
  --prefix DIR    Installation prefix for --system (e.g. /usr)
  --destdir DIR   Stage the files under DIR (for distribution packages)
  --python PATH   Python interpreter that has PyGObject (default: /usr/bin/python3)
  --restart       Restart Files afterwards so the menu appears immediately
  --uninstall     Remove nautilus-7zip
  -q, --quiet     Only print errors
  -h, --help      Show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --user) MODE="user" ;;
    --system) MODE="system" ;;
    --prefix) PREFIX="${2:?--prefix needs a directory}"; MODE="system"; shift ;;
    --prefix=*) PREFIX="${1#*=}"; MODE="system" ;;
    --destdir) DESTDIR="${2:?--destdir needs a directory}"; shift ;;
    --destdir=*) DESTDIR="${1#*=}" ;;
    --python) PYTHON="${2:?--python needs a path}"; shift ;;
    --python=*) PYTHON="${1#*=}" ;;
    --restart) RESTART=1 ;;
    --uninstall|--remove) ACTION="uninstall" ;;
    -q|--quiet) QUIET=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "Unknown option: $1" ;;
  esac
  shift
done

SOURCE_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
# The copy installed as …/share/nautilus-7zip/uninstall.sh knows where it lives.
if [ "$ACTION" = "uninstall" ] && [ -z "$PREFIX" ] && [ "${SOURCE_DIR%/share/nautilus-7zip}" != "$SOURCE_DIR" ]; then
  case "$SOURCE_DIR" in
    "$HOME"/*) MODE="user" ;;
    *) MODE="system"; PREFIX="${SOURCE_DIR%/share/nautilus-7zip}" ;;
  esac
fi

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
if [ "$MODE" = "system" ]; then
  PREFIX="${PREFIX:-/usr/local}"
  DATADIR="$PREFIX/share"
  BINDIR="$PREFIX/bin"
else
  [ "$(id -u)" = 0 ] && [ -z "$DESTDIR" ] && warn "Running as root installs into root's home. Use --system for all users."
  DATADIR="${XDG_DATA_HOME:-$HOME/.local/share}"
  BINDIR="$HOME/.local/bin"
fi
PKGDATADIR="$DATADIR/nautilus-7zip"
HELPER="$PKGDATADIR/nautilus-7zip"
EXTDIR="$DATADIR/nautilus-python/extensions"
MANIFEST="$PKGDATADIR/installed-files.txt"

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
uninstall() {
  local root="${DESTDIR}"
  if [ ! -d "$root$PKGDATADIR" ] && [ ! -e "$root$EXTDIR/nautilus-7zip.py" ]; then
    say "nautilus-7zip is not installed in $DATADIR."
    return 0
  fi
  step "Removing nautilus-7zip from $DATADIR"
  if [ -f "$root$MANIFEST" ]; then
    while IFS= read -r file; do
      [ -n "$file" ] && rm -f "$root$file"
    done < "$root$MANIFEST"
  fi
  rm -f "$root$EXTDIR/nautilus-7zip.py" "$root$BINDIR/nautilus-7zip" \
        "$root$DATADIR/applications/$APP_ID.desktop" \
        "$root$DATADIR/dbus-1/services/$APP_ID.service" \
        "$root$DATADIR/icons/hicolor/scalable/apps/$APP_ID.svg" \
        "$root$DATADIR/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg"
  find "$root$DATADIR/locale" -name 'nautilus-7zip.mo' -delete 2>/dev/null || true
  rm -rf "$root$PKGDATADIR"
  refresh_caches
  say "${GREEN}✓${RESET} nautilus-7zip was removed. Your settings are kept in ~/.config/nautilus-7zip."
  say "  Restart Files to drop the menu: ${BOLD}nautilus -q${RESET}"
}

refresh_caches() {
  [ -n "$DESTDIR" ] && return 0
  if command -v update-desktop-database >/dev/null 2>&1 && [ -d "$DATADIR/applications" ]; then
    update-desktop-database -q "$DATADIR/applications" 2>/dev/null || true
  fi
  # Only refresh an icon cache that already exists: creating one in a user's
  # icon folder would hide icons that other apps add later without updating it.
  if [ -f "$DATADIR/icons/hicolor/icon-theme.cache" ]; then
    for tool in gtk4-update-icon-cache gtk-update-icon-cache; do
      if command -v "$tool" >/dev/null 2>&1; then
        "$tool" -q -t -f "$DATADIR/icons/hicolor" 2>/dev/null || true
        break
      fi
    done
  fi
}

# ---------------------------------------------------------------------------
# Bootstrap: when run via "curl … | bash" fetch the sources first
# ---------------------------------------------------------------------------
if [ "$ACTION" = "install" ] && { [ -z "$SOURCE_DIR" ] || [ ! -f "$SOURCE_DIR/extension/nautilus-7zip.py" ]; }; then
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  URL="https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
  step "Downloading nautilus-7zip ($BRANCH)"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" | tar -xz -C "$TMP" --strip-components=1
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$URL" | tar -xz -C "$TMP" --strip-components=1
  else
    die "Please install curl or wget."
  fi
  ARGS=(--"$MODE")
  [ -n "$PREFIX" ] && ARGS+=(--prefix "$PREFIX")
  [ -n "$DESTDIR" ] && ARGS+=(--destdir "$DESTDIR")
  [ -n "$PYTHON" ] && ARGS+=(--python "$PYTHON")
  [ "$RESTART" = 1 ] && ARGS+=(--restart)
  [ "$QUIET" = 1 ] && ARGS+=(--quiet)
  bash "$TMP/install.sh" "${ARGS[@]}"
  exit $?
fi

if [ "$ACTION" = "uninstall" ]; then
  uninstall
  exit 0
fi

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
distro_hint() {
  # $1 = what is missing: nautilus-python | gtk | 7zip
  local id like
  id="$(. /etc/os-release 2>/dev/null && echo "${ID:-}")"
  like="$(. /etc/os-release 2>/dev/null && echo "${ID_LIKE:-}")"
  case " $id $like " in
    *" arch "*|*" cachyos "*|*" manjaro "*|*" endeavouros "*)
      case "$1" in nautilus-python) echo "sudo pacman -S --needed nautilus-python" ;;
                   gtk) echo "sudo pacman -S --needed python-gobject gtk4 libadwaita" ;;
                   7zip) echo "sudo pacman -S --needed 7zip" ;; esac ;;
    *" debian "*|*" ubuntu "*)
      case "$1" in nautilus-python) echo "sudo apt install python3-nautilus" ;;
                   gtk) echo "sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1" ;;
                   7zip) echo "sudo apt install 7zip" ;; esac ;;
    *" fedora "*|*" rhel "*)
      case "$1" in nautilus-python) echo "sudo dnf install nautilus-python" ;;
                   gtk) echo "sudo dnf install python3-gobject gtk4 libadwaita" ;;
                   7zip) echo "sudo dnf install 7zip" ;; esac ;;
    *" suse "*|*" opensuse "*)
      case "$1" in nautilus-python) echo "sudo zypper install python3-nautilus" ;;
                   gtk) echo "sudo zypper install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1" ;;
                   7zip) echo "sudo zypper install 7zip" ;; esac ;;
  esac
}

have_nautilus_python() {
  local lib
  for lib in /usr/lib*/nautilus/extensions-4/libnautilus-python.so \
             /usr/lib/*/nautilus/extensions-4/libnautilus-python.so \
             /usr/local/lib*/nautilus/extensions-4/libnautilus-python.so; do
    [ -e "$lib" ] && return 0
  done
  return 1
}

if [ -z "$PYTHON" ]; then
  if [ -x /usr/bin/python3 ]; then PYTHON=/usr/bin/python3; else PYTHON="$(command -v python3 || true)"; fi
fi
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || die "Python 3 was not found."
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10 or newer is required ($("$PYTHON" --version 2>&1))."

if [ -z "$DESTDIR" ]; then
  if ! "$PYTHON" - <<'PY' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw
raise SystemExit(0 if (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 5) else 1)
PY
  then
    hint="$(distro_hint gtk)"
    die "GTK 4 and libadwaita (1.5+) for Python are required.${hint:+ Install them with: $hint}"
  fi
  if ! have_nautilus_python; then
    hint="$(distro_hint nautilus-python)"
    warn "nautilus-python is not installed, so Files cannot load the menu yet."
    [ -n "$hint" ] && warn "Install it with: ${BOLD}$hint${RESET}"
  fi
fi

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
ROOT="$DESTDIR"
INSTALLED=()
put() { # put SRC DEST [MODE]
  install -Dm"${3:-644}" "$1" "$ROOT$2"
  INSTALLED+=("$2")
}
render() { # render TEMPLATE DEST [MODE] — substitute @VARS@
  local tmp
  tmp="$(mktemp)"
  sed -e "s|@PKGDATADIR@|$PKGDATADIR|g" -e "s|@HELPER@|$HELPER|g" -e "s|@PYTHON@|$PYTHON|g" \
      -e "s|@BINDIR@|$BINDIR|g" "$1" > "$tmp"
  put "$tmp" "$2" "${3:-644}"
  rm -f "$tmp"
}

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SOURCE_DIR/src/nautilus7zip/__init__.py")"
step "Installing nautilus-7zip $VERSION into $DATADIR"

# Remove the previous version's files first (keeps upgrades clean).
rm -rf "$ROOT$PKGDATADIR/nautilus7zip"

while IFS= read -r -d '' file; do
  rel="${file#"$SOURCE_DIR/src/"}"
  put "$file" "$PKGDATADIR/$rel"
done < <(find "$SOURCE_DIR/src/nautilus7zip" -name '*.py' -print0)

render "$SOURCE_DIR/data/nautilus-7zip.in" "$HELPER" 755
render "$SOURCE_DIR/extension/nautilus-7zip.py" "$EXTDIR/nautilus-7zip.py"
render "$SOURCE_DIR/data/$APP_ID.desktop.in" "$DATADIR/applications/$APP_ID.desktop"
render "$SOURCE_DIR/data/$APP_ID.service.in" "$DATADIR/dbus-1/services/$APP_ID.service"
put "$SOURCE_DIR/data/icons/hicolor/scalable/apps/$APP_ID.svg" "$DATADIR/icons/hicolor/scalable/apps/$APP_ID.svg"
put "$SOURCE_DIR/data/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg" \
    "$DATADIR/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg"
if [ -z "$DESTDIR" ]; then  # distribution packages are removed by the package manager
  put "$SOURCE_DIR/install.sh" "$PKGDATADIR/uninstall.sh" 755
fi

# Command-line launcher on the PATH.
mkdir -p "$ROOT$BINDIR"
ln -sfn "$HELPER" "$ROOT$BINDIR/nautilus-7zip"
INSTALLED+=("$BINDIR/nautilus-7zip")

# Translations
if command -v msgfmt >/dev/null 2>&1 && compgen -G "$SOURCE_DIR/po/*.po" >/dev/null; then
  for po in "$SOURCE_DIR"/po/*.po; do
    lang="$(basename "$po" .po)"
    mo="$(mktemp)"
    msgfmt -o "$mo" "$po"
    put "$mo" "$DATADIR/locale/$lang/LC_MESSAGES/nautilus-7zip.mo"
    rm -f "$mo"
  done
fi

if [ -z "$DESTDIR" ]; then
  printf '%s\n' "${INSTALLED[@]}" | install -Dm644 /dev/stdin "$ROOT$MANIFEST"
fi
find "$ROOT$PKGDATADIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
refresh_caches

say "${GREEN}✓${RESET} Installed. Right-click any file in Files and open the ${BOLD}7-Zip${RESET} submenu."

if [ -z "$DESTDIR" ]; then
  if ! command -v 7z >/dev/null 2>&1 && ! command -v 7zz >/dev/null 2>&1 && ! command -v 7za >/dev/null 2>&1; then
    hint="$(distro_hint 7zip)"
    say "  ${DIM}ZIP and TAR archives work right away. For 7z, RAR, ISO and 40+ more formats install 7-Zip${hint:+: $hint}${RESET}"
  fi
  if [ "$RESTART" = 1 ]; then
    step "Restarting Files"
    nautilus -q >/dev/null 2>&1 || true
  elif pgrep -x nautilus >/dev/null 2>&1; then
    say "  Restart Files once so it loads the menu: ${BOLD}nautilus -q${RESET}"
  fi
  if [ "$MODE" = "user" ]; then
    say "  ${DIM}Uninstall any time with: ~/.local/share/nautilus-7zip/uninstall.sh${RESET}"
  else
    say "  ${DIM}Uninstall any time with: sudo $PKGDATADIR/uninstall.sh --system${RESET}"
  fi
fi
