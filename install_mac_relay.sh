#!/bin/bash
# install_mac_relay.sh - install the ClipBridge Mac relay as a launchd agent on this Mac
# (starts at login, restarted automatically if it ever dies).
#
#   bash install_mac_relay.sh --url http://100.81.162.90:48765 --token KEY   [--no-push] [--pull-remote] [--python /path/to/python3]
#   bash install_mac_relay.sh --url "http://100.81.162.90:48765/?t=KEY"       (the pairing link works too)
#   bash install_mac_relay.sh --status       is it running? + last log lines
#   bash install_mac_relay.sh --uninstall    stop it and remove the agent
#
# Files it creates:
#   ~/Library/Application Support/ClipBridge/mac_relay.py
#   ~/Library/LaunchAgents/com.clipbridge.macrelay.plist   (holds the pairing key, mode 600)
#   ~/Library/Logs/clipbridge-relay.log
set -eu

LABEL="com.clipbridge.macrelay"
APP_DIR="$HOME/Library/Application Support/ClipBridge"
AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENTS_DIR/$LABEL.plist"
LOG="$HOME/Library/Logs/clipbridge-relay.log"
HERE="$(cd "$(dirname "$0")" && pwd)"
DOMAIN="gui/$(id -u)"

URL=""
TOKEN=""
PY=""
EXTRA_FLAGS=""
MODE="install"

die() { echo "error: $*" >&2; exit 1; }
usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --url) URL="${2:-}"; shift 2 ;;
        --url=*) URL="${1#--url=}"; shift ;;
        --token) TOKEN="${2:-}"; shift 2 ;;
        --token=*) TOKEN="${1#--token=}"; shift ;;
        --python) PY="${2:-}"; shift 2 ;;
        --python=*) PY="${1#--python=}"; shift ;;
        --no-push|--pull-remote) EXTRA_FLAGS="$EXTRA_FLAGS $1"; shift ;;
        --uninstall) MODE="uninstall"; shift ;;
        --status) MODE="status"; shift ;;
        -h|--help) usage; exit 0 ;;
        http://*|https://*) URL="$1"; shift ;;
        *)
            if [ -n "$URL" ] && [ -z "$TOKEN" ]; then TOKEN="$1"; shift
            else die "unknown argument: $1 (see --help)"; fi ;;
    esac
done

[ "$(uname -s)" = "Darwin" ] || die "this installer is for macOS (run it on the Mac)"

stop_agent() {
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 \
        || launchctl unload "$PLIST" >/dev/null 2>&1 \
        || true
}

case "$MODE" in
    uninstall)
        stop_agent
        rm -f "$PLIST" "$APP_DIR/mac_relay.py"
        echo "ClipBridge Mac relay stopped and removed. (Log kept: $LOG)"
        exit 0 ;;
    status)
        if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
            echo "agent $LABEL is loaded:"
            launchctl print "$DOMAIN/$LABEL" | grep -E '^[[:space:]]*(state|pid|last exit code|program) ' || true
        else
            echo "agent $LABEL is not loaded (install with --url ... --token ...)"
        fi
        if [ -f "$LOG" ]; then echo "--- last lines of $LOG"; tail -n 15 "$LOG"; fi
        exit 0 ;;
esac

# ---- arguments ---------------------------------------------------------------------------------
case "$URL" in
    *\?t=*)                                   # pairing link given: split off the key
        [ -n "$TOKEN" ] || { TOKEN="${URL##*\?t=}"; TOKEN="${TOKEN%%&*}"; }
        URL="${URL%%\?*}" ;;
esac
URL="${URL%/}"
if [ -z "$URL" ] || [ -z "$TOKEN" ]; then
    usage
    die "--url and --token are required (the key is the t=... part of the pairing link shown at http://localhost:48765/ on the PC)"
