#!/bin/sh
set -eu

LABEL="com.quantdesk.local"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")
ENGINE_DIR="$PROJECT_DIR/engine"
WEB_DIR="$PROJECT_DIR/web"
PYTHON="$ENGINE_DIR/.venv/bin/python"
TA_PYTHON="$HOME/.openclaw/workspace/integrations/TradingAgents/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/.quantdesk/logs"
DOMAIN="gui/$(id -u)"

xml_escape() {
  printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g; s/"/\&quot;/g'
}

status_service() {
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "QuantDesk service: running"
    echo "Web: http://127.0.0.1:4173/"
    return 0
  fi
  echo "QuantDesk service: stopped"
  return 1
}

case "${1:-status}" in
  install)
    [ "$(uname -s)" = "Darwin" ] || { echo "This installer supports macOS only." >&2; exit 1; }
    [ -x "$PYTHON" ] || { echo "Missing engine environment: $PYTHON" >&2; exit 1; }
    mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"
    (cd "$WEB_DIR" && npm run build)
    PROJECT_XML=$(xml_escape "$PROJECT_DIR")
    ENGINE_XML=$(xml_escape "$ENGINE_DIR")
    PYTHON_XML=$(xml_escape "$PYTHON")
    TA_XML=$(xml_escape "$TA_PYTHON")
    LOG_XML=$(xml_escape "$LOG_DIR")
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PYTHON_XML</string><string>-m</string><string>quantdesk.cli</string>
    <string>serve</string><string>--port</string><string>4173</string>
  </array>
  <key>WorkingDirectory</key><string>$ENGINE_XML</string>
  <key>EnvironmentVariables</key><dict>
    <key>QUANTDESK_WEB_DIST</key><string>$PROJECT_XML/web/dist</string>
    <key>TRADINGAGENTS_PYTHON</key><string>$TA_XML</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG_XML/service.log</string>
  <key>StandardErrorPath</key><string>$LOG_XML/service-error.log</string>
</dict></plist>
EOF
    chmod 600 "$PLIST"
    plutil -lint "$PLIST" >/dev/null
    launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    launchctl kickstart -k "$DOMAIN/$LABEL"
    status_service
    ;;
  uninstall)
    launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
    rm -f "$PLIST"
    echo "QuantDesk service removed; application data was preserved."
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL"
    status_service
    ;;
  logs)
    tail -n 100 "$LOG_DIR/service-error.log" "$LOG_DIR/service.log"
    ;;
  status)
    status_service
    ;;
  *)
    echo "Usage: $0 {install|uninstall|restart|status|logs}" >&2
    exit 2
    ;;
esac
