"""Renewal-related helpers shared across routes and the scheduler.

Lives in its own module so app.py is just routes + factory. The two
public functions:

  utcnow()           — naive UTC datetime (replaces deprecated datetime.utcnow)
  execute_renewal()  — run one renewal, write the log, schedule next, notify
"""
from datetime import datetime, timedelta, timezone

import notify
from extensions import db
from models import LibraryConfig, RenewalLog
from renewer import renew
from scheduler import schedule_account_renewal


def utcnow() -> datetime:
    """Naive UTC datetime, matching the DB column type."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _record_renewal_log(account, *, success, message, duration_ms, result_url=None):
    """Append one RenewalLog row for this attempt."""
    log = RenewalLog(
        account_id=account.id,
        success=success,
        message=message,
        duration_seconds=int((duration_ms or 0) / 1000),
        result_url=result_url,
    )
    db.session.add(log)
    db.session.commit()


def execute_renewal(account):
    """Run one renewal end-to-end: HTTP call → write log → reschedule →
    fire notifications on state transitions.

    Returns the renewer.RenewalResult, or None if the library is missing
    its NYT URL config (in which case we still record the failure)."""
    library = LibraryConfig.query.filter_by(type=account.library_type).first()
    if library is None or not library.nyt_url:
        msg = "No library configuration / NYT URL for this account."
        _record_renewal_log(account, success=False, message=msg, duration_ms=0)
        notify.notify_renewal_failed(account.name, msg)
        return None

    # Detect transitions (failed → ok, ok → failed) so notifications only
    # fire on state changes — not on every successful renewal.
    previous = (RenewalLog.query.filter_by(account_id=account.id)
                .order_by(RenewalLog.id.desc()).first())
    was_failing = previous is not None and not previous.success

    result = renew(
        library_url=library.nyt_url,
        library_user=account.library_username,
        library_pass=account.library_password,
        account_id=account.id,
    )

    _record_renewal_log(
        account,
        success=result.success, message=result.message,
        duration_ms=result.duration_ms, result_url=result.final_url,
    )

    account.last_renewal = utcnow()
    if result.success and result.expiration:
        account.next_renewal = result.expiration + timedelta(minutes=1)
    else:
        # Cover both success-without-expiration and any failure — failed
        # renewals retry on the same cadence as successful ones.
        account.next_renewal = (utcnow()
                                + timedelta(hours=account.effective_renewal_interval, minutes=1))
    db.session.commit()
    schedule_account_renewal(account)

    if not result.success:
        notify.notify_renewal_failed(account.name, result.message)
    elif was_failing:
        notify.notify_renewal_recovered(account.name)

    return result
