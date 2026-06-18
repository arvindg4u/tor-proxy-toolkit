#!/bin/bash
# ============================================
# tor-proxy-toolkit — Master Start Script
# Starts: Tor + Claude Proxy + mimo2codex + IP Rotator
# ============================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="/var/log/tor-toolkit"
PID_DIR="/tmp/tor-toolkit-pids"

mkdir -p "$LOG_DIR" "$PID_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log() { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err() { echo -e "${RED}[✗]${NC} $1"; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

# ── Load .env ──
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a; source "$SCRIPT_DIR/.env"; set +a
    log "Loaded .env"
else
    warn "No .env found — using defaults. Copy .env.example to .env first!"
fi

# ── Defaults ──
TOR_SOCKS_PORT1="${TOR_SOCKS_PORT1:-9050}"
TOR_SOCKS_PORT2="${TOR_SOCKS_PORT2:-9060}"
TOR_CONTROL_PORT="${TOR_CONTROL_PORT:-9051}"
TOR_CONTROL_PASSWORD="${TOR_…ord:-chintu_tor_2026}"
CLAUDE_PROXY_PORT="${CLAUDE_PROXY_PORT:-4013}"
MIMO_PROXY_PORT="${MIMO_PROXY_PORT:-8788}"
ROTATE_INTERVAL="${ROTATE_INTERVAL:-600}"

# ── Check dependencies ──
check_deps() {
    local missing=0
    for cmd in tor python3 curl; do
        if ! command -v $cmd &>/dev/null; then
            err "Missing: $cmd"
            missing=1
        fi
    done
    python3 -c "import stem" 2>/dev/null || { warn "Installing stem..."; pip3 install --break-system-packages stem 2>/dev/null || pip3 install stem; }
    python3 -c "import requests" 2>/dev/null || { warn "Installing requests..."; pip3 install --break-system-packages requests 2>/dev/null || pip3 install requests; }
    [ $missing -eq 1 ] && exit 1
    log "Dependencies OK"
}

# ── Start Tor ──
start_tor() {
    if [ -f "$PID_DIR/tor.pid" ] && kill -0 "$(cat $PID_DIR/tor.pid)" 2>/dev/null; then
        warn "Tor already running (PID: $(cat $PID_DIR/tor.pid))"
        return
    fi

    info "Starting Tor daemon..."
    tor --defaults-torrc "$SCRIPT_DIR/config/torrc" -f "$SCRIPT_DIR/config/torrc" &
    echo $! > "$PID_DIR/tor.pid"
    log "Tor starting (PID: $!)"

    # Wait for bootstrap
    info "Waiting for bootstrap..."
    for i in $(seq 1 30); do
        if grep -q "Bootstrapped 100%" "$LOG_DIR/tor.log" 2>/dev/null || \
           grep -q "Bootstrapped 100%" /var/log/tor/notices.log 2>/dev/null; then
            log "Tor bootstrapped!"
            break
        fi
        sleep 1
    done

    # Verify ports
    for port in $TOR_SOCKS_PORT1 $TOR_SOCKS_PORT2 $TOR_CONTROL_PORT; do
        if netstat -tlnp 2>/dev/null | grep -q ":$port " || ss -tlnp 2>/dev/null | grep -q ":$port "; then
            log "Port $port listening"
        else
            warn "Port $port not yet ready"
        fi
    done
}

# ── Start Claude Code Proxy ──
start_claude_proxy() {
    if [ -f "$PID_DIR/claude-proxy.pid" ] && kill -0 "$(cat $PID_DIR/claude-proxy.pid)" 2>/dev/null; then
        warn "Claude proxy already running (PID: $(cat $PID_DIR/claude-proxy.pid))"
        return
    fi

    info "Starting Claude Code Proxy on port $CLAUDE_PROXY_PORT..."
    cd "$SCRIPT_DIR/claude-code-proxy"

    export OPENAI_API_KEY="${OPENAI_API_KEY:-sk-dummy}"
    export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://opencode.ai/zen/v1}"
    export BIG_MODEL="${BIG_MODEL:-deepseek-v4-flash-free}"
    export MIDDLE_MODEL="${MIDDLE_MODEL:-${BIG_MODEL}}"
    export SMALL_MODEL="${SMALL_MODEL:-deepseek-v4-flash-free}"
    export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
    export PORT="$CLAUDE_PROXY_PORT"
    export LOG_LEVEL="WARNING"
    export MAX_TOKENS_LIMIT="16384"

    python3 start_proxy.py > "$LOG_DIR/claude-proxy.log" 2>&1 &
    echo $! > "$PID_DIR/claude-proxy.pid"
    log "Claude proxy started (PID: $!)"
    cd "$SCRIPT_DIR"
}

# ── Start mimo2codex Proxy (Codex CLI) ──
start_codex_proxy() {
    if [ -f "$PID_DIR/codex-proxy.pid" ] && kill -0 "$(cat $PID_DIR/codex-proxy.pid)" 2>/dev/null; then
        warn "mimo2codex already running (PID: $(cat $PID_DIR/codex-proxy.pid))"
        return
    fi

    info "Starting mimo2codex Proxy on port $MIMO_PROXY_PORT..."

    # Check if mimo2codex is installed
    if ! command -v mimo2codex &>/dev/null; then
        warn "mimo2codex not found. Installing..."
        npm install -g mimo2codex 2>/dev/null || { err "Failed to install mimo2codex"; return; }
    fi

    # Check if .env exists
    if [ ! -f ~/.mimo2codex/.env ]; then
        warn "No ~/.mimo2codex/.env found — copying template"
        mkdir -p ~/.mimo2codex
        cp "$SCRIPT_DIR/mimo2codex/.env.example" ~/.mimo2codex/.env
        warn "⚠️  Edit ~/.mimo2codex/.env with your API key before starting!"
    fi

    mimo2codex --model generic --host 127.0.0.1 --port $MIMO_PROXY_PORT > "$LOG_DIR/codex-proxy.log" 2>&1 &
    echo $! > "$PID_DIR/codex-proxy.pid"
    log "mimo2codex started (PID: $!)"
}

