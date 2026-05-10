"""HTTP-only renewer.

The whole renewal is a redirect chain: library auth (EZproxy POST) →
ezproxy-rewritten NYT URL → NYT /activate-access/ippass with the ip_token in
the query string. We carry the user's captured NYT cookies in the session,
so the ippass page recognizes us as already-logged-in and the redemption
completes server-side without ever rendering JS, never touching DataDome's
tags.js, never spinning up a browser.

Replaces the old browser-driven renewal_engine / library_adapters /
enhanced_browser / state_detector pipeline (~1500 lines).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from cookie_jar import extract_cookies

logger = logging.getLogger(__name__)


# Linux Chrome UA — matches what the capture browser presents so the captured
# DataDome trust cookie isn't fingerprint-mismatched against an inconsistent
# UA at the HTTP layer.
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


class State(str, Enum):
    """Renewal outcome states. Only one is "success" (RENEWED) — everything
    else is a flavor of failure with an actionable message."""
    RENEWED = "renewed"                  # ✅ pass redeemed
    NO_SESSION = "no_session"            # need to capture first
    SESSION_EXPIRED = "session_expired"  # captured cookies stale; re-capture
    LIBRARY_AUTH_FAILED = "library_auth_failed"  # bad library card / PIN
    NETWORK_ERROR = "network_error"      # transient
    UNEXPECTED = "unexpected"            # anything we can't classify


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

    The account must have a captured Chrome session (cookies in
    data/profiles/<account_id>/Default/Cookies). Returns a RenewalResult.
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
        except Exception as e:  # bad cookie shape — just skip
            logger.debug("Skipping cookie %s: %s", c.get("name"), e)

    started = datetime.now()
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    with httpx.Client(cookies=jar, follow_redirects=True, timeout=timeout,
                      headers=headers) as client:
        # Step 1: GET the library auth URL.
        try:
            r = client.get(library_url)
        except httpx.HTTPError as e:
            return _result(State.NETWORK_ERROR,
                           f"Library URL fetch failed: {e}", started=started)

        # If we somehow ended up at a NYT URL already (library cookies still
        # valid), we can skip auth.
        if not _is_nyt_host(r.url):
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

        # Step 2: classify the final response.
        return _classify(r, started=started)


# ---------- response classification ----------

# Phrases NYT renders when access is granted (SPA may swap these post-JS, but
# they appear in the initial HTML on some endpoints).
_RENEWED_TEXT = (
    "your pass is active and will expire on",
    "you've claimed your nytimes pass",
    "thank you for becoming a subscriber",
    "you now have unlimited access",
)

# Server-side flag embedded in the activate-access SPA payload. Present iff
# the server treated us as logged-in when rendering the page.
_RENEWED_PROVISIONAL = '"isProvisionallyLoggedIn":true'

# Phrases on NYT's email-only login page (where we land if cookies are stale).
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

    # Hard NYT login wall — server-side bounce means our cookies didn't
    # authenticate us. Re-capture needed.
    if "/auth/login" in final_lower:
        return _result(State.SESSION_EXPIRED,
                       "Captured NYT session expired — re-capture from the dashboard.",
                       final_url=final_url, started=started)

    if any(p in body_lower for p in _LOGIN_REQUIRED_TEXT):
        return _result(State.SESSION_EXPIRED,
                       "Captured NYT session expired — re-capture from the dashboard.",
                       final_url=final_url, started=started)

    # Library refused us (we never reached NYT).
    parsed = urlparse(final_url)
    if not _is_nyt_host(r.url) and "/login" in (parsed.path or "").lower():
        return _result(State.LIBRARY_AUTH_FAILED,
                       f"Library auth failed (still at {parsed.hostname}). Check card / PIN.",
                       final_url=final_url, started=started)

    # NYT's pass-redemption SPA. Two parallel positive signals:
    # - explicit success copy (when present in initial HTML)
    # - the server-side isProvisionallyLoggedIn flag
    if "/activate-access/" in final_lower:
        if _RENEWED_PROVISIONAL in body or any(p in body_lower for p in _RENEWED_TEXT):
            return _result(State.RENEWED, "NYT pass renewed",
                           expiration=expiry, final_url=final_url, started=started)
        # Reached the right URL but no provisional/success markers — probably
        # the redemption was rejected mid-flight (rate limit, something else).
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
        duration_ms = int((datetime.now() - started).total_seconds() * 1000)
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
