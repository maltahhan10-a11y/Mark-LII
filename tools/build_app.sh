#!/bin/bash
# Build Jarvis.app — a launcher bundle, not a frozen binary.
#
# py2app and PyInstaller both choke on this dependency set: mediapipe ships
# model assets and native libraries that a freezer relocates incorrectly, and
# PyQt6 plugins need their own coaxing. Since this app only ever runs on this
# machine, freezing buys nothing and costs a great deal, so the bundle simply
# points at the repo and its virtualenv.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${1:-$HOME/Applications/Jarvis.app}"
NAME="Jarvis"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleIdentifier</key><string>com.markii.jarvis</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>$NAME</string>
  <key>CFBundleIconFile</key><string>jarvis</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <!-- macOS refuses the camera and microphone outright unless the bundle
       says why it wants them. Without these keys the app is killed on first
       access rather than being denied politely. -->
  <key>NSCameraUsageDescription</key>
  <string>Jarvis watches for hand gestures and can look through the camera when you ask it to.</string>
  <key>NSMicrophoneUsageDescription</key>
  <string>Jarvis listens for your voice so you can talk to it.</string>
  <key>NSAppleEventsUsageDescription</key>
  <string>Jarvis controls other apps on your behalf when you ask it to.</string>
  <key>NSSpeechRecognitionUsageDescription</key>
  <string>Jarvis turns what you say into text so it can act on it.</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/$NAME" <<LAUNCH
#!/bin/bash
# Launcher. Keep this thin — anything clever here is invisible when it fails,
# because a double-clicked app has nowhere to print to.
REPO="$REPO"
LOG="\$HOME/Library/Logs/Jarvis.log"
export PYTHONUNBUFFERED=1
mkdir -p "\$(dirname "\$LOG")"

cd "\$REPO" || {
  osascript -e 'display alert "Jarvis" message "The Jarvis folder has moved. Rebuild the app with tools/build_app.sh."'
  exit 1
}

PY="\$REPO/venv/bin/python"
[ -x "\$PY" ] || PY="\$(command -v python3)"
[ -x "\$PY" ] || {
  osascript -e 'display alert "Jarvis" message "No Python found. Create the virtualenv first."'
  exit 1
}

# One instance only. Two copies fight over the microphone, the camera and
# port 8000, and the second one loses in confusing ways.
if pgrep -f "\$REPO/main.py" >/dev/null 2>&1; then
  osascript -e 'display notification "Jarvis is already running." with title "Jarvis"'
  exit 0
fi

# -u, not buffered. Writing to a file rather than a terminal makes Python
# block-buffer stdout, so a crash loses everything still in the buffer -- the
# log arrives empty exactly when it is needed. The app is not chatty enough
# for the flushing to cost anything.
{
  echo "=== Jarvis started \$(date) ==="
  exec "\$PY" -u "\$REPO/main.py"
} >>"\$LOG" 2>&1
LAUNCH

chmod +x "$APP/Contents/MacOS/$NAME"
[ -f "$REPO/tools/jarvis.icns" ] && cp "$REPO/tools/jarvis.icns" "$APP/Contents/Resources/jarvis.icns"

# Ask Launch Services to notice the new bundle so it appears in Spotlight.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" 2>/dev/null || true

echo "Built $APP"
