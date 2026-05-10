"""Notifications via Apprise.

One env var (APPRISE_URLS, comma-separated) drives everything. Apprise
parses ~80 service URLs natively — discord://, ntfy://, mailto://,
tgram://, slack://, gotify://, custom webhooks, etc. — so users plug in
whatever they already use.

Triggers:
  - renewal failed (one-shot)
  - renewal recovered (after a streak of failures)
  - capture session expiring soon (cron-driven, weekly check; not yet wired)
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


def _client():
    """Return a configured Apprise client, or None if no URLs are set or
    apprise is unavailable. We import lazily so apprise is optional at runtime."""
    urls = (os.environ.get('APPRISE_URLS') or '').strip()
    if not urls:
        return None
    try:
        import apprise  # type: ignore
    except ImportError:
        logger.warning("APPRISE_URLS is set but the apprise package is not installed.")
        return None
    a = apprise.Apprise()
    for url in (u.strip() for u in urls.split(',') if u.strip()):
        if not a.add(url):
            logger.warning("Apprise rejected URL (bad format?): %s", _redact(url))
    return a if len(a) else None


def notify(title: str, body: str, *, tag: Optional[Iterable[str]] = None) -> bool:
    """Send a notification. Returns True on success (or if no notifier
    configured — we don't treat 'no recipient' as failure)."""
    client = _client()
    if client is None:
        return True
    try:
        ok = client.notify(title=title, body=body)
        if not ok:
            logger.warning("Apprise reported partial/total delivery failure for %r", title)
        return bool(ok)
    except Exception as e:
        logger.error("Apprise notify crashed: %s", e)
        return False


def notify_renewal_failed(account_name: str, message: str, attempts: int = 1) -> None:
    """Fire-and-forget: a renewal just failed."""
    suffix = f" ({attempts} attempts)" if attempts > 1 else ""
    notify(
        title=f"Newspaparr: {account_name} renewal failed{suffix}",
        body=message,
    )


def notify_renewal_recovered(account_name: str) -> None:
    """A renewal just succeeded after one or more failures."""
    notify(
        title=f"Newspaparr: {account_name} renewals recovered",
        body=f"Renewal succeeded after a previous failure.",
    )


def _redact(url: str) -> str:
    """Best-effort redact secrets from a URL for logging."""
    import re
    return re.sub(r'(://[^:/@]+):[^@]+@', r'\1:***@', url)
