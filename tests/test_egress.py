"""Egress lanes: parsing, sticky per-account choice, the loopback forwarder, and the guarantee
that nothing goes direct once a lane is configured."""
import base64
import os
import socket
import threading
from unittest import mock

import pytest

import egress


# ---------- lane list ----------

def test_webshare_direct_lines_and_urls_and_comments(tmp_path, monkeypatch):
    f = tmp_path / "proxies.txt"
    f.write_text(
        "# webshare export\n"
        "1.2.3.4:8080:alice:s3cret\n"
        "http://bob:pw@5.6.7.8:9000\n"
        "1.2.3.4:8080:alice:s3cret\n"          # duplicate host -> dropped
        "socks5://x:y@9.9.9.9:1080\n"           # unsupported scheme -> skipped
        "9.9.9.9:1\n"                           # unauthenticated ip:port
        "garbage\n"
    )
    monkeypatch.setenv("PROXIES_FILE", str(f))
    monkeypatch.delenv("PROXY_URLS", raising=False)
    lanes = egress.load_lanes()
    assert [lane.host for lane in lanes] == ["1.2.3.4:8080", "5.6.7.8:9000", "9.9.9.9:1"]
    assert lanes[0].url == "http://alice:s3cret@1.2.3.4:8080"
    assert lanes[2].url == "http://9.9.9.9:1"


def test_password_with_colons_survives():
    assert egress._parse_line("1.2.3.4:8080:u:p:w:x") == "http://u:p:w:x@1.2.3.4:8080"


