#!/usr/bin/env python3
"""
tor_rotate.py — Auto rotate Tor exit node IP every 10 minutes
Dual SOCKS5 proxy: 9050 + 9060
Control port: 9051
"""

import os
import time
import sys
import stem.control
import requests

INTERVAL = 600  # 10 minutes in seconds
SOCKS_PORTS = [9050, 9060]
CONTROL_PORT = 9051
TOR_PASSWORD = os.environ.get('TOR_CONTROL_PASSWORD', 'your-tor-control-password')

def get_ip(port):
    """Get current exit IP through specified SOCKS port"""
    try:
        r = requests.get(
            'https://check.torproject.org/api/ip',
            proxies={'https': f'socks5h://127.0.0.1:{port}'},
            timeout=30
        )
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def rotate_circuit(controller):
    """Send NEWNYM signal to rotate circuit"""
    controller.signal(stem.Signal.NEWNYM)

def main():
    try:
        controller = stem.control.Controller.from_port(port=CONTROL_PORT)
        controller.authenticate(password=TOR_PASSWORD)
        print(f"✅ Connected to Tor control port {CONTROL_PORT}")
    except Exception as e:
        print(f"❌ Failed to connect: {e}")
        sys.exit(1)

    print(f"🔄 Tor IP Rotator started — rotating every {INTERVAL//60} minutes")
    print(f"📡 SOCKS ports: {', '.join(str(p) for p in SOCKS_PORTS)}\n")

    # Show initial IPs
    for port in SOCKS_PORTS:
        ip_info = get_ip(port)
        print(f"[{time.strftime('%H:%M:%S')}] Port {port}: {ip_info}")

    while True:
        print(f"\n⏳ Next rotation in {INTERVAL} seconds...")
        time.sleep(INTERVAL)

        print("\n🔄 Rotating circuit...")
        try:
            rotate_circuit(controller)
            time.sleep(2)  # wait for new circuit to build

            for port in SOCKS_PORTS:
                ip_info = get_ip(port)
                print(f"[{time.strftime('%H:%M:%S')}] Port {port}: {ip_info}")
        except Exception as e:
            print(f"⚠️ Rotation error: {e}")
            # Try to reconnect
            try:
                controller = stem.control.Controller.from_port(port=CONTROL_PORT)
                controller.authenticate(password=TOR_PASSWORD)
                print("✅ Reconnected to control port")
            except:
                print("❌ Reconnect failed, will retry next cycle")

if __name__ == '__main__':
    main()
