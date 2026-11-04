"""Renewal cadence: jittered delay after expiry, never before it, optional quiet hours."""
import random
from datetime import datetime, timedelta, timezone

import cadence

UTC = timezone.utc


def _naive(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi)


def test_defaults_are_a_window_after_expiry(monkeypatch):
    monkeypatch.delenv('RENEWAL_DELAY_MIN_MINUTES', raising=False)
    monkeypatch.delenv('RENEWAL_DELAY_MAX_MINUTES', raising=False)
    monkeypatch.delenv('RENEWAL_QUIET_HOURS', raising=False)
    expiry = _naive(2026, 11, 5, 15, 0)
    now = _naive(2026, 11, 4, 15, 0)
    runs = {cadence.next_run(expiry, now=now, rng=random.Random(i)) for i in range(200)}
    assert all(expiry + timedelta(minutes=5) <= r <= expiry + timedelta(minutes=120) for r in runs)
    assert len(runs) > 50                                   # actually varies
    assert all(r.microsecond == 0 for r in runs)


def test_never_before_expiry_and_never_in_the_past(monkeypatch):
    monkeypatch.setenv('RENEWAL_DELAY_MIN_MINUTES', '0')
    monkeypatch.setenv('RENEWAL_DELAY_MAX_MINUTES', '0')
    expiry = _naive(2026, 11, 5, 15, 0)
    assert cadence.next_run(expiry, now=_naive(2026, 11, 4, 0, 0)) == expiry
    stale = _naive(2026, 11, 1, 0, 0)                       # data-layer cache handed us a past date
    now = _naive(2026, 11, 4, 12, 30)
    assert cadence.next_run(stale, now=now) == now


def test_aware_inputs_are_normalised_to_naive_utc(monkeypatch):
    monkeypatch.setenv('RENEWAL_DELAY_MIN_MINUTES', '0')
    monkeypatch.setenv('RENEWAL_DELAY_MAX_MINUTES', '0')
    expiry = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
    out = cadence.next_run(expiry, now=datetime(2026, 11, 4, 0, 0, tzinfo=UTC))
    assert out == _naive(2026, 11, 5, 15, 0) and out.tzinfo is None


def test_bounds_swap_and_bad_values_fall_back(monkeypatch):
    monkeypatch.setenv('RENEWAL_DELAY_MIN_MINUTES', '90')
    monkeypatch.setenv('RENEWAL_DELAY_MAX_MINUTES', '30')
    assert cadence.delay_bounds() == (30, 90)
    monkeypatch.setenv('RENEWAL_DELAY_MIN_MINUTES', 'soon')
    assert cadence.delay_bounds()[0] == cadence.DEFAULT_DELAY_MIN


def test_after_interval_is_hours_plus_jitter(monkeypatch):
    monkeypatch.setenv('RENEWAL_DELAY_MIN_MINUTES', '10')
    monkeypatch.setenv('RENEWAL_DELAY_MAX_MINUTES', '20')
    monkeypatch.delenv('RENEWAL_QUIET_HOURS', raising=False)
    now = _naive(2026, 11, 4, 9, 0)
    out = cadence.after_interval(24, now=now, rng=random.Random(1))
    assert now + timedelta(hours=24, minutes=10) <= out <= now + timedelta(hours=24, minutes=20)


def test_quiet_hours_push_the_run_past_the_window(monkeypatch):
    monkeypatch.setenv('TZ', 'America/New_York')
    monkeypatch.setenv('RENEWAL_QUIET_HOURS', '00:30-07:00')
    monkeypatch.setenv('RENEWAL_DELAY_MIN_MINUTES', '0')
    monkeypatch.setenv('RENEWAL_DELAY_MAX_MINUTES', '30')
    # 08:00 UTC on 2026-11-05 is 03:00 EST: inside the window.
    expiry = _naive(2026, 11, 5, 8, 0)
    now = _naive(2026, 11, 4, 8, 0)
    for seed in range(50):
        out = cadence.next_run(expiry, now=now, rng=random.Random(seed))
        # Window ends 07:00 EST = 12:00 UTC; run lands within the next 30 min.
        assert _naive(2026, 11, 5, 12, 0) <= out <= _naive(2026, 11, 5, 12, 30), out


