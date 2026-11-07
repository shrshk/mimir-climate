#!/usr/bin/env bash
# Install (or update) mimir-climate on the Mac that runs OpenClaw, and start it at login.
#   bash scripts/install-mac.sh            install or update
#   bash scripts/install-mac.sh --remove   stop and remove (settings and token are kept)
# Needs uv (brew install uv) and the settings file first: copy settings.example.json to
# ~/.config/mimir-climate/settings.json and fill it in. It is never in git.
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
REPO=$(cd "$(dirname "$0")/.." && pwd)
LABEL=dev.mimir-climate
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
GUI="gui/$(id -u)"
CONFIG="${MIMIR_CLIMATE_CONFIG:-$HOME/.config/mimir-climate}"

launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--remove" ]; then
  rm -f "$PLIST"
  uv tool uninstall mimir-climate 2>/dev/null || true
  echo "removed"
  exit 0
fi
[ -f "$CONFIG/settings.json" ] || { echo "first copy settings.example.json to $CONFIG/settings.json and fill it in" >&2; exit 1; }

uv tool install --force --quiet "$REPO/service"
# The headless browser that drives the portal, matching the installed Playwright.
"$(uv tool dir)/mimir-climate/bin/python" -m playwright install chromium-headless-shell
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key><array><string>$HOME/.local/bin/mimir-climate</string></array>
    <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$HOME/Library/Logs/mimir-climate.log</string>
    <key>StandardErrorPath</key><string>$HOME/Library/Logs/mimir-climate.log</string>
</dict>
</plist>
EOF
launchctl bootstrap "$GUI" "$PLIST"
launchctl kickstart -k "$GUI/$LABEL"
echo "installed and started (log ~/Library/Logs/mimir-climate.log); MCP at http://127.0.0.1:8725/mcp, token in $CONFIG/mcp-token"