fi
case "$URL$TOKEN" in *[\&\<\>\"\']*) die "the URL or key contains a character that cannot be written to the plist" ;; esac
case "$URL" in http://*|https://*) ;; *) URL="http://$URL" ;; esac

# ---- which python3? prefer one that has PyObjC (AppKit) ---------------------------------------
runs()       { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; }
has_appkit() { "$1" -c 'import AppKit, Foundation' >/dev/null 2>&1; }
pick_python() {
    local c candidates="/usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3"
    if command -v python3 >/dev/null 2>&1; then candidates="$candidates $(command -v python3)"; fi
    # /usr/bin/python3 is only a stub until the Command Line Tools are installed (it pops up an installer)
    if ! xcode-select -p >/dev/null 2>&1; then candidates="${candidates#/usr/bin/python3}"; fi
    for c in $candidates; do [ -x "$c" ] && runs "$c" && has_appkit "$c" && { echo "$c"; return 0; }; done
    for c in $candidates; do [ -x "$c" ] && runs "$c" && { echo "$c"; return 0; }; done
    return 1
}
if [ -z "$PY" ]; then
    PY="$(pick_python)" || die "no python3 found. Run  xcode-select --install  (Apple's python3 includes PyObjC) or install Python 3, then rerun."
fi
[ -x "$PY" ] || die "not an executable python: $PY"
if has_appkit "$PY"; then
    BACKEND="AppKit via PyObjC"
else
    BACKEND="osascript fallback (works, polls once a second; for the fast path: $PY -m pip install --user pyobjc-framework-Cocoa)"
fi

# ---- files -------------------------------------------------------------------------------------
mkdir -p "$APP_DIR" "$AGENTS_DIR" "$(dirname "$LOG")"
if [ -f "$HERE/mac_relay.py" ] && [ "$HERE/mac_relay.py" -ef "$APP_DIR/mac_relay.py" ]; then
    :   # running from the install folder itself: nothing to copy
elif [ -f "$HERE/mac_relay.py" ]; then
    tr -d '\r' < "$HERE/mac_relay.py" > "$APP_DIR/mac_relay.py"
else
    echo "downloading mac_relay.py from $URL ..."
    tmp="$(mktemp)"
    curl -fsS -H "X-Token: $TOKEN" -o "$tmp" "$URL/mac_relay.py" \
        || die "could not download $URL/mac_relay.py - is ClipBridge running on the PC and Tailscale connected on both machines?"
    tr -d '\r' < "$tmp" > "$APP_DIR/mac_relay.py"; rm -f "$tmp"
fi
chmod 644 "$APP_DIR/mac_relay.py"

TEMPLATE=""
if [ -f "$HERE/$LABEL.plist" ]; then
    TEMPLATE="$(tr -d '\r' < "$HERE/$LABEL.plist")"
else
    TEMPLATE="$(curl -fsS -H "X-Token: $TOKEN" "$URL/$LABEL.plist" 2>/dev/null | tr -d '\r' || true)"
fi
if [ -z "$TEMPLATE" ]; then
    TEMPLATE="$(cat <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.clipbridge.macrelay</string>
    <key>ProgramArguments</key>
    <array>
        <string>__PYTHON__</string>
        <string>__SCRIPT__</string>
__EXTRA_ARGS__    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CLIPBRIDGE_URL</key><string>__URL__</string>
        <key>CLIPBRIDGE_TOKEN</key><string>__TOKEN__</string>
        <key>PYTHONUNBUFFERED</key><string>1</string>
        <key>LANG</key><string>en_US.UTF-8</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>LimitLoadToSessionType</key><string>Aqua</string>
    <key>ProcessType</key><string>Interactive</string>
    <key>StandardOutPath</key><string>__LOG__</string>
    <key>StandardErrorPath</key><string>__LOG__</string>
</dict>
</plist>
PLIST
)"
fi

# ---- does it work? ------------------------------------------------------------------------------
echo "checking the connection to the PC and the pasteboard with $PY ..."
set +e
"$PY" "$APP_DIR/mac_relay.py" --url "$URL" --token "$TOKEN" --check
rc=$?
set -e
case $rc in
    0) ;;
    3) die "the PC rejected the key. Copy the current pairing link from http://localhost:48765/ on the PC and rerun." ;;
    4) echo "warning: the PC is not reachable right now; installing anyway (the relay keeps retrying until it is)." ;;
    *) die "mac_relay.py --check failed (exit $rc), see the message above" ;;
esac

# ---- write the agent and load it ------------------------------------------------------------------
EXTRA_XML=""
for f in $EXTRA_FLAGS; do
    EXTRA_XML="$EXTRA_XML        <string>$f</string>
"
done
content="$TEMPLATE"
content="${content//__PYTHON__/$PY}"
content="${content//__SCRIPT__/$APP_DIR/mac_relay.py}"
content="${content//__URL__/$URL}"
content="${content//__TOKEN__/$TOKEN}"
content="${content//__LOG__/$LOG}"
content="${content//__EXTRA_ARGS__/$EXTRA_XML}"

stop_agent
( umask 077; printf '%s\n' "$content" > "$PLIST" )
chmod 600 "$PLIST"
plutil -lint "$PLIST" >/dev/null || die "generated plist is invalid: $PLIST"

launchctl enable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
err="$(mktemp)"
if ! launchctl bootstrap "$DOMAIN" "$PLIST" 2>"$err"; then
    launchctl load -w "$PLIST" 2>>"$err" || { cat "$err" >&2; rm -f "$err"; die "launchctl could not load the agent"; }
fi
rm -f "$err"
sleep 3

echo
echo "ClipBridge Mac relay installed and running."
echo "  PC        : $URL"
echo "  python    : $PY  [$BACKEND]"
echo "  options   :${EXTRA_FLAGS:- (both directions)}"
echo "  agent     : $PLIST"
echo "  log       : $LOG"
if [ -f "$LOG" ]; then echo "--- last log lines"; tail -n 6 "$LOG"; fi
echo
echo "Copy something on the PC: it is now in this Mac's clipboard and, through Universal Clipboard,"
echo "in the paste menu of the iPhone next to it. Copy on the Mac -> it lands in the PC clipboard."
echo
echo "Stop / remove :  bash install_mac_relay.sh --uninstall      (or: launchctl bootout $DOMAIN/$LABEL)"
echo "Status        :  bash install_mac_relay.sh --status         Live log:  tail -f \"$LOG\""
