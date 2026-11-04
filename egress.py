"""Egress lanes: every library / NYT request leaves through a proxy, never the host's own IP.

A *lane* is one upstream HTTP proxy URL. Lanes come from the PROXY_URLS env var and/or a
proxies file (Webshare "direct" export, ip:port:user:pass per line; full URLs and # comments
accepted). Each account is pinned to one lane (Account.egress_lane, host:port) from the moment
its NYT session is captured, so the DataDome trust minted during capture and every later renewal
share the same exit IP. The pin is only re-chosen at capture time (or explicitly rotated).

Three transports use the lane:
  - httpx (EZproxy chain, data-layer verify): proxy=<lane url>; the proxy resolves DNS (CONNECT).
  - Chromium (capture + headless redemption): --proxy-server=http://127.0.0.1:<port> pointing at a
    LocalForwarder, because Chromium cannot take credentials on --proxy-server. The forwarder is a
    loopback-only CONNECT relay that adds Proxy-Authorization and tunnels to the lane.

Guarantee: once any lane is configured, nothing falls back to a direct request. With
PROXY_REQUIRED=true the app refuses to renew or capture at all while no lane is usable.
"""

from __future__ import annotations

import base64
import logging
import os
import random
import select
import socket
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from paths import DATA_DIR

logger = logging.getLogger(__name__)

DEFAULT_PROXIES_FILE = os.path.join(DATA_DIR, 'proxies.txt')


class EgressError(RuntimeError):
    """No usable lane for a request that must not go direct."""


@dataclass(frozen=True)
class Lane:
    url: str          # full proxy URL with credentials; never logged or displayed
    host: str         # host:port, the stable id stored on the account

    @property
    def scheme(self) -> str:
        return urlsplit(self.url).scheme or 'http'


def proxy_host(url: str) -> str:
    """host:port of a proxy URL with credentials stripped (safe to log / show)."""
    parts = urlsplit(url if '://' in url else 'http://' + url)
    host = parts.hostname or ''
    return f'{host}:{parts.port}' if parts.port else host


def _parse_line(line: str) -> Optional[str]:
    line = line.strip()
    if not line or line.startswith('#'):
        return None
    if '://' in line:
        return line
    fields = line.split(':')
    if len(fields) >= 4:
        ip, port, user = fields[0], fields[1], fields[2]
        pw = ':'.join(fields[3:])
        return f'http://{user}:{pw}@{ip}:{port}'
    if len(fields) == 2:
        return f'http://{fields[0]}:{fields[1]}'
    logger.warning("egress: ignoring unparseable proxy line (%d fields)", len(fields))
    return None


def proxies_file() -> str:
    return os.environ.get('PROXIES_FILE') or DEFAULT_PROXIES_FILE


def load_lanes() -> List[Lane]:
    """Lanes from PROXY_URLS (comma / whitespace separated) and the proxies file, de-duplicated,
    in configuration order. Re-read on every call so a changed file needs no restart."""
    raw: List[str] = []
    env = os.environ.get('PROXY_URLS', '')
    for tok in env.replace(',', ' ').split():
        raw.append(tok)
    path = proxies_file()
    try:
        with open(path) as f:
            for line in f:
                raw.append(line)
    except FileNotFoundError:
        if os.environ.get('PROXIES_FILE'):
            logger.error("egress: PROXIES_FILE %s not found", path)
    except OSError as e:
        logger.error("egress: cannot read %s: %s", path, e)

    seen = set()
    lanes: List[Lane] = []
    for item in raw:
        url = _parse_line(item)
        if not url:
            continue
        scheme = urlsplit(url).scheme
        if scheme not in ('http', 'https'):
            logger.warning("egress: lane %s uses %s; only http(s) proxies are supported, skipping",
                           proxy_host(url), scheme)
            continue
        host = proxy_host(url)
        if host in seen:
            continue
        seen.add(host)
        lanes.append(Lane(url=url, host=host))
    return lanes


def required() -> bool:
    """True when a request without a usable lane must fail instead of going direct.
    Default: true as soon as any lane is configured; PROXY_REQUIRED overrides either way."""
    override = os.environ.get('PROXY_REQUIRED')
    if override is not None and override != '':
        return override.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(load_lanes())


def lane_for(host: Optional[str], lanes: Optional[List[Lane]] = None) -> Optional[Lane]:
    if not host:
        return None
    for lane in (load_lanes() if lanes is None else lanes):
        if lane.host == host:
            return lane
    return None


def choose_lane(usage: Dict[str, int], lanes: Optional[List[Lane]] = None,
                avoid: Optional[str] = None) -> Optional[Lane]:
    """Least-loaded lane (by number of accounts pinned to it), random among ties. `avoid` is the
    lane a rotation moves away from; it is only picked when it is the sole lane."""
    lanes = load_lanes() if lanes is None else lanes
    if not lanes:
        return None
    candidates = [lane for lane in lanes if lane.host != avoid] or lanes
    low = min(usage.get(lane.host, 0) for lane in candidates)
    return random.choice([lane for lane in candidates if usage.get(lane.host, 0) == low])


