#!/usr/bin/env bash
# Installs the watcher as a macOS launch agent: starts at login, restarts if it
# dies. Run ./install_service.sh again after editing .env to pick up changes.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.unstable.premiumbot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/unstable-premiumbot.log"

if [ ! -f "$DIR/.env" ]; then
  echo "No .env found. Copy .env.example to .env and fill it in first." >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/run.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Installed and started: $LABEL"
echo "Logs:  tail -f $LOG"
echo "Stop:  launchctl unload $PLIST"
