"""APScheduler integration: one BackgroundScheduler shared across the app,
plus the schedule/cancel/run logic for per-account renewal jobs.

Initialized once at startup via init(app, execute_renewal). Routes call
schedule_account_renewal() after creating or updating an account; the
scheduler itself fires _run_account_renewal() at the scheduled time.
"""
import atexit
import logging
from datetime import timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from extensions import db
from models import Account

logger = logging.getLogger(__name__)

# Module-level state populated by init(). The Flask app and the renewal
# callback both come from app.py, but we don't import app.py to avoid a
# circular dependency.
_app = None
_execute_renewal = None
_scheduler: BackgroundScheduler | None = None


def job_count() -> int:
    """Number of scheduled jobs. Returns 0 if init() hasn't been called yet
    (e.g., during early boot or in tests)."""
    return len(_scheduler.get_jobs()) if _scheduler else 0


def is_running() -> bool:
    """True if the scheduler has been initialized and is running."""
    return _scheduler is not None and _scheduler.running


def cancel_account_renewal(account_id: int) -> None:
    """Remove any scheduled renewal job for this account. Silent if there
    is none — used after edits and on account deletion."""
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(f'renewal_{account_id}')
    except Exception:
        pass


def init(app, execute_renewal_fn) -> None:
    """Bind this module to the Flask app and the renewal entry point.

    Idempotent — repeated calls don't double-start the scheduler. Called
    once at app startup before any schedule_* calls."""
    global _app, _execute_renewal, _scheduler
    _app = app
    _execute_renewal = execute_renewal_fn
    if _scheduler is None:
        _scheduler = BackgroundScheduler()
        _scheduler.start()
        atexit.register(lambda: _scheduler and _scheduler.shutdown())


def _utcnow_naive():
    """Naive UTC. Local copy to avoid importing from app.py."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


def schedule_account_renewal(account) -> None:
    """Schedule (or reschedule) the next renewal job for an account.

    If account.next_renewal is set, fire at that exact time. Otherwise
    fall back to interval-based firing every effective_renewal_interval
    hours + 1 minute (the +1m avoids racing the pass expiration)."""
    if not account.active:
        return
    if _scheduler is None:
        raise RuntimeError("scheduler.init() was not called before schedule_account_renewal()")

    job_id = f'renewal_{account.id}'
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass  # job didn't exist, fine

    if account.next_renewal:
        # Stored as naive UTC; APScheduler wants tz-aware
        next_run = (account.next_renewal if account.next_renewal.tzinfo
                    else pytz.UTC.localize(account.next_renewal))
        _scheduler.add_job(
            func=_run_account_renewal,
            trigger=DateTrigger(run_date=next_run),
            id=job_id, args=[account.id], replace_existing=True,
        )
        logger.info(f"📅 Scheduled renewal for {account.name} at {next_run}")
    else:
        _scheduler.add_job(
            func=_run_account_renewal,
            trigger=IntervalTrigger(
                hours=account.effective_renewal_interval, minutes=1,
            ),
            id=job_id, args=[account.id], replace_existing=True,
        )
        logger.info(
            f"⏰ Scheduled renewal for {account.name} "
            f"every {account.effective_renewal_interval}h"
        )


def _run_account_renewal(account_id: int) -> None:
    """Scheduler callback. Runs one renewal attempt and always reschedules
    afterward — failures must keep the schedule rolling, not strand the
    account in a never-retried state."""
    with _app.app_context():
        account = db.session.get(Account, account_id)
        if not account or not account.active:
            return
        try:
            result = _execute_renewal(account)
            if result is None:
                logger.warning(
                    f"Scheduled renewal skipped for {account.name} — library config missing.")
            elif result.success:
                logger.info(
                    f"Scheduled renewal succeeded for {account.name} ({result.duration_ms}ms)")
            else:
                logger.warning(
                    f"Scheduled renewal failed for {account.name}: {result.message}")
        except Exception as e:  # noqa: BLE001 — keep the loop alive
            logger.error(f"Scheduled renewal crashed for {account.name}: {e}")
            try:
                account.next_renewal = _utcnow_naive() + timedelta(
                    hours=account.effective_renewal_interval, minutes=1,
                )
                db.session.commit()
                logger.info(
                    f"⏰ Scheduled retry for {account.name} via interval despite crash")
            except Exception as commit_error:
                logger.error(f"Failed to update next_renewal after crash: {commit_error}")
        finally:
            try:
                schedule_account_renewal(account)
            except Exception as e:
                logger.error(f"Failed to reschedule {account.name}: {e}")
