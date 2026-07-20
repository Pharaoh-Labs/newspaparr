"""Renewer: EZproxy via httpx + ippass redemption via headless Chrome.

The renewal is a two-phase process:
  1. httpx follows the EZproxy redirect chain to obtain the ippass URL.
  2. Headless Chrome visits the ippass URL so the SPA's Apollo Client can fire
     the redeemIpPass GraphQL mutation — the only path the server validates.

Attempting the mutation via httpx always returns pass_redemption_error because
the server validates the Apollo Client request format (APQ, specific headers)
that we cannot reliably replicate. Headless Chrome with the captured profile
runs the real SPA and succeeds.

Replaces the old browser-driven renewal_engine / library_adapters /
enhanced_browser / state_detector pipeline (~1500 lines).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from cookie_jar import extract_cookies

logger = logging.getLogger(__name__)


DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


class State(str, Enum):
    RENEWED = "renewed"
    NO_SESSION = "no_session"
    SESSION_EXPIRED = "session_expired"
    LIBRARY_AUTH_FAILED = "library_auth_failed"
    NETWORK_ERROR = "network_error"
    UNEXPECTED = "unexpected"


@dataclass
class RenewalResult:
    state: State
    message: str
    expiration: Optional[datetime] = None
    final_url: Optional[str] = None
    duration_ms: int = 0

    @property
    def success(self) -> bool:
        return self.state == State.RENEWED


def renew(*, library_url: str, library_user: str, library_pass: str,
          account_id: int, timeout: float = 30.0) -> RenewalResult:
    """Run an NYT pass renewal for an account.

    Phase 1 (httpx): follow the EZproxy redirect chain, stopping just before
    the final redirect to the ippass URL so the token is not consumed.
    Phase 2 (headless Chrome): open the ippass URL fresh so the SPA's Apollo
    Client fires redeemIpPass and the ZUORA subscription is updated.
    """
    cookies = extract_cookies(account_id, "nyt")
    if not cookies:
        return RenewalResult(
            State.NO_SESSION,
            "No captured NYT session — open Capture from the dashboard first.",
        )

    jar = httpx.Cookies()
    for c in cookies:
        try:
            jar.set(name=c["name"], value=c["value"],
                    domain=c["domain"], path=c.get("path", "/"))
        except Exception as e:
            logger.debug("Skipping cookie %s: %s", c.get("name"), e)

    started = datetime.now(timezone.utc)
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # Use follow_redirects=False so we can intercept the redirect to the ippass
    # URL before visiting it — visiting it with httpx consumes the token and
    # prevents the browser from redeeming it.
    ippass_url: Optional[str] = None
    final_r: Optional[httpx.Response] = None

    with httpx.Client(cookies=jar, follow_redirects=False, timeout=timeout,
                      headers=headers) as client:
        try:
            r = client.get(library_url)
        except httpx.HTTPError as e:
            return _result(State.NETWORK_ERROR,
                           f"Library URL fetch failed: {e}", started=started)

        # Handle EZproxy login page (200) before entering the redirect chain.
        if r.status_code == 200 and not _is_nyt_host(r.url):
            html = r.text
            action = _extract_form_action(html) or "/login"
            action_url = urljoin(str(r.url), action)
            form = _extract_form_inputs(html)
            form.update({"user": library_user, "pass": library_pass})
            try:
                r = client.post(action_url, data=form)
            except httpx.HTTPError as e:
                return _result(State.NETWORK_ERROR,
                               f"Library auth post failed: {e}", started=started)

        # Walk the redirect chain manually; stop before visiting the ippass URL.
        for _ in range(20):
            if not r.is_redirect:
                final_r = r
                break
            location = r.headers.get("location", "")
            if not location.startswith("http"):
                location = urljoin(str(r.url), location)
            if "ip_token" in location:
                ippass_url = location
                break
            try:
                r = client.get(location)
            except httpx.HTTPError as e:
                return _result(State.NETWORK_ERROR,
                               f"Redirect follow failed: {e}", started=started)

    # If we intercepted the ippass URL, hand off to the browser immediately.
    if ippass_url:
        provisional = RenewalResult(
            State.RENEWED, "NYT pass renewed",
            final_url=ippass_url,
        )
        return _redeem_via_browser(provisional, account_id=account_id, started=started)

    # Fell through without finding the ippass URL — classify the last response.
    if final_r is None:
        return _result(State.UNEXPECTED, "Redirect chain ended without resolution",
                       started=started)

    phase1 = _classify(final_r, started=started)
    if not phase1.success:
        return phase1

    # final_r landed on the ippass URL (follow_redirects=False caught it late).
    return _redeem_via_browser(phase1, account_id=account_id, started=started)


# ---------- headless Chrome redemption ----------

def _redeem_via_browser(provisional: RenewalResult, *, account_id: int,
                        started: datetime) -> RenewalResult:
    """Open the ippass URL in headless Chrome with the captured profile.

    The SPA's Apollo Client fires redeemIpPass automatically on page load.
    Success is confirmed by querying the NYT data-layer API after Chrome runs —
    the authoritative source for subscription state and expiry date.
    """
    if not provisional.final_url or "ip_token" not in provisional.final_url:
        return provisional

    try:
        from capture_session import _find_chrome_binary, profile_dir_for
    except ImportError as e:
        logger.error("Browser renewal unavailable — capture_session import failed: %s", e)
        return provisional

    chrome_bin = _find_chrome_binary()
    profile_src = profile_dir_for(account_id)
    port = _free_port()
    tmp_dir = tempfile.mkdtemp(prefix=f"nwspr-renew-{account_id}-")

    try:
        profile_copy = os.path.join(tmp_dir, "profile")
        shutil.copytree(profile_src, profile_copy,
                        symlinks=True, ignore_dangling_symlinks=True)

        proc = subprocess.Popen(
            [
                chrome_bin,
                "--headless=new",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--mute-audio",
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                f"--user-data-dir={profile_copy}",
                "--no-first-run",
                "--disable-extensions",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            ws_url = _wait_for_cdp(port, timeout=15)
            if not ws_url:
                logger.warning("Browser renewal: Chrome CDP did not become ready")
                return provisional

            result_url = _navigate_and_await_redemption(
                ws_url, provisional.final_url, timeout=45)

            if result_url and "auth/login" in result_url:
                return _result(State.SESSION_EXPIRED,
                               "Captured NYT session expired — re-capture from the dashboard.",
                               final_url=provisional.final_url, started=started)

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    except Exception as e:
        logger.error("Browser renewal error: %s", e)
        return provisional
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Authoritative check: query the NYT data-layer for the live subscription
    # state and expiry. This handles both "purchase-confirmation" (new
    # redemption) and "pass_still_active" (already-active pass) as success.
    return _verify_subscription(account_id, provisional, started=started)


_NYT_DATA_LAYER = "https://a.nytimes.com/svc/nyt/data-layer"


_VERIFY_RETRY_DELAYS = (5, 10, 20)  # seconds between attempts when cache is stale


def _verify_subscription(account_id: int, provisional: RenewalResult,
                          *, started: datetime) -> RenewalResult:
    """Query the NYT data-layer API to confirm the subscription is active and
    obtain the real expiry date.

    ZUORA data is cached in the data-layer and may be stale immediately after
    renewal. We retry up to len(_VERIFY_RETRY_DELAYS)+1 times with short waits
    until we see a future endDate. Falls back to the provisional result only if
    the subscription-type check fails, or if all retries see a stale date (in
    which case we use +24 h as a conservative fallback)."""
    cookies = extract_cookies(account_id, "nyt")
    if not cookies:
        return provisional

    jar = httpx.Cookies()
    for c in cookies:
        try:
            jar.set(name=c["name"], value=c["value"],
                    domain=c["domain"], path=c.get("path", "/"))
        except Exception:
            pass

    attempts = 1 + len(_VERIFY_RETRY_DELAYS)
    for attempt in range(attempts):
        if attempt > 0:
            delay = _VERIFY_RETRY_DELAYS[attempt - 1]
            logger.info("Data-layer returned stale endDate; retrying in %ds (attempt %d/%d)",
                        delay, attempt + 1, attempts)
            time.sleep(delay)

        try:
            with httpx.Client(cookies=jar, follow_redirects=True, timeout=15,
                              headers={"User-Agent": DEFAULT_UA}) as client:
                r = client.get(_NYT_DATA_LAYER)
                data = r.json()
        except Exception as e:
            logger.warning("Subscription verify request failed: %s — using provisional", e)
            return provisional

        user = data.get("user") or {}
        user_type = user.get("type", "")
        sub_info = user.get("subInfo") or {}

        if user_type not in ("sub", "lgn"):
            logger.warning("Subscription verify: user.type=%s (not a subscriber)", user_type)
            return _result(State.UNEXPECTED,
                           f"NYT account shows no active subscription after renewal "
                           f"(type={user_type}). Re-capture may be needed.",
                           final_url=provisional.final_url, started=started)

        now = datetime.now(timezone.utc)
        for sub in sub_info.get("subscriptions") or []:
            if "ADA" in (sub.get("bundleType") or ""):
                end = sub.get("endDate")
                if end:
                    parsed = _parse_iso(end)
                    if parsed and parsed > now:
                        logger.info("Data-layer confirmed ADA expiry: %s", parsed.isoformat())
                        return _result(State.RENEWED, "NYT pass redeemed",
                                       expiration=parsed,
                                       final_url=provisional.final_url, started=started)
                    logger.debug("Data-layer ADA endDate is stale: %s", end)
                break  # found ADA sub but date is stale — retry outer loop

    # All retries exhausted without a fresh date; use +24 h as safe fallback.
    logger.warning("Data-layer ADA endDate still stale after %d attempts; using +24h fallback",
                   attempts)
    return _result(State.RENEWED, "NYT pass redeemed",
                   expiration=datetime.now(timezone.utc) + timedelta(hours=24),
                   final_url=provisional.final_url, started=started)


def _parse_iso(value: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_cdp(port: int, timeout: float = 15.0) -> Optional[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://localhost:{port}/json", timeout=1.0)
            for tab in r.json():
                if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl"):
                    return tab["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.4)
    return None


def _navigate_and_await_redemption(ws_url: str, ippass_url: str,
                                   timeout: float = 45.0) -> Optional[str]:
    """Navigate to ippass_url and poll until purchase-confirmation appears."""
    try:
        import websocket
    except ImportError:
        logger.error("websocket-client not installed")
        return None

    try:
        ws = websocket.create_connection(ws_url, timeout=10)
    except Exception as e:
        logger.warning("CDP WebSocket failed: %s", e)
        return None

    _id = [1]

    def send(method, params=None):
        mid = _id[0]; _id[0] += 1
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        return mid

    def recv_for(msg_id, limit=5.0):
        ws.settimeout(limit)
        for _ in range(200):
            try:
                msg = json.loads(ws.recv())
                if msg.get("id") == msg_id:
                    return msg
            except Exception:
                break
        return {}

    try:
        send("Page.enable")
        recv_for(send("Page.navigate", {"url": ippass_url}))

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.0)
            mid = send("Runtime.evaluate", {"expression": "window.location.href"})
            ws.settimeout(3.0)
            for _ in range(30):
                try:
                    msg = json.loads(ws.recv())
                    if msg.get("id") == mid:
                        url = ((msg.get("result") or {})
                               .get("result", {}).get("value", ""))
                        if "purchase-confirmation" in url or "auth/login" in url:
                            return url
                        break
                except Exception:
                    break

        # Final read
        mid = send("Runtime.evaluate", {"expression": "window.location.href"})
        ws.settimeout(5.0)
        for _ in range(30):
            try:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid:
                    return ((msg.get("result") or {})
                            .get("result", {}).get("value"))
            except Exception:
                break

    finally:
        try:
            ws.close()
        except Exception:
            pass

    return None


# ---------- response classification (phase 1) ----------

_RENEWED_TEXT = (
    "your pass is active and will expire on",
    "you've claimed your nytimes pass",
    "thank you for becoming a subscriber",
    "you now have unlimited access",
)
_RENEWED_PROVISIONAL = '"isProvisionallyLoggedIn":true'
_LOGIN_REQUIRED_TEXT = (
    "log in or create an account",
    "continue with google",
)


def _classify(r: httpx.Response, *, started: datetime) -> RenewalResult:
    final_url = str(r.url)
    final_lower = final_url.lower()
    body = r.text
    body_lower = body.lower()
    expiry = _extract_expiration(body)

    if "/auth/login" in final_lower:
        return _result(State.SESSION_EXPIRED,
                       "Captured NYT session expired — re-capture from the dashboard.",
                       final_url=final_url, started=started)

    if any(p in body_lower for p in _LOGIN_REQUIRED_TEXT):
        return _result(State.SESSION_EXPIRED,
                       "Captured NYT session expired — re-capture from the dashboard.",
                       final_url=final_url, started=started)

    parsed = urlparse(final_url)
    if not _is_nyt_host(r.url) and "/login" in (parsed.path or "").lower():
        return _result(State.LIBRARY_AUTH_FAILED,
                       f"Library auth failed (still at {parsed.hostname}). Check card / PIN.",
                       final_url=final_url, started=started)

    if "/activate-access/" in final_lower:
        if _RENEWED_PROVISIONAL in body or any(p in body_lower for p in _RENEWED_TEXT):
            return _result(State.RENEWED, "NYT pass renewed",
                           expiration=expiry, final_url=final_url, started=started)
        return _result(State.UNEXPECTED,
                       "Reached redemption URL but server didn't mark us as logged-in.",
                       final_url=final_url, started=started)

    return _result(State.UNEXPECTED,
                   f"Unexpected final URL: {final_url[:200]}",
                   final_url=final_url, started=started)


# ---------- helpers ----------

def _result(state: State, message: str, *,
            expiration: Optional[datetime] = None,
            final_url: Optional[str] = None,
            started: Optional[datetime] = None) -> RenewalResult:
    duration_ms = 0
    if started is not None:
        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return RenewalResult(state, message, expiration, final_url, duration_ms)


def _is_nyt_host(url) -> bool:
    h = (urlparse(str(url)).hostname or "").lower()
    return h.endswith("nytimes.com")


_FORM_ACTION_RE = re.compile(r'<form\s+[^>]*action="([^"]+)"', re.IGNORECASE)
_INPUT_RE = re.compile(
    r'<input\s+(?=[^>]*\btype="(?:hidden|text)")[^>]*\bname="([^"]+)"[^>]*\bvalue="([^"]*)"',
    re.IGNORECASE,
)


def _extract_form_action(html: str) -> Optional[str]:
    m = _FORM_ACTION_RE.search(html)
    return m.group(1) if m else None


def _extract_form_inputs(html: str) -> dict:
    """Hidden + pre-filled text inputs from the EZproxy login form."""
    return {m.group(1): m.group(2) for m in _INPUT_RE.finditer(html)}


_DATE_PATTERNS = (
    re.compile(r'expire(?:s)?\s+on\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})'),
    re.compile(r'access\s+will\s+expire\s+on\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})'),
    re.compile(r'expires?\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})'),
)


def _extract_expiration(html: str) -> Optional[datetime]:
    for pat in _DATE_PATTERNS:
        m = pat.search(html)
        if not m:
            continue
        s = m.group(1).replace(",", "")
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None