# ── Start IP Rotator ──
start_rotator() {
    if [ -f "$PID_DIR/rotator.pid" ] && kill -0 "$(cat $PID_DIR/rotator.pid)" 2>/dev/null; then
        warn "IP rotator already running (PID: $(cat $PID_DIR/rotator.pid))"
        return
    fi

    info "Starting IP Rotator (every ${ROTATE_INTERVAL}s)..."

    # Create temp script with correct password
    sed "s/TOR_PASSWORD = .*$/TOR_PASSWORD = \"$TOR_CONTROL_PASSWORD\"/" \
        "$SCRIPT_DIR/tor/tor_rotate.py" > /tmp/tor_rotate_active.py

    python3 /tmp/tor_rotate_active.py > "$LOG_DIR/rotator.log" 2>&1 &
    echo $! > "$PID_DIR/rotator.pid"
    log "IP rotator started (PID: $!)"
}

# ── Show status ──
show_status() {
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════${NC}"
    echo -e "${CYAN}  🧅 Tor Proxy Toolkit — Status${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════${NC}"

    for svc in tor claude-proxy codex-proxy rotator; do
        pidfile="$PID_DIR/$svc.pid"
        if [ -f "$pidfile" ] && kill -0 "$(cat $pidfile)" 2>/dev/null; then
            echo -e "  ${GREEN}●${NC} $svc: running (PID: $(cat $pidfile))"
        else
            echo -e "  ${RED}●${NC} $svc: stopped"
        fi
    done

    echo ""
    echo "  Ports:"
    for port in $TOR_SOCKS_PORT1 $TOR_SOCKS_PORT2 $TOR_CONTROL_PORT $CLAUDE_PROXY_PORT $MIMO_PROXY_PORT; do
        if netstat -tlnp 2>/dev/null | grep -q ":$port " || ss -tlnp 2>/dev/null | grep -q ":$port "; then
            echo -e "    ${GREEN}✓${NC} $port — listening"
        else
            echo -e "    ${RED}✗${NC} $port — not listening"
        fi
    done

    echo ""
    echo "  Proxy URLs:"
    echo "    SOCKS5 (port 1): socks5://127.0.0.1:$TOR_SOCKS_PORT1"
    echo "    SOCKS5 (port 2): socks5://127.0.0.1:$TOR_SOCKS_PORT2"
    echo "    Claude Code:     http://127.0.0.1:$CLAUDE_PROXY_PORT"
    echo "    Codex/mimo2codex: http://127.0.0.1:$MIMO_PROXY_PORT"
    echo -e "${CYAN}═══════════════════════════════════════════${NC}"
}

# ── Stop all ──
stop_all() {
    warn "Stopping all services..."
    for svc in rotator codex-proxy claude-proxy tor; do
        pidfile="$PID_DIR/$svc.pid"
        if [ -f "$pidfile" ]; then
            kill "$(cat $pidfile)" 2>/dev/null && log "Stopped $svc" || warn "$svc not running"
            rm -f "$pidfile"
        fi
    done
    pkill -f "tor_rotate.py" 2>/dev/null
    pkill -f "tor_rotate_active.py" 2>/dev/null
    log "All services stopped"
}

# ── Manual rotate ──
manual_rotate() {
    info "Manual IP rotation..."
    python3 -c "
import stem.control, requests, time
controller = stem.control.Controller.from_port(port=$TOR_CONTROL_PORT)
controller.authenticate(password='$TOR_CONTROL_PASSWORD')
for port in [$TOR_SOCKS_PORT1, $TOR_SOCKS_PORT2]:
    r = requests.get('https://check.torproject.org/api/ip', proxies={'https': f'socks5h://127.0.0.1:{port}'}, timeout=15)
    print(f'  Before — Port {port}: {r.json()}')
controller.signal(stem.Signal.NEWNYM)
print('  🔄 NEWNYM sent, waiting 3s...')
time.sleep(3)
for port in [$TOR_SOCKS_PORT1, $TOR_SOCKS_PORT2]:
    r = requests.get('https://check.torproject.org/api/ip', proxies={'https': f'socks5h://127.0.0.1:{port}'}, timeout=15)
    print(f'  After  — Port {port}: {r.json()}')
controller.close()
"
}

# ── Main ──
case "${1:-start}" in
    start)
        echo -e "${CYAN}🚀 Tor Proxy Toolkit — Starting All Services${NC}"
        check_deps
        start_tor
        sleep 3
        start_claude_proxy
        start_codex_proxy
        start_rotator
        show_status
        ;;
    stop)     stop_all ;;
    restart)  stop_all; sleep 2; $0 start ;;
    status)   show_status ;;
    rotate)   manual_rotate ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|rotate}"
        exit 1
        ;;
esac
