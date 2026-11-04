"""Renewal-related helpers shared across routes and the scheduler.

Lives in its own module so app.py is just routes + factory. The two
public functions:

  utcnow()           — naive UTC datetime (replaces deprecated datetime.utcnow)
  execute_renewal()  — run one renewal, write the log, schedule next, notify
"""
from datetime import datetime, timezone

import logging

import cadence
import egress
import notify
import renewal_progress
from extensions import db
from models import Account, LibraryConfig, RenewalLog
from renewer import renew
from scheduler import schedule_account_renewal


logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Naive UTC datetime, matching the DB column type."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def format_duration(ms: int) -> str:
    """Milliseconds → '48.6s' or '1m 12s' — for logs and UI messages."""
    seconds = (ms or 0) / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


def _lane_usage() -> dict:
    usage = {}
    for (host,) in db.session.query(Account.egress_lane).filter(Account.egress_lane.isnot(None)):
        usage[host] = usage.get(host, 0) + 1
    return usage


def account_lane(account, *, for_capture: bool = False, rotate: bool = False):
    """The egress lane this account's traffic leaves through, or None when no lane is configured
    and direct egress is allowed.

    The pin (Account.egress_lane) is chosen the first time the account needs one and kept so the
    captured NYT trust and every renewal share one exit IP. A pin whose lane has disappeared from
    the configuration is re-chosen at capture time (a capture mints fresh trust anyway) but is an
    error for a renewal: going direct would expose the host's IP, and a different lane would not
    carry the captured trust. rotate=True forces a new lane (capture flow only).

    Raises egress.EgressError when the request must not go direct and no lane is usable."""
    lanes = egress.load_lanes()
    if not lanes:
        if egress.required():
            raise egress.EgressError(
                "No proxy lane is configured and PROXY_REQUIRED is on; refusing to use the host's own IP. "
                "Add lanes to PROXY_URLS or the proxies file.")
        return None

    current = egress.lane_for(account.egress_lane, lanes)
    if current is not None and not rotate:
        return current

    if account.egress_lane and current is None and not for_capture:
        raise egress.EgressError(
            f"Lane {account.egress_lane} is no longer configured. Restore it, or save cookies for "
            "this account again to move it to another lane.")

    chosen = egress.choose_lane(_lane_usage(), lanes, avoid=account.egress_lane if rotate else None)
    if chosen is None:
        raise egress.EgressError("No proxy lane is usable.")
    logger.info("egress: account %s pinned to lane %s%s", account.id, chosen.host,
                " (rotated)" if rotate else "")
    account.egress_lane = chosen.host
    db.session.commit()
    return chosen


def _record_renewal_log(account, *, success, message, duration_ms,
                        result_url=None, expiration=None):
    """Append one RenewalLog row for this attempt."""
    log = RenewalLog(
        account_id=account.id,
        success=success,
        message=message,
        duration_seconds=int((duration_ms or 0) / 1000),
        result_url=result_url,
        expiration=expiration.replace(tzinfo=None) if expiration else None,
    )
    db.session.add(log)
    db.session.commit()


def execute_renewal(account):
    """Run one renewal end-to-end: HTTP call → write log → reschedule →
    fire notifications on state transitions.

    Returns the renewer.RenewalResult, or None if the library is missing
    its NYT URL config (in which case we still record the failure)."""
    renewal_progress.begin(account.id, account.name)
    library = LibraryConfig.query.filter_by(type=account.library_type).first()
    if library is None or not library.nyt_url:
        msg = "No library configuration / NYT URL for this account."
        _record_renewal_log(account, success=False, message=msg, duration_ms=0)
        renewal_progress.finish(account.id, False, msg)
        notify.notify_renewal_failed(account.name, msg)
        return None

    # Detect transitions (failed → ok, ok → failed) so notifications only
    # fire on state changes — not on every successful renewal.
    previous = (RenewalLog.query.filter_by(account_id=account.id)
                .order_by(RenewalLog.id.desc()).first())
    was_failing = previous is not None and not previous.success

    try:
        lane = account_lane(account)
    except egress.EgressError as e:
        msg = f"Egress blocked: {e}"
        _record_renewal_log(account, success=False, message=msg, duration_ms=0)
        account.next_renewal = cadence.after_interval(account.effective_renewal_interval)
        db.session.commit()
        schedule_account_renewal(account)
        renewal_progress.finish(account.id, False, msg)
        notify.notify_renewal_failed(account.name, msg)
        return None

    try:
        result = renew(
            library_url=library.nyt_url,
            library_user=account.library_username,
            library_pass=account.library_password,
            account_id=account.id,
            proxy=lane.url if lane else None,
            egress_label=lane.host if lane else None,
        )
    except Exception as e:
        renewal_progress.finish(account.id, False, f"Renewal crashed: {e}")
        raise

    _record_renewal_log(
        account,
        success=result.success, message=result.message,
        duration_ms=result.duration_ms, result_url=result.final_url,
        expiration=result.expiration,
    )

    account.last_renewal = utcnow()
    if result.success and result.expiration:
        # A pass only redeems once the previous one has lapsed, so the next run is
        # the expiry plus a random delay (see cadence.py), never a fixed +1 minute.
        # next_run() normalises an aware expiry to the naive UTC the column expects.
        account.next_renewal = cadence.next_run(result.expiration)
    else:
        # Cover both success-without-expiration and any failure — failed
        # renewals retry on the same (jittered) cadence as successful ones.
        account.next_renewal = cadence.after_interval(account.effective_renewal_interval)
    db.session.commit()
    schedule_account_renewal(account)
    finish_msg = result.message
    if result.duration_ms:
        finish_msg = f"{result.message} ({format_duration(result.duration_ms)})"
    renewal_progress.finish(account.id, result.success, finish_msg)

    if not result.success:
        notify.notify_renewal_failed(account.name, result.message)
    elif was_failing:
        notify.notify_renewal_recovered(account.name)

    return result
