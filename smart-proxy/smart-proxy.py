#!/usr/bin/env python3
"""smart-proxy: localhost forward proxy with automatic tunnel/direct failover.

Clients point here ALWAYS (e.g. HTTPS_PROXY=http://127.0.0.1:18090) and this
proxy decides per request:
  tunnel healthy -> forward via upstream proxy (wireproxy stable :18104)
  tunnel down    -> connect direct (no failure, no restart needed)

If the upstream dies mid-request, that request falls back to direct.
Health is a cached probe (default 15s): TCP to the upstream port + one real
request through it to PROBE_URL (any HTTP status, even 429/404, proves
TCP+TLS+data). A missing/dead active.pid fails fast without waiting.

Stdlib only. Handles CONNECT (HTTPS) + plain-HTTP methods. Long SSE
streams stay open (no idle timeout on relay).

Env overrides:
  SMART_LISTEN=127.0.0.1:18090  SMART_UPSTREAM=127.0.0.1:18104
  SMART_PROBE_URL=https://api.kilo.ai/api/gateway/models
  SMART_PROBE_INTERVAL=15  SMART_PROBE_TIMEOUT=8
  SMART_PIDFILE=<wireproxy active.pid path for fast-fail>
"""

import os
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

LISTEN = os.environ.get("SMART_LISTEN", "127.0.0.1:18090")
UPSTREAM = os.environ.get("SMART_UPSTREAM", "127.0.0.1:18104")
PROBE_URL = os.environ.get(
    "SMART_PROBE_URL", "https://api.kilo.ai/api/gateway/models"
)
INTERVAL = float(os.environ.get("SMART_PROBE_INTERVAL", "15"))
TIMEOUT = float(os.environ.get("SMART_PROBE_TIMEOUT", "8"))
PIDFILE = os.environ.get(
    "SMART_PIDFILE",
    "/teamspace/studios/this_studio/wgtunnel/wireproxy/active.pid",
)


def _parse_hostport(s, default_port=80):
    if ":" in s:
        h, p = s.rsplit(":", 1)
        try:
            return h, int(p)
        except ValueError:
            pass
    return s, default_port


_state = {"healthy": False, "checked": 0.0, "lock": threading.Lock()}


def _pid_alive():
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip().split()[0])
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _probe_once():
    """True if the tunnel data path works right now."""
    if not _pid_alive():
        return False
    uh, up = _parse_hostport(UPSTREAM, 8080)
    try:
        s = socket.create_connection((uh, up), timeout=TIMEOUT)
    except Exception:
        return False
    try:
        # Plain-HTTP probe through the upstream proxy (absolute URI form).
        req = (
            "GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n"
            % (PROBE_URL, PROBE_URL.split("/")[2])
        )
        s.sendall(req.encode())
        s.settimeout(TIMEOUT)
        data = s.recv(12)
        return data.startswith(b"HTTP/")
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def healthy():
    now = time.monotonic()
    with _state["lock"]:
        if now - _state["checked"] < INTERVAL:
            return _state["healthy"]
    ok = _probe_once()
    with _state["lock"]:
        _state["healthy"] = ok
        _state["checked"] = time.monotonic()
    return ok


def mark_down():
    with _state["lock"]:
        _state["healthy"] = False
        _state["checked"] = time.monotonic()


def _relay(a, b):
    """Blind bidirectional relay until either side closes."""
    for s in (a, b):
        try:
            s.setblocking(False)
        except Exception:
            pass
    while True:
        try:
            r, _, _ = select.select([a, b], [], [], 300)
        except Exception:
            break
        if not r:
            continue
        try:
            data = r[0].recv(65536)
        except Exception:
            break
        if not data:
            break
        try:
            (b if r[0] is a else a).sendall(data)
        except Exception:
            break


def _connect_direct(host, port):
    return socket.create_connection((host, port), timeout=30)


def _connect_via_upstream(host, port):
    uh, up = _parse_hostport(UPSTREAM, 8080)
    s = socket.create_connection((uh, up), timeout=15)
    s.sendall(
        ("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n" % (host, port, host, port)).encode()
    )
    resp = b""
    s.settimeout(15)
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            break
        resp += chunk
    if not resp.startswith(b"HTTP/1.1 200") and not resp.startswith(b"HTTP/1.0 200"):
        raise OSError("upstream CONNECT refused: %r" % resp[:60])
    return s


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _read_head(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            return self.rfile.read(length)
        return b""

    def _target(self):
        """(host, port, path) for this request."""
        if self.command == "CONNECT":
            host, port = _parse_hostport(self.path, 443)
            return host, port, None
        url = self.path
        if url.startswith("http://") or url.startswith("https://"):
            rest = url.split("://", 1)[1]
            hp, _, path = rest.partition("/")
            host, port = _parse_hostport(hp, 443 if url.startswith("https") else 80)
            return host, port, "/" + path
        host = (self.headers.get("Host") or "").split(":")[0]
        return host, 80, url

    def _serve(self):
        host, port, _ = self._target()
        if not host:
            self.send_error(400, "no host")
            return
        if self.command == "CONNECT":
            self._serve_connect(host, port)
        else:
            self._serve_http(host, port)

    def _serve_connect(self, host, port):
        try:
            if healthy():
                try:
                    upstream = _connect_via_upstream(host, port)
                except Exception:
                    mark_down()
                    upstream = _connect_direct(host, port)
            else:
                upstream = _connect_direct(host, port)
        except Exception:
            self.send_error(502, "connect failed")
            return
        try:
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.wfile.flush()
        except Exception:
            try:
                upstream.close()
            except Exception:
                pass
            return
        _relay(self.connection, upstream)
        try:
            upstream.close()
        except Exception:
            pass

    def _serve_http(self, host, port):
        body = self._read_head()
        # Rebuild head in origin form for direct, absolute form for upstream.
        lines = ["%s %s HTTP/1.1" % (self.command, self.path)]
        for k, v in self.headers.items():
            if k.lower() in ("proxy-connection", "proxy-authorization"):
                continue
            lines.append("%s: %s" % (k, v))
        head_origin = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
        use_upstream = healthy()
        if use_upstream:
            try:
                s = socket.create_connection(_parse_hostport(UPSTREAM, 8080), timeout=15)
                s.sendall(head_origin)
                _relay(self.connection, s)
                try:
                    s.close()
                except Exception:
                    pass
                return
            except Exception:
                mark_down()
        try:
            # Direct: rewrite request line to origin-form path.
            url = self.path
            if "://" in url:
                url = "/" + url.split("://", 1)[1].partition("/")[2]
            lines[0] = "%s %s HTTP/1.1" % (self.command, url or "/")
            head = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
            s = _connect_direct(host, port)
            s.sendall(head)
            _relay(self.connection, s)
            try:
                s.close()
            except Exception:
                pass
        except Exception:
            try:
                self.send_error(502, "forward failed")
            except Exception:
                pass

    do_CONNECT = _serve
    do_GET = _serve
    do_POST = _serve
    do_PUT = _serve
    do_DELETE = _serve
    do_PATCH = _serve
    do_HEAD = _serve
    do_OPTIONS = _serve


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    lh, lp = _parse_hostport(LISTEN, 18090)
    srv = Server((lh, lp), Handler)
    print("smart-proxy on %s:%d -> upstream %s (probe %ss), direct fallback on" % (lh, lp, UPSTREAM, INTERVAL), flush=True)
    # Prime the health cache in background so the port binds immediately.
    threading.Thread(target=healthy, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