def status() -> dict:
    lanes = load_lanes()
    return {
        'lanes': [lane.host for lane in lanes],
        'required': required(),
        'source': proxies_file() if os.path.isfile(proxies_file()) else None,
    }


# ---------- Chromium: loopback forwarder that adds proxy credentials ----------

def chrome_proxy_args(forwarder: Optional['LocalForwarder']) -> List[str]:
    """Flags that route all of Chromium's traffic through the forwarder and stop WebRTC from
    revealing the host's real IP over UDP."""
    if forwarder is None:
        return []
    return [
        f'--proxy-server={forwarder.address}',
        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
    ]


class LocalForwarder:
    """Tiny HTTP proxy on 127.0.0.1 that relays to one authenticated upstream proxy.

    CONNECT (https) is tunnelled verbatim with Proxy-Authorization added; plain absolute-URI
    requests (http) are forwarded one per connection. The target hostname is resolved by the
    upstream, so no DNS for the vendor hosts leaves the box either. One instance per Chromium
    launch; stop() closes the listener and every live tunnel.
    """

    BUFSIZE = 65536

    def __init__(self, lane: Lane):
        self.lane = lane
        parts = urlsplit(lane.url)
        self._up_host = parts.hostname
        self._up_port = parts.port or (443 if parts.scheme == 'https' else 80)
        self._up_tls = parts.scheme == 'https'
        self._auth = None
        if parts.username is not None:
            cred = f'{parts.username}:{parts.password or ""}'.encode()
            self._auth = b'Basic ' + base64.b64encode(cred)
        self._srv: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._conns: set = set()
        self._lock = threading.Lock()
        self.port = 0

    @property
    def address(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    def start(self) -> 'LocalForwarder':
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', 0))
        srv.listen(64)
        srv.settimeout(0.5)
        self._srv = srv
        self.port = srv.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, name='egress-forwarder', daemon=True)
        self._thread.start()
        logger.info("egress: forwarder on %s -> lane %s", self.address, self.lane.host)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
        with self._lock:
            conns = list(self._conns)
            self._conns.clear()
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    # -- internals --

    def _track(self, sock: socket.socket) -> None:
        with self._lock:
            self._conns.add(sock)

    def _untrack(self, sock: socket.socket) -> None:
        with self._lock:
            self._conns.discard(sock)
        try:
            sock.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._track(client)
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _connect_upstream(self) -> socket.socket:
        up = socket.create_connection((self._up_host, self._up_port), timeout=20)
        if self._up_tls:
            import ssl
            up = ssl.create_default_context().wrap_socket(up, server_hostname=self._up_host)
        up.settimeout(None)
        self._track(up)
        return up

    @staticmethod
    def _read_head(sock: socket.socket) -> bytes:
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return buf
            buf += chunk
            if len(buf) > 1 << 20:
                break
        return buf

    def _rewrite_head(self, head: bytes) -> bytes:
        """Drop any client Proxy-Authorization / Proxy-Connection, add ours, close after one
        plain request so a keep-alive connection never carries an unauthenticated second one."""
        lines = head.split(b'\r\n')
        out = [lines[0]]
        for line in lines[1:]:
            if not line:
                continue
            key = line.split(b':', 1)[0].strip().lower()
            if key in (b'proxy-authorization', b'proxy-connection'):
                continue
            out.append(line)
        if self._auth:
            out.append(b'Proxy-Authorization: ' + self._auth)
        if not lines[0].upper().startswith(b'CONNECT '):
            out = [line for line in out if not line.lower().startswith(b'connection:')]
            out.append(b'Connection: close')
        return b'\r\n'.join(out) + b'\r\n\r\n'

    def _handle(self, client: socket.socket) -> None:
        up = None
        try:
            client.settimeout(30)
            raw = self._read_head(client)
            if b'\r\n\r\n' not in raw:
                return
            head, rest = raw.split(b'\r\n\r\n', 1)
            request_line = head.split(b'\r\n', 1)[0]
            try:
                up = self._connect_upstream()
            except OSError as e:
                logger.warning("egress: lane %s unreachable: %s", self.lane.host, e)
                client.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                return
            up.sendall(self._rewrite_head(head) + rest)
            if request_line.upper().startswith(b'CONNECT '):
                # The upstream answers the CONNECT itself; relay its status line to Chromium
                # verbatim (200 opens the tunnel, 407 / 403 surface as a proxy error there).
                pass
            client.settimeout(None)
            self._pipe(client, up)
        except Exception as e:  # noqa: BLE001 — one tunnel failing must not take the forwarder down
            logger.debug("egress: forwarder connection ended: %s", e)
        finally:
            if up is not None:
                self._untrack(up)
            self._untrack(client)

    def _pipe(self, a: socket.socket, b: socket.socket) -> None:
        socks = [a, b]
        while not self._stop.is_set():
            try:
                readable, _, errored = select.select(socks, [], socks, 1.0)
            except (OSError, ValueError):
                return
            if errored:
                return
            for s in readable:
                try:
                    data = s.recv(self.BUFSIZE)
                except OSError:
                    return
                if not data:
                    return
                other = b if s is a else a
                try:
                    other.sendall(data)
                except OSError:
                    return