def test_quiet_hours_wrap_midnight_and_leave_daytime_alone(monkeypatch):
    monkeypatch.setenv('TZ', 'UTC')
    monkeypatch.setenv('RENEWAL_QUIET_HOURS', '23:00-06:00')
    monkeypatch.setenv('RENEWAL_DELAY_MIN_MINUTES', '0')
    monkeypatch.setenv('RENEWAL_DELAY_MAX_MINUTES', '0')
    daytime = _naive(2026, 11, 5, 14, 0)
    assert cadence.next_run(daytime, now=_naive(2026, 11, 4, 0, 0)) == daytime
    late = _naive(2026, 11, 5, 23, 30)
    assert cadence.next_run(late, now=_naive(2026, 11, 4, 0, 0)) == _naive(2026, 11, 6, 6, 0)
    early = _naive(2026, 11, 5, 2, 0)
    assert cadence.next_run(early, now=_naive(2026, 11, 4, 0, 0)) == _naive(2026, 11, 5, 6, 0)


def test_quiet_hours_parse_errors_are_ignored(monkeypatch):
    monkeypatch.setenv('RENEWAL_QUIET_HOURS', 'night')
    assert cadence.quiet_hours() is None
    monkeypatch.setenv('RENEWAL_QUIET_HOURS', '02:00-02:00')
    assert cadence.quiet_hours() is None
    monkeypatch.setenv('RENEWAL_QUIET_HOURS', '')
    assert cadence.quiet_hours() is None


def test_describe_matches_env(monkeypatch):
    monkeypatch.setenv('TZ', 'Europe/Berlin')
    monkeypatch.setenv('RENEWAL_QUIET_HOURS', '01:00-06:30')
    monkeypatch.setenv('RENEWAL_DELAY_MIN_MINUTES', '3')
    monkeypatch.setenv('RENEWAL_DELAY_MAX_MINUTES', '45')
    assert cadence.describe() == {'delay_minutes': [3, 45], 'quiet_hours': '01:00-06:30',
                                  'timezone': 'Europe/Berlin'}


def test_execute_renewal_schedules_jittered_after_expiry(app_client, monkeypatch):
    app_mod, _ = app_client
    import helpers
    from models import Account, LibraryConfig

    monkeypatch.setenv('RENEWAL_DELAY_MIN_MINUTES', '15')
    monkeypatch.setenv('RENEWAL_DELAY_MAX_MINUTES', '45')
    monkeypatch.delenv('RENEWAL_QUIET_HOURS', raising=False)
    monkeypatch.delenv('PROXY_URLS', raising=False)
    monkeypatch.setenv('PROXY_REQUIRED', 'false')
    expiry = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=23)

    import renewer
    monkeypatch.setattr(helpers, 'renew', lambda **kw: renewer.RenewalResult(
        renewer.State.RENEWED, 'ok', expiration=expiry, duration_ms=10))
    monkeypatch.setattr(helpers, 'schedule_account_renewal', lambda account: None)
    monkeypatch.setattr(helpers.notify, 'notify_renewal_failed', lambda *a, **k: None)
    monkeypatch.setattr(helpers.notify, 'notify_renewal_recovered', lambda *a, **k: None)

    with app_mod.app.app_context():
        lib = LibraryConfig(name='L', type='cad-lib', nyt_url='https://ezproxy.example/login')
        acct = Account(name='cad', library_type='cad-lib', library_username='u', library_password='p')
        app_mod.db.session.add_all([lib, acct])
        app_mod.db.session.commit()
        try:
            result = helpers.execute_renewal(acct)
            assert result.success
            naive_expiry = expiry.replace(tzinfo=None)
            assert naive_expiry + timedelta(minutes=15) <= acct.next_renewal <= naive_expiry + timedelta(minutes=45)
        finally:
            app_mod.db.session.delete(acct)
            app_mod.db.session.delete(lib)
            app_mod.db.session.commit()
