"""In-memory, thread-safe progress store for renewal runs.

The UI polls /accounts/<id>/renew/status while a renewal runs in a
background thread (manual trigger) or in the APScheduler thread
(scheduled trigger). Both paths funnel through helpers.execute_renewal,
which calls begin()/finish(); renewer.py reports phase changes via
update(). Single-process only — matches the single-worker gunicorn
deployment.
"""
import threading
import time
from typing import Optional

_lock = threading.Lock()
_runs: dict[int, dict] = {}  # account_id -> status dict


def begin(account_id: int, name: str) -> None:
    with _lock:
        _runs[account_id] = {
            'account_id': account_id,
            'name': name,
            'running': True,
            'success': None,
            'message': 'Starting renewal…',
            'started_at': time.time(),
            'finished_at': None,
        }


def update(account_id: int, message: str) -> None:
    """Report a phase change. No-op if begin() wasn't called (e.g. tests)."""
    with _lock:
        run = _runs.get(account_id)
        if run and run['running']:
            run['message'] = message


def finish(account_id: int, success: bool, message: str) -> None:
    with _lock:
        run = _runs.get(account_id)
        if not run:
            return
        run.update(running=False, success=bool(success), message=message,
                   finished_at=time.time())


def get(account_id: int) -> Optional[dict]:
    with _lock:
        run = _runs.get(account_id)
        return dict(run) if run else None


def is_running(account_id: int) -> bool:
    with _lock:
        run = _runs.get(account_id)
        return bool(run and run['running'])


def active() -> list[dict]:
    """All currently-running renewals (for page-load pickup)."""
    with _lock:
        return [dict(r) for r in _runs.values() if r['running']]
