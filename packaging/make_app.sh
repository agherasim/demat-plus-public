#!/usr/bin/env bash
# Generates the DEMAT+.app launcher bundle from launcher.sh.tmpl (ADR 0009,
# ADR 0017). Called by `demat install-app`; DEMAT_BIN is the stable path of
# the `demat` command the bundle hands over to. Safe to re-run: removes an
# existing bundle first. Does not install dependencies or touch
# $DEMAT_HOME/data.
#
# Usage: make_app.sh <path-to-.app>
set -euo pipefail

APP_PATH="${1:?usage: make_app.sh <path-to-.app>}"
DEMAT_HOME="${DEMAT_HOME:-$HOME/.demat}"
DEMAT_BIN="${DEMAT_BIN:?DEMAT_BIN must name the demat command}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$SCRIPT_DIR/launcher.sh.tmpl"

log() { printf '==> %s\n' "$1"; }

VERSION="$(grep -m1 '^version' "$PROJECT_ROOT/pyproject.toml" | sed -E 's/version *= *"([^"]+)".*/\1/')"
if [[ -z "$VERSION" ]]; then
  VERSION="0.0.0"
fi

log "Generating app bundle at $APP_PATH (version $VERSION)"
rm -rf "$APP_PATH"
mkdir -p "$APP_PATH/Contents/MacOS" "$APP_PATH/Contents/Resources"

cat >"$APP_PATH/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>DEMAT+</string>
    <key>CFBundleExecutable</key>
    <string>demat-launcher</string>
    <key>CFBundleIdentifier</key>
    <string>de.demat.app</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>$VERSION</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

sed -e "s|@@HOME@@|$DEMAT_HOME|g" -e "s|@@DEMAT_BIN@@|$DEMAT_BIN|g" "$TEMPLATE" >"$APP_PATH/Contents/MacOS/demat-launcher"
chmod 755 "$APP_PATH/Contents/MacOS/demat-launcher"

ICON_SRC="$SCRIPT_DIR/icon.icns"
if [[ -f "$ICON_SRC" ]]; then
  cp "$ICON_SRC" "$APP_PATH/Contents/Resources/icon.icns"
  log "Bundled icon.icns"
else
  log "No icon asset at $ICON_SRC; bundle uses the generic app icon"
fi

# Force LaunchServices to re-read the bundle (mtime bump).
touch "$APP_PATH"
log "App bundle ready: $APP_PATH"
