"""When the next renewal runs. Humans do not renew a pass 60 seconds after it lapses, every day,
to the second; a fixed cadence from one IP is a bot signal in its own right. So the next run is
the earliest moment it is useful (a pass only redeems once the previous one has lapsed) plus a
random delay, optionally pushed out of a nightly quiet window.

  RENEWAL_DELAY_MIN_MINUTES   lower bound of the delay after expiry (default 5)
  RENEWAL_DELAY_MAX_MINUTES   upper bound (default 120); set both equal for a fixed offset
  RENEWAL_QUIET_HOURS         "HH:MM-HH:MM" in TZ during which nothing fires (default: none);
                              a run that lands inside is moved to a random point in the first
                              RENEWAL_DELAY_MAX_MINUTES after the window ends

Manual "Renew now" is unaffected: it runs immediately.
"""

from __future__ import annotations

import logging
import os
import random
from datetime import datetime, time, timedelta, timezone
from typing import Optional, Tuple

import pytz

logger = logging.getLogger(__name__)

DEFAULT_DELAY_MIN = 5
DEFAULT_DELAY_MAX = 120


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("cadence: %s=%r is not a number; using %d", name, raw, default)
        return default


def delay_bounds() -> Tuple[int, int]:
    lo = _int_env('RENEWAL_DELAY_MIN_MINUTES', DEFAULT_DELAY_MIN)
    hi = _int_env('RENEWAL_DELAY_MAX_MINUTES', DEFAULT_DELAY_MAX)
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def quiet_hours() -> Optional[Tuple[time, time]]:
    """(start, end) local times, or None. A window may wrap midnight ("23:00-06:30")."""
    raw = (os.environ.get('RENEWAL_QUIET_HOURS') or '').strip()
    if not raw:
        return None
    try:
        a, b = raw.split('-', 1)
        start = time.fromisoformat(a.strip())
        end = time.fromisoformat(b.strip())
    except ValueError:
        logger.warning("cadence: RENEWAL_QUIET_HOURS=%r is not HH:MM-HH:MM; ignoring", raw)
        return None
    if start == end:
        return None
    return start, end


def local_tz():
    name = os.environ.get('TZ', 'UTC')
    try:
        return pytz.timezone(name)
    except pytz.UnknownTimeZoneError:
        return pytz.UTC


def _in_window(local: datetime, window: Tuple[time, time]) -> bool:
    start, end = window
    t = local.time()
    if start < end:
        return start <= t < end
    return t >= start or t < end          # wraps midnight


def _window_end_after(local: datetime, window: Tuple[time, time]) -> datetime:
    """The next moment the quiet window ends, given `local` is inside it."""
    _, end = window
    candidate = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


def next_run(earliest: datetime, *, now: Optional[datetime] = None,
             rng: random.Random = random) -> datetime:
    """Next renewal time (naive UTC): `earliest` (naive or aware UTC; the moment the renewal
    becomes useful, e.g. the pass expiry) plus a random delay within the configured bounds,
    moved out of the quiet window if it lands inside. Never earlier than `now`."""
    if earliest.tzinfo is not None:
        earliest = earliest.astimezone(timezone.utc).replace(tzinfo=None)
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    base = max(earliest, now)

    lo, hi = delay_bounds()
    when = base + timedelta(minutes=rng.uniform(lo, hi))

    window = quiet_hours()
    if window is not None:
        tz = local_tz()
        local = pytz.UTC.localize(when).astimezone(tz)
        if _in_window(local, window):
            end_local = tz.localize(_window_end_after(local.replace(tzinfo=None), window))
            when = end_local.astimezone(pytz.UTC).replace(tzinfo=None) + timedelta(minutes=rng.uniform(0, hi))
    return when.replace(microsecond=0)


def after_interval(hours: float, *, now: Optional[datetime] = None,
                   rng: random.Random = random) -> datetime:
    """Next run when no expiry is known: `hours` from now, jittered like any other run."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return next_run(now + timedelta(hours=hours), now=now, rng=rng)


def describe() -> dict:
    lo, hi = delay_bounds()
    window = quiet_hours()
    return {
        'delay_minutes': [lo, hi],
        'quiet_hours': f"{window[0].strftime('%H:%M')}-{window[1].strftime('%H:%M')}" if window else None,
        'timezone': str(local_tz()),
    }
