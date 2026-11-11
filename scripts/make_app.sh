#!/bin/bash
# Build dist/disko.app, a double-clickable launcher for disko in this checkout.
#
#   scripts/make_app.sh             build dist/disko.app
#   scripts/make_app.sh --install   build it, then copy it to ~/Applications
#
# The app is not self-contained: it runs this checkout's .venv-live, and this checkout's path is
# baked into it when it is built. Move or rename the checkout and the app stops working; build it
# again (bin/disko app --install) to point it at the new place.
#
# Works on macOS's stock bash 3.2.
set -eu

VERSION=0.1.0                    # the app's version: CFBundleShortVersionString and CFBundleVersion
BUNDLE_ID=com.emirkaan5.disko
MIN_MACOS=13.0                   # PySide6 6.11's wheels need macOS 13

REPO=$(cd -P "$(dirname "$0")/.." && pwd)
INSTALL=0
for arg in "$@"; do
    case $arg in
        --install) INSTALL=1 ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "make_app.sh: unknown option $arg (try --help)" >&2; exit 2 ;;
    esac
done

LIVE_PY="$REPO/.venv-live/bin/python"
if [ ! -x "$LIVE_PY" ]; then
    echo "make_app.sh: .venv-live is missing; run bin/disko setup first." >&2
    exit 1
fi
for tool in iconutil plutil codesign; do
    command -v "$tool" >/dev/null || { echo "make_app.sh: $tool not found (it ships with macOS)" >&2; exit 1; }
done

APP="$REPO/dist/disko.app"
C="$APP/Contents"
echo "Building $APP"
echo "  for the checkout at $REPO (baked in: move the checkout, then rebuild the app)"
rm -rf "$APP"
mkdir -p "$C/MacOS" "$C/Resources"

# --- icon -------------------------------------------------------------------------------------
work=$(mktemp -d "${TMPDIR:-/tmp}/disko-app.XXXXXX")
trap 'rm -rf "$work"' EXIT
QT_QPA_PLATFORM=offscreen "$LIVE_PY" "$REPO/scripts/make_icon.py" "$work/disko.iconset" >/dev/null
iconutil -c icns -o "$C/Resources/disko.icns" "$work/disko.iconset"

# --- where the checkout is ------------------------------------------------------------------
printf '%s\n' "$REPO" > "$C/Resources/checkout"

# --- the interpreter, as the bundle's own executable -----------------------------------------
# A framework build of Python (Homebrew's, python.org's) runs as Python.app, so a window opened
# by `.venv-live/bin/python` shows up in the Dock as "Python" with Python's rocket icon. Running
# the framework's interpreter through a link inside this bundle makes macOS take disko.app as the
# main bundle (its name, icon and Info.plist), and __PYVENV_LAUNCHER__ (set by the launcher, as
# Python's own bin/ stub does) keeps .venv-live active. Nothing under live/ needs to change.
framework_python=$("$LIVE_PY" -I -c 'import os, sys, sysconfig
if sysconfig.get_config_var("PYTHONFRAMEWORK"):
    p = os.path.join(sys.base_prefix, "Resources", "Python.app", "Contents", "MacOS", "Python")
    if os.access(p, os.X_OK):
        print(p)')
if [ -n "$framework_python" ]; then
    ln -s "$framework_python" "$C/MacOS/disko-python"
    echo "  Dock identity: runs $framework_python as disko.app"
else
    echo "  Dock identity: .venv-live's Python isn't a framework build; running it directly"
fi

# --- the launcher -----------------------------------------------------------------------------
cat > "$C/MacOS/disko" <<'LAUNCHER'
#!/bin/bash
# disko.app's launcher: runs `python -m live` from the checkout named in Resources/checkout.
# DISKO_APP_CHECK=1 runs the checks and reports instead of starting disko.
set -eu
here=$(cd -P "$(dirname "$0")" && pwd)
contents=$(dirname "$here")
REPO=$(cat "$contents/Resources/checkout" 2>/dev/null || true)
LOG="$HOME/Library/Logs/disko.log"
CHECK=${DISKO_APP_CHECK:-0}
# Apps opened from Finder get a minimal PATH; disko looks up uv (to start Kev) and ffmpeg on it.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

fail() {
    if [ "$CHECK" = 1 ]; then
        echo "✗ $1" >&2
    else
        mkdir -p "$(dirname "$LOG")" && printf '%s disko.app: %s\n' "$(date '+%F %T')" "$1" >>"$LOG"
        msg=$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')
        osascript -e "display dialog \"$msg\" with title \"disko\" buttons {\"OK\"} default button \"OK\" with icon caution" \
            >/dev/null 2>&1 || true
    fi
    exit 1
}

[ -n "$REPO" ] || fail "This copy of disko.app is damaged (no checkout recorded). Rebuild it: bin/disko app --install"
[ -f "$REPO/live/__main__.py" ] || fail "disko.app runs disko from $REPO, but that checkout is gone. If you moved it, rebuild the app from the new place: bin/disko app --install"
LIVE_PY="$REPO/.venv-live/bin/python"
[ -x "$LIVE_PY" ] || fail "disko isn't set up in $REPO yet (.venv-live is missing). In Terminal, run: \"$REPO/bin/disko\" setup"

if [ -x "$here/disko-python" ]; then
    interp="$here/disko-python"
    export __PYVENV_LAUNCHER__="$LIVE_PY"  # Python reads and clears this: .venv-live, not the base install
else
    interp="$LIVE_PY"
fi
cd "$REPO"

if [ "$CHECK" = 1 ]; then
    echo "✓ checkout: $REPO"
    echo "✓ .venv-live: $LIVE_PY"
    echo "  interpreter: $interp"
    echo "  log: $LOG"
    exec "$interp" -I -c 'import ctypes, ctypes.util, os, subprocess, sys
cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
cf.CFBundleGetMainBundle.restype = ctypes.c_void_p
cf.CFBundleGetValueForInfoDictionaryKey.restype = ctypes.c_void_p
cf.CFBundleGetValueForInfoDictionaryKey.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
cf.CFStringCreateWithCString.restype = ctypes.c_void_p
cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
def info(key):
    v = cf.CFBundleGetValueForInfoDictionaryKey(cf.CFBundleGetMainBundle(), cf.CFStringCreateWithCString(None, key.encode(), 0x08000100))
    buf = ctypes.create_string_buffer(1024)
    return buf.value.decode() if v and cf.CFStringGetCString(v, buf, 1024, 0x08000100) else None
print("  main bundle (what the Dock shows):", info("CFBundleName"), info("CFBundleIdentifier"), "icon", info("CFBundleIconFile"))
print("  python:", sys.executable, "prefix", sys.prefix)
print("  __PYVENV_LAUNCHER__ left for child processes:", "__PYVENV_LAUNCHER__" in os.environ)
import PySide6
print("  PySide6", PySide6.__version__, "from", os.path.dirname(PySide6.__file__))
fast = os.path.join(os.getcwd(), ".venv-essentia-tf", "bin", "python")
if os.access(fast, os.X_OK):
    out = subprocess.run([fast, "-c", "import sys; print(sys.prefix)"], capture_output=True, text=True).stdout.strip()
    print("  a child .venv-essentia-tf python sees prefix", out)
uv = __import__("shutil").which("uv")
print("  uv on PATH:", uv)'
fi

# Finder on old macOS versions passes a -psn_ process serial number; drop it.
for a in "$@"; do
    shift
    case $a in -psn_*) ;; *) set -- "$@" "$a" ;; esac
