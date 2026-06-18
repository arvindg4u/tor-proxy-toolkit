#!/bin/bash
# tor-service.sh — Start/Stop Tor + IP Rotation
# Usage: tor-service.sh start|stop|status|rotate

PIDFILE_TOR="/tmp/tor.pid"
PIDFILE_ROTATE="/tmp/tor_rotate.pid"

start() {
    echo "🚀 Starting Tor..."
    
    # Check if already running
    if [ -f "$PIDFILE_TOR" ] && kill -0 $(cat $PIDFILE_TOR) 2>/dev/null; then
        echo "⚠️  Tor already running (PID: $(cat $PIDFILE_TOR))"
    else
        tor &
        echo $! > "$PIDFILE_TOR"
        echo "✅ Tor started (PID: $!)"
        sleep 10  # wait for bootstrap
    fi
    
    # Verify ports
    echo "📡 Checking ports..."
    netstat -tlnp 2>/dev/null | grep -E '9050|9060|9051' || echo "⚠️  Ports not ready yet"
    
    echo ""
    echo "🚀 Starting IP Rotator (10 min interval)..."
    if [ -f "$PIDFILE_ROTATE" ] && kill -0 $(cat $PIDFILE_ROTATE) 2>/dev/null; then
        echo "⚠️  Rotator already running (PID: $(cat $PIDFILE_ROTATE))"
    else
        python3 /root/tor_rotate.py > /var/log/tor/rotate.log 2>&1 &
        echo $! > "$PIDFILE_ROTATE"
        echo "✅ Rotator started (PID: $!)"
    fi
    
    echo ""
    echo "✅ All services running!"
    echo "   SOCKS Proxy 1: 127.0.0.1:9050"
    echo "   SOCKS Proxy 2: 127.0.0.1:9060"
    echo "   Control Port:  127.0.0.1:9051"
    echo "   Rotation log:  /var/log/tor/rotate.log"
}

stop() {
    echo "🛑 Stopping services..."
    
    if [ -f "$PIDFILE_ROTATE" ]; then
        kill $(cat $PIDFILE_ROTATE) 2>/dev/null
        rm -f "$PIDFILE_ROTATE"
        echo "✅ Rotator stopped"
    fi
    
    if [ -f "$PIDFILE_TOR" ]; then
        kill $(cat $PIDFILE_TOR) 2>/dev/null
        rm -f "$PIDFILE_TOR"
        echo "✅ Tor stopped"
    fi
    
    # Also kill any orphaned tor processes
    pkill -f "tor_rotate.py" 2>/dev/null
    echo "🛑 All services stopped"
}

status() {
    echo "📊 Tor Service Status"
    echo "====================="
    
    if [ -f "$PIDFILE_TOR" ] && kill -0 $(cat $PIDFILE_TOR) 2>/dev/null; then
        echo "✅ Tor:        RUNNING (PID: $(cat $PIDFILE_TOR))"
    else
        echo "❌ Tor:        STOPPED"
    fi
    
    if [ -f "$PIDFILE_ROTATE" ] && kill -0 $(cat $PIDFILE_ROTATE) 2>/dev/null; then
        echo "✅ Rotator:    RUNNING (PID: $(cat $PIDFILE_ROTATE))"
    else
        echo "❌ Rotator:    STOPPED"
    fi
    
    echo ""
    echo "📡 Port Status:"
    netstat -tlnp 2>/dev/null | grep -E '9050|9060|9051' || echo "   No ports listening"
    
    echo ""
    echo "🌐 Current IPs:"
    for port in 9050 9060; do
        IP=$(curl -s --socks5-hostname 127.0.0.1:$port --max-time 10 https://check.torproject.org/api/ip 2>/dev/null)
        echo "   Port $port: $IP"
    done
}

rotate() {
    echo "🔄 Manual rotation..."
    python3 -c "
import stem.control, requests, time
controller = stem.control.Controller.from_port(port=9051)
controller.authenticate(password='"$TOR_CONTROL_PASSWORD"')
for port in [9050, 9060]:
    r = requests.get('https://check.torproject.org/api/ip', proxies={'https': f'socks5h://127.0.0.1:{port}'}, timeout=15)
    print(f'Before — Port {port}: {r.json()}')
controller.signal(stem.Signal.NEWNYM)
print('🔄 NEWNYM sent, waiting 3s...')
time.sleep(3)
for port in [9050, 9060]:
    r = requests.get('https://check.torproject.org/api/ip', proxies={'https': f'socks5h://127.0.0.1:{port}'}, timeout=15)
    print(f'After  — Port {port}: {r.json()}')
controller.close()
"
}

case "$1" in
    start)   start ;;
    stop)    stop ;;
    status)  status ;;
    rotate)  rotate ;;
    restart) stop; sleep 2; start ;;
    *)       echo "Usage: $0 {start|stop|status|rotate|restart}" ;;
esac
