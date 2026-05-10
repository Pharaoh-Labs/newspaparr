"""CaptureSessionManager lifecycle tests.

The capture flow owns 4 subprocesses (Xvfb, Chromium, x11vnc, websockify).
Without disciplined cleanup, gunicorn --reload during dev leaves orphans —
the bug originally tracked as newspaparr-l06. These tests pin the contract:

  - sweep_orphans() runs at construction (kills leftovers before we start)
  - stop_all() is idempotent (safe to call from atexit *and* signal handlers)
  - Shutdown hooks register exactly once across multiple constructions
"""
import subprocess
from unittest import mock


def test_singleton_construction_runs_boot_sweep():
    """First instantiation must call sweep_orphans(); subsequent calls don't."""
    import capture_session as cs
    cs.CaptureSessionManager._instance = None  # reset for test isolation

    with mock.patch.object(cs, 'sweep_orphans') as swept:
        cs.CaptureSessionManager()
        cs.CaptureSessionManager()  # second call — singleton, no resweep
        cs.CaptureSessionManager()
    assert swept.call_count == 1, f"sweep ran {swept.call_count}x, expected 1"


def test_sweep_orphans_invokes_pkill_on_managed_patterns():
    """The sweep targets exactly our managed display + ports + profile dir,
    not arbitrary Chrome/Xvfb on the host."""
    import capture_session as cs
    with mock.patch.object(subprocess, 'run') as runner:
        runner.return_value = mock.Mock(returncode=1)  # nothing matched
        cs.sweep_orphans()
    invoked = [call.args[0] for call in runner.call_args_list]
    # Each pkill is `['pkill', '-f', '<pattern>']`
    patterns = [args[2] for args in invoked]
    joined = ' '.join(patterns)
    assert f':{cs.DISPLAY_NUM}' in joined, "should target our display num"
    assert str(cs.WS_PORT) in joined, "should target our websockify port"
    assert cs.PROFILES_DIR in joined, "should scope chromium kill to our profiles"


def test_stop_all_is_idempotent():
    """atexit can fire after a SIGTERM handler already cleaned up — calling
    stop_all twice must not crash and must not throw on already-stopped sessions."""
    import capture_session as cs
    cs.CaptureSessionManager._instance = None
    mgr = cs.CaptureSessionManager()
    fake = mock.Mock(spec=cs.CaptureSession)
    fake.token = 'tok-xyz'
    mgr.sessions[fake.token] = fake

    mgr.stop_all()
    mgr.stop_all()  # must not raise

    # The session was stopped exactly once — second call is a no-op
    # because sessions.clear() was the first thing the first call did.
    assert fake.stop.call_count == 1


def test_stop_all_swallows_per_session_failures():
    """One broken session must not block cleanup of the others — if it did,
    a single zombie subprocess would block worker shutdown indefinitely."""
    import capture_session as cs
    cs.CaptureSessionManager._instance = None
    mgr = cs.CaptureSessionManager()
    bad = mock.Mock(spec=cs.CaptureSession, token='bad')
    bad.stop.side_effect = RuntimeError("subprocess wedged")
    good = mock.Mock(spec=cs.CaptureSession, token='good')
    mgr.sessions['bad'] = bad
    mgr.sessions['good'] = good

    mgr.stop_all()

    bad.stop.assert_called_once()
    good.stop.assert_called_once()