def test_env_lanes_come_first_and_file_is_optional(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXY_URLS", "http://a:b@10.0.0.1:1,  http://c:d@10.0.0.2:2")
    monkeypatch.setenv("PROXIES_FILE", str(tmp_path / "missing.txt"))
    assert [lane.host for lane in egress.load_lanes()] == ["10.0.0.1:1", "10.0.0.2:2"]


def test_proxy_host_strips_credentials():
    assert egress.proxy_host("http://user:pa:ss@1.2.3.4:8080") == "1.2.3.4:8080"
    assert "user" not in egress.proxy_host("http://user:pw@host.example:1")


def test_required_defaults_to_whether_lanes_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXIES_FILE", str(tmp_path / "none.txt"))
    monkeypatch.delenv("PROXY_URLS", raising=False)
    monkeypatch.delenv("PROXY_REQUIRED", raising=False)
    assert egress.required() is False
    monkeypatch.setenv("PROXY_URLS", "http://a:b@10.0.0.1:1")
    assert egress.required() is True
    monkeypatch.setenv("PROXY_REQUIRED", "false")
    assert egress.required() is False
    monkeypatch.delenv("PROXY_URLS")
    monkeypatch.setenv("PROXY_REQUIRED", "true")
    assert egress.required() is True


def test_choose_lane_is_least_loaded_and_rotation_avoids_current():
    lanes = [egress.Lane("http://a", "a:1"), egress.Lane("http://b", "b:1"), egress.Lane("http://c", "c:1")]
    assert egress.choose_lane({"a:1": 2, "b:1": 1, "c:1": 1}, lanes).host in ("b:1", "c:1")
    assert egress.choose_lane({"a:1": 2, "b:1": 1, "c:1": 3}, lanes).host == "b:1"
    assert egress.choose_lane({}, lanes, avoid="a:1").host != "a:1"
    assert egress.choose_lane({}, lanes[:1], avoid="a:1").host == "a:1"   # sole lane: still used
    assert egress.choose_lane({}, []) is None


def test_chrome_proxy_args():
    assert egress.chrome_proxy_args(None) == []
    fw = mock.Mock(address="http://127.0.0.1:4321")
    args = egress.chrome_proxy_args(fw)
    assert "--proxy-server=http://127.0.0.1:4321" in args
    assert any(a.startswith("--force-webrtc-ip-handling-policy=") for a in args)


# ---------- forwarder ----------

class FakeUpstreamProxy:
    """Records what it was sent, answers CONNECT with 200 and plain requests with a fixed body."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.requests = []
        self.echo = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = c.recv(4096)
            if not chunk:
                break
            head += chunk
        self.requests.append(head)
        if head.startswith(b"CONNECT "):
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            data = c.recv(4096)          # whatever the client sends inside the tunnel
            c.sendall(b"tunnel:" + data)
        else:
            c.sendall(self.echo)
        c.close()

    def close(self):
        self.srv.close()


@pytest.fixture
def upstream():
    up = FakeUpstreamProxy()
    yield up
    up.close()


def _lane(up):
    return egress.Lane(f"http://alice:s3cret@127.0.0.1:{up.port}", f"127.0.0.1:{up.port}")


def test_forwarder_adds_credentials_to_connect_and_relays_tunnel(upstream):
    fw = egress.LocalForwarder(_lane(upstream)).start()
    try:
        with socket.create_connection(("127.0.0.1", fw.port), timeout=5) as s:
            s.sendall(b"CONNECT www.nytimes.com:443 HTTP/1.1\r\nHost: www.nytimes.com:443\r\n"
                      b"Proxy-Connection: keep-alive\r\n\r\n")
            reply = s.recv(4096)
            assert reply.startswith(b"HTTP/1.1 200")
            s.sendall(b"ping")
            assert s.recv(4096) == b"tunnel:ping"
    finally:
        fw.stop()
    head = upstream.requests[0]
    expected = b"Proxy-Authorization: Basic " + base64.b64encode(b"alice:s3cret")
    assert expected in head
    assert b"Proxy-Connection" not in head
    assert head.startswith(b"CONNECT www.nytimes.com:443 HTTP/1.1")


def test_forwarder_plain_http_request_forced_to_close(upstream):
    fw = egress.LocalForwarder(_lane(upstream)).start()
    try:
        with socket.create_connection(("127.0.0.1", fw.port), timeout=5) as s:
            s.sendall(b"GET http://ezproxy.example.org/login HTTP/1.1\r\nHost: ezproxy.example.org\r\n"
                      b"Proxy-Authorization: Basic Ym9ndXM=\r\nConnection: keep-alive\r\n\r\n")
            body = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                body += chunk
            assert body.endswith(b"hello")
    finally:
        fw.stop()
    head = upstream.requests[0]
    assert head.count(b"Proxy-Authorization:") == 1
    assert b"Ym9ndXM=" not in head                     # the client's own header is dropped
    assert b"Connection: close" in head and b"keep-alive" not in head


def test_forwarder_reports_502_when_lane_unreachable():
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()                                       # nothing listens here any more
    fw = egress.LocalForwarder(egress.Lane(f"http://u:p@127.0.0.1:{port}", f"127.0.0.1:{port}")).start()
    try:
        with socket.create_connection(("127.0.0.1", fw.port), timeout=5) as s:
            s.sendall(b"CONNECT a.nytimes.com:443 HTTP/1.1\r\nHost: a.nytimes.com:443\r\n\r\n")
            assert s.recv(4096).startswith(b"HTTP/1.1 502")
    finally:
        fw.stop()


def test_forwarder_binds_loopback_only_and_stop_closes_listener(upstream):
    fw = egress.LocalForwarder(_lane(upstream)).start()
    assert fw.address.startswith("http://127.0.0.1:")
    fw.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", fw.port), timeout=1).close()


# ---------- renewer threads the lane through every transport ----------

def test_renewer_passes_proxy_to_httpx_and_chrome(monkeypatch, tmp_path):
    import renewer

    clients = []

    class FakeResponse:
        status_code = 302
        is_redirect = True
        headers = {"location": "https://www.nytimes.com/subscription/ip-pass?ip_token=abc"}
        url = "https://ezproxy.example.org/login"
        text = ""

    class FakeClient:
        def __init__(self, **kw):
            clients.append(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            return FakeResponse()

    monkeypatch.setattr(renewer.httpx, "Client", FakeClient)
    monkeypatch.setattr(renewer, "extract_cookies",
                        lambda *a, **k: [{"name": "NYT-S", "value": "x", "domain": ".nytimes.com"}])
    captured = {}

    def fake_browser(provisional, *, account_id, started, proxy=None):
        captured["proxy"] = proxy
        return provisional

    monkeypatch.setattr(renewer, "_redeem_via_browser", fake_browser)

    result = renewer.renew(library_url="https://ezproxy.example.org/login?url=nyt",
                           library_user="u", library_pass="p", account_id=1,
                           proxy="http://a:b@10.0.0.1:1", egress_label="10.0.0.1:1")
    assert clients[0]["proxy"] == "http://a:b@10.0.0.1:1"
    assert captured["proxy"] == "http://a:b@10.0.0.1:1"
    assert result.message.endswith("[via 10.0.0.1:1]")
    assert "a:b" not in result.message


def test_browser_launch_uses_forwarder_for_lane(monkeypatch, tmp_path):
    import renewer

    launched = {}

    class FakeProc:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            pass

        def kill(self):
            pass

    def fake_popen(args, **kw):
        launched["args"] = args
        return FakeProc()

    profile = tmp_path / "profile"
    (profile / "Default").mkdir(parents=True)
    import capture_session
    monkeypatch.setattr(capture_session, "_find_chrome_binary", lambda: "/bin/true")
    monkeypatch.setattr(capture_session, "profile_dir_for", lambda _id: str(profile))
    monkeypatch.setattr(renewer.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(renewer, "_wait_for_cdp", lambda port, timeout=15: None)
    started_fw = []
    real_start = egress.LocalForwarder.start

    def spy_start(self):
        started_fw.append(self)
        return real_start(self)

    monkeypatch.setattr(egress.LocalForwarder, "start", spy_start)

    provisional = renewer.RenewalResult(renewer.State.RENEWED, "x",
                                        final_url="https://www.nytimes.com/x?ip_token=1")
    renewer._redeem_via_browser(provisional, account_id=7,
                                started=renewer.datetime.now(renewer.timezone.utc),
                                proxy="http://a:b@10.0.0.1:1")
    assert len(started_fw) == 1
    fw = started_fw[0]
    assert f"--proxy-server={fw.address}" in launched["args"]
    assert fw._stop.is_set()                             # stopped on the way out
    assert not any("a:b" in a for a in launched["args"])  # credentials never on the command line


# ---------- per-account pin (DB-backed) ----------

def test_account_lane_is_sticky_and_refuses_direct_when_lane_gone(app_client, monkeypatch):
    app_mod, _ = app_client
    import helpers
    from models import Account

    monkeypatch.setenv("PROXY_URLS", "http://a:b@10.0.0.1:1 http://c:d@10.0.0.2:2")
    monkeypatch.delenv("PROXIES_FILE", raising=False)
    monkeypatch.delenv("PROXY_REQUIRED", raising=False)
    with app_mod.app.app_context():
        acct = Account(name="pin-test", library_type="lib", library_username="u",
                       library_password="p")
        app_mod.db.session.add(acct)
        app_mod.db.session.commit()
        try:
            first = helpers.account_lane(acct)
            assert first.host in ("10.0.0.1:1", "10.0.0.2:2")
            assert acct.egress_lane == first.host
            assert helpers.account_lane(acct).host == first.host     # sticky
            assert helpers.account_lane(acct, for_capture=True).host == first.host

            other = helpers.account_lane(acct, for_capture=True, rotate=True)
            assert other.host != first.host
            assert acct.egress_lane == other.host

            # Lane disappears from config: renewals refuse, capture re-pins.
            monkeypatch.setenv("PROXY_URLS", "http://a:b@10.0.0.1:1")
            if other.host != "10.0.0.1:1":
                with pytest.raises(egress.EgressError):
                    helpers.account_lane(acct)
                assert helpers.account_lane(acct, for_capture=True).host == "10.0.0.1:1"

            # No lanes at all + required: refuse; not required: direct (None).
            monkeypatch.delenv("PROXY_URLS")
            monkeypatch.setenv("PROXIES_FILE", os.path.join(os.sep, "nonexistent", "proxies.txt"))
            monkeypatch.setenv("PROXY_REQUIRED", "true")
            with pytest.raises(egress.EgressError):
                helpers.account_lane(acct)
            monkeypatch.setenv("PROXY_REQUIRED", "false")
            assert helpers.account_lane(acct) is None
        finally:
            app_mod.db.session.delete(acct)
            app_mod.db.session.commit()


def test_status_endpoint_reports_egress(app_client, monkeypatch):
    app_mod, client = app_client
    monkeypatch.setenv("PROXY_URLS", "http://a:b@10.0.0.1:1")
    monkeypatch.setenv("PROXIES_FILE", os.path.join(os.sep, "nonexistent", "proxies.txt"))
    data = client.get("/api/status").get_json()
    assert data["egress"] == {"lanes": ["10.0.0.1:1"], "required": True, "source": None}
    assert "a:b" not in str(data)


def test_paste_cookies_pins_and_repins_lane(app_client, monkeypatch):
    """Saving cookies is the capture moment for the paste path: it must pin the account to a
    lane, and re-pin it when the pinned lane has vanished from the config (the recovery the
    `Egress blocked` message tells the user to perform)."""
    app_mod, client = app_client
    import cookie_jar
    from models import Account

    saved = []
    monkeypatch.setattr(cookie_jar, "save_pasted_cookies",
                        lambda account_id, raw, newspaper_type="nyt": saved.append(raw) or [{"name": "NYT-S"}])
    monkeypatch.setenv("PROXY_URLS", "http://a:b@10.0.0.1:1")
    monkeypatch.setenv("PROXIES_FILE", os.path.join(os.sep, "nonexistent", "proxies.txt"))
    monkeypatch.delenv("PROXY_REQUIRED", raising=False)
    with app_mod.app.app_context():
        acct = Account(name="paste-pin", library_type="lib", library_username="u",
                       library_password="p", egress_lane="10.9.9.9:9")   # lane no longer configured
        app_mod.db.session.add(acct)
        app_mod.db.session.commit()
        acct_id = acct.id
    try:
        r = client.post(f"/accounts/{acct_id}/cookies", data={"cookies_json": "x" * 40})
        assert r.status_code == 302 and r.headers["Location"].endswith("/accounts")
        assert saved == ["x" * 40]
        with app_mod.app.app_context():
            assert app_mod.db.session.get(Account, acct_id).egress_lane == "10.0.0.1:1"

        # Required but nothing usable: refuse and keep the cookies unsaved.
        monkeypatch.delenv("PROXY_URLS")
        monkeypatch.setenv("PROXY_REQUIRED", "true")
        r = client.post(f"/accounts/{acct_id}/cookies", data={"cookies_json": "y" * 40})
        assert r.status_code == 302 and r.headers["Location"].endswith(f"/accounts/{acct_id}/capture")
        assert saved == ["x" * 40]
    finally:
        with app_mod.app.app_context():
            app_mod.db.session.delete(app_mod.db.session.get(Account, acct_id))
            app_mod.db.session.commit()
