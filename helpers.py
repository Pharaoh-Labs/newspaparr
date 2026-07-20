"""Renewal-related helpers shared across routes and the scheduler.

Lives in its own module so app.py is just routes + factory. The two
public functions:

  utcnow()           — naive UTC datetime (replaces deprecated datetime.utcnow)
  execute_renewal()  — run one renewal, write the log, schedule next, notify
"""
from datetime import datetime, timedelta, timezone

import notify
import renewal_progress
from extensions import db
from models import LibraryConfig, RenewalLog
from renewer import renew
from scheduler import schedule_account_renewal


def utcnow() -> datetime:
    """Naive UTC datetime, matching the DB column type."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def format_duration(ms: int) -> str:
    """Milliseconds → '48.6s' or '1m 12s' — for logs and UI messages."""
    seconds = (ms or 0) / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


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
        result = renew(
            library_url=library.nyt_url,
            library_user=account.library_username,
            library_pass=account.library_password,
            account_id=account.id,
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
        # Normalize to naive UTC — the DB column and every consumer
        # (min() across accounts, utcnow() comparisons) expect naive.
        expiration = result.expiration
        if expiration.tzinfo is not None:
            expiration = expiration.astimezone(timezone.utc).replace(tzinfo=None)
        account.next_renewal = expiration + timedelta(minutes=1)
    else:
        # Cover both success-without-expiration and any failure — failed
        # renewals retry on the same cadence as successful ones.
        account.next_renewal = (utcnow()
                                + timedelta(hours=account.effective_renewal_interval, minutes=1))
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
