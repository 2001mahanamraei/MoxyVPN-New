"""Server list + latency ("ping") measurements for MoxyVPN.

* load_servers()  - Mozilla's public VPN server list (Remote Settings), cached on disk
* ping_all()      - TCP-connect latency to the servers of every country (what the Android app does too)
* tunnel_ping()   - latency *through* the running VPN (SOCKS5 CONNECT via the local proxy)

Everything here blocks, so call it from a worker thread.
"""
import json
import os
import socket
import ssl
import struct
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import usage

REMOTE_SETTINGS_HOST = "firefox.settings.services.mozilla.com"
REMOTE_SETTINGS_PATH = "/v1/buckets/main/collections/vpn-serverlist/records"
USER_AGENT = "MozillaVPN/2.35.0 (sys:linux; iap:true)"
CACHE_FILE = os.path.join(usage.STATE_DIR, "serverlist.json")
CACHE_MAX_AGE = 12 * 3600
PING_TIMEOUT = 1.5
TARGETS_PER_COUNTRY = 4


# ------------------------------------------------------------------ HTTP (direct / http proxy / socks5)
def _socks5_connect(sock, host, port, user=None, pw=None):
    """Do a SOCKS5 handshake + CONNECT on an already-open socket to the proxy."""
    sock.sendall(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")
    ver, method = sock.recv(2)
    if method == 2:
        u, p = (user or "").encode(), (pw or "").encode()
        sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
        if sock.recv(2)[1] != 0:
            raise OSError("SOCKS5 authentication failed")
    elif method != 0:
        raise OSError("SOCKS5 proxy refused the handshake")
    try:
        addr = b"\x01" + socket.inet_aton(host)
    except OSError:
        h = host.encode()
        addr = b"\x03" + bytes([len(h)]) + h
    sock.sendall(b"\x05\x01\x00" + addr + struct.pack(">H", port))
    head = _recv_exact(sock, 4)
    if head[1] != 0:
        raise OSError(f"SOCKS5 connect failed (code {head[1]})")
    skip = {1: 4, 4: 16}.get(head[3])
    if skip is None:                                   # domain name
        skip = _recv_exact(sock, 1)[0]
    _recv_exact(sock, skip + 2)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("connection closed")
        buf += chunk
    return buf


def _decode_chunked(body):
    out = b""
    while body:
        line, _, rest = body.partition(b"\r\n")
        try:
            size = int(line.split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        out += rest[:size]
        body = rest[size + 2:]
    return out


def _http_get(host, path, proxy, timeout=10):
    """GET https://host/path -> bytes. proxy: '' | http(s)://h:p | socks5://[user:pw@]h:p"""
    proxy = (proxy or "").strip()
    scheme = urllib.parse.urlsplit(proxy).scheme.lower() if proxy else ""
    if scheme.startswith("socks"):
        u = urllib.parse.urlsplit(proxy)
        sock = socket.create_connection((u.hostname, u.port or 1080), timeout=timeout)
        try:
            _socks5_connect(sock, host, 443, u.username, u.password)
            tls = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            tls.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {USER_AGENT}\r\n"
                         "Accept: application/json\r\nAccept-Encoding: identity\r\n"
                         "Connection: close\r\n\r\n").encode())
            data = b""
            while True:
                chunk = tls.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        head, _, body = data.partition(b"\r\n\r\n")
        if b" 200" not in head.split(b"\r\n")[0]:
            raise OSError("server list: " + head.split(b"\r\n")[0].decode("latin1"))
        return _decode_chunked(body) if b"chunked" in head.lower() else body
    handlers = [urllib.request.ProxyHandler({"https": proxy, "http": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(f"https://{host}{path}",
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with opener.open(req, timeout=timeout) as r:
        return r.read()


# ------------------------------------------------------------------ server list
def parse_servers(raw):
    """Mozilla record list -> {CODE: {"name": str, "targets": [(host, port), ...]}}"""
    data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
    out = {}
    for rec in data.get("data", []):
        c = rec.get("country") if isinstance(rec.get("country"), dict) else rec
        code, name = str(c.get("code", "")).upper(), c.get("name", "")
        if not code or name.lower() == "catchall anycast":
            continue
        targets = []
        for city in c.get("cities", []):
            for srv in city.get("servers", []):
                if srv.get("quarantined"):
                    continue
                host, port = srv.get("hostname", ""), int(srv.get("port") or 0)
                for proto in srv.get("protocols", []):
                    if proto.get("name") == "connect":
                        host = proto.get("host") or host
                        port = int(proto.get("port") or port)
                        break
                if host and port:
                    targets.append((host, port))
                    break                               # one server per city is enough
        if targets:
            entry = out.setdefault(code, {"name": name, "targets": []})
            entry["targets"] += targets
    for e in out.values():
        e["targets"] = e["targets"][:TARGETS_PER_COUNTRY]
    return out


def load_servers(proxies=(), force=False):
    """Return (servers, source). `proxies` are tried in order after a direct attempt; the on-disk
    cache is the last resort, so pinging still works when Mozilla's list is unreachable."""
    cached = None
    try:
        if os.path.exists(CACHE_FILE):
            age = time.time() - os.path.getmtime(CACHE_FILE)
            with open(CACHE_FILE, "rb") as f:
                cached = f.read()
            if not force and age < CACHE_MAX_AGE:
                return parse_servers(cached), "cache"
    except (OSError, ValueError):
        cached = None
    last = None
    for proxy in ("",) + tuple(p for p in proxies if p):
        try:
            raw = _http_get(REMOTE_SETTINGS_HOST, REMOTE_SETTINGS_PATH, proxy)
            servers = parse_servers(raw)
            if servers:
                try:
                    os.makedirs(usage.STATE_DIR, exist_ok=True)
                    with open(CACHE_FILE, "wb") as f:
                        f.write(raw)
                except OSError:
                    pass
                return servers, "network"
        except Exception as exc:  # noqa: BLE001
            last = exc
    if cached:
        try:
            return parse_servers(cached), "cache (offline)"
        except ValueError:
            pass
    raise OSError(f"could not load the server list: {last}")


# ------------------------------------------------------------------ latency
def tcp_ping(host, port, timeout=PING_TIMEOUT, tries=2):
    """Best TCP-connect time in ms over a few tries, or None. DNS is resolved outside the timing."""
    try:
        info = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
    except OSError:
        return None
    best = None
    for _ in range(tries):
        s = socket.socket(info[0], info[1], info[2])
        s.settimeout(timeout)
        t0 = time.perf_counter()
        try:
            s.connect(info[4])
            ms = (time.perf_counter() - t0) * 1000
            best = ms if best is None else min(best, ms)
        except OSError:
            pass
        finally:
            s.close()
    return None if best is None else max(1, round(best))


def ping_country(targets):
    vals = [v for v in (tcp_ping(h, p) for h, p in targets) if v is not None]
    return min(vals) if vals else None


def ping_all(servers, on_result, workers=12, stop=lambda: False):
    """Ping every country in parallel; on_result(CODE, ms_or_None) is called from worker threads."""
    def job(item):
        code, entry = item
        if stop():
            return
        on_result(code, ping_country(entry["targets"]))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(job, servers.items()))


def tunnel_ping(host, port, target=("1.1.1.1", 443), timeout=4.0):
    """Round-trip through the running VPN: time for the local SOCKS5 proxy to open a connection to
    `target` via Mozilla's edge. Returns ms or None."""
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None
    try:
        s.settimeout(timeout)
        s.sendall(b"\x05\x01\x00")
        if s.recv(2) != b"\x05\x00":
            return None
        addr = b"\x01" + socket.inet_aton(target[0])
        t0 = time.perf_counter()
        s.sendall(b"\x05\x01\x00" + addr + struct.pack(">H", target[1]))
        head = _recv_exact(s, 4)
        ms = (time.perf_counter() - t0) * 1000
        return max(1, round(ms)) if head[1] == 0 else None
    except OSError:
        return None
    finally:
        s.close()