done

mkdir -p "$(dirname "$LOG")"
printf '\n--- %s disko.app: %s -m live %s\n' "$(date '+%F %T')" "$interp" "$*" >>"$LOG"
exec "$interp" -m live "$@" >>"$LOG" 2>&1
LAUNCHER
chmod 755 "$C/MacOS/disko"

# --- Info.plist -------------------------------------------------------------------------------
# No NSMicrophoneUsageDescription: disko only opens an output stream (live/engine.py).
cat > "$C/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleName</key>
    <string>disko</string>
    <key>CFBundleDisplayName</key>
    <string>disko</string>
    <key>CFBundleIdentifier</key>
    <string>$BUNDLE_ID</string>
    <key>CFBundleShortVersionString</key>
    <string>$VERSION</string>
    <key>CFBundleVersion</key>
    <string>$VERSION</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleExecutable</key>
    <string>disko</string>
    <key>CFBundleIconFile</key>
    <string>disko</string>
    <key>LSMinimumSystemVersion</key>
    <string>$MIN_MACOS</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.music</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST
printf 'APPL????' > "$C/PkgInfo"

# --- sign and verify --------------------------------------------------------------------------
# Ad hoc, without --deep: the only nested item is the link to Python, which belongs to its own
# install and must not be re-signed. Strict verification rejects a link that leaves the bundle,
# so the check below is the plain one.
if codesign --force -s - "$APP" 2>"$work/codesign.err" && codesign --verify "$APP" 2>>"$work/codesign.err"; then
    echo "  signed (ad hoc)"
else
    echo "  not signed: $(tr '\n' ' ' < "$work/codesign.err")"
fi

plutil -lint "$C/Info.plist" >/dev/null
[ -x "$C/MacOS/disko" ]
DISKO_APP_CHECK=1 "$C/MacOS/disko" >/dev/null
echo "Built $APP"

if [ "$INSTALL" = 1 ]; then
    mkdir -p "$HOME/Applications"
    rm -rf "$HOME/Applications/disko.app"
    ditto "$APP" "$HOME/Applications/disko.app"
    echo "Installed $HOME/Applications/disko.app"
    echo "Open it from Finder, Spotlight or Launchpad, or drag it to the Dock. Its log: ~/Library/Logs/disko.log"
else
    echo "Install it with: bin/disko app --install (copies it to ~/Applications)"
fi
