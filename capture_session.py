"""Browser-in-browser capture sessions.

Spins up Xvfb + Chrome + x11vnc + websockify so the user can log into a
newspaper inside an embedded noVNC viewer. The Chrome user-data-dir is
persistent per account, so the renewal flow can later launch headless with
the same profile (cookies, localStorage, DataDome trust) that the user
established interactively.
"""

import os
import secrets
import shutil
import subprocess
import threading
import time
from typing import Optional

from paths import DATA_DIR

PROFILES_DIR = os.path.join(DATA_DIR, 'profiles')
os.makedirs(PROFILES_DIR, exist_ok=True)

# Display range and matching VNC/WS port range. One concurrent session for now;
# expanding to N requires walking the range and skipping busy ones.
DISPLAY_NUM = 100
VNC_PORT = 5900 + DISPLAY_NUM   # 6000
WS_PORT = 6100                  # what the user's browser connects to


def _find_chrome_binary() -> str:
    override = os.environ.get('CHROME_BINARY')
    if override and os.path.isfile(override):
        return override
    for path in ['/usr/bin/chromium', '/usr/bin/chromium-browser',
                 '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
                 '/opt/google/chrome/google-chrome']:
        if os.path.isfile(path):
            return path
    found = shutil.which('chromium') or shutil.which('google-chrome') or shutil.which('chrome')
    if found:
        return found
    raise FileNotFoundError("No Chrome/Chromium binary found")


def profile_dir_for(account_id: int) -> str:
    path = os.path.join(PROFILES_DIR, str(account_id))
    os.makedirs(path, exist_ok=True)
    return path


def profile_exists(account_id: int) -> bool:
    """Profile is "real" once Chrome has written its Default subdir."""
    return os.path.isdir(os.path.join(profile_dir_for(account_id), 'Default'))


def materialize_runtime_profile(account_id: int) -> str:
    """Copy the golden profile to a fresh temp dir for one renewal run.

    Reusing the golden --user-data-dir directly across runs leaks window-state,
    Sessions/, and lock-file weirdness that intermittently lands Chrome in an
    unrenderable viewport. Always launch from a copy; discard after.
    Returns the temp path; caller is responsible for deleting it via
    discard_runtime_profile().
    """
    import tempfile
    src = profile_dir_for(account_id)
    if not os.path.isdir(os.path.join(src, 'Default')):
        return src  # nothing captured; let caller fall through
    dst = tempfile.mkdtemp(prefix=f'newspaparr-runtime-{account_id}-', dir=PROFILES_DIR)

    # Skip Chrome's runtime-only artifacts: Unix sockets (Singleton*), lock
    # files, and crashed-session leftovers. Sockets in particular fail
    # copytree with ENXIO since they aren't regular files.
    skip_names = {
        'SingletonSocket', 'SingletonLock', 'SingletonCookie',
        'lockfile', 'parent.lock', 'Crashpad',
        'Sessions', 'Last Session', 'Last Tabs',
        'Current Session', 'Current Tabs',
    }

    def _ignore(_dir, names):
        return [n for n in names if n in skip_names]

    shutil.copytree(src, dst, dirs_exist_ok=True, ignore_dangling_symlinks=True,
                    ignore=_ignore)
    return dst


def discard_runtime_profile(path: str) -> None:
    """Remove a temp profile created by materialize_runtime_profile."""
    if not path:
        return
    if not os.path.basename(path).startswith('newspaparr-runtime-'):
        # Don't accidentally delete the golden profile if caller passed it
        return
    shutil.rmtree(path, ignore_errors=True)


def sanitize_profile_for_launch(account_id: int) -> None:
    """Suppress Chrome's session-restore prompt before relaunching a profile.

    Chrome stores 'exit_type' in Preferences and a Sessions/ directory with
    last-session state. On relaunch with --user-data-dir, that triggers a
    'restore tabs?' UI that occludes the page and breaks selenium's first
    send_keys with 'element not interactable'. Force a clean state."""
    import json
    profile = profile_dir_for(account_id)
    default = os.path.join(profile, 'Default')
    if not os.path.isdir(default):
        return

    prefs_path = os.path.join(default, 'Preferences')
    if os.path.isfile(prefs_path):
        try:
            with open(prefs_path, 'r') as f:
                prefs = json.load(f)
            prefs.setdefault('profile', {})
            prefs['profile']['exit_type'] = 'Normal'
            prefs['profile']['exited_cleanly'] = True
            with open(prefs_path, 'w') as f:
                json.dump(prefs, f)
        except (json.JSONDecodeError, OSError):
            # Worst case Chrome will rewrite this on next clean exit; not fatal
            pass

    # Drop saved tab/session state so Chrome doesn't try to restore them
    for path in [os.path.join(default, 'Sessions'),
                 os.path.join(default, 'Current Session'),
                 os.path.join(default, 'Current Tabs'),
                 os.path.join(default, 'Last Session'),
                 os.path.join(default, 'Last Tabs')]:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


class CaptureSession:
    def __init__(self, account_id: int, start_url: str):
        self.account_id = account_id
        self.start_url = start_url
        self.token = secrets.token_urlsafe(16)
        self.display = DISPLAY_NUM
        self.vnc_port = VNC_PORT
        self.ws_port = WS_PORT
        self.profile_dir = profile_dir_for(account_id)
        self.xvfb: Optional[subprocess.Popen] = None
        self.chrome: Optional[subprocess.Popen] = None
        self.vnc: Optional[subprocess.Popen] = None
        self.ws: Optional[subprocess.Popen] = None
        self.state = 'pending'
        self.created_at = time.time()

    def start(self):
        env = os.environ.copy()
        env['DISPLAY'] = f':{self.display}'

        self.xvfb = subprocess.Popen(
            ['Xvfb', f':{self.display}', '-screen', '0', '1366x768x24', '-ac'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.7)

        chrome_bin = _find_chrome_binary()
        # Minimal flags: Chrome should look like a real user's Chrome to DataDome.
        # Notably we do NOT force --user-agent (a Windows UA on Linux Chrome is a
        # detectable consistency mismatch) and we do NOT pass
        # --disable-blink-features=AutomationControlled (its presence is itself
        # a bot signal; without selenium/chromedriver navigator.webdriver is
        # already false). --no-sandbox is required when running as root.
        chrome_args = [
            chrome_bin,
            f'--user-data-dir={self.profile_dir}',
            '--disable-dev-shm-usage',
            '--no-default-browser-check',
            '--no-first-run',
            self.start_url,
        ]
        if os.geteuid() == 0:
            chrome_args.insert(2, '--no-sandbox')
        self.chrome = subprocess.Popen(
            chrome_args, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)

        self.vnc = subprocess.Popen([
            'x11vnc', '-display', f':{self.display}',
            '-rfbport', str(self.vnc_port),
            '-nopw', '-forever', '-shared',
            '-quiet', '-noxdamage',
            '-bg', '-o', '/tmp/x11vnc.log',
        ])
        # x11vnc with -bg daemonizes; give it a moment to bind
        time.sleep(0.6)

        # websockify lives in the venv. Bind to 0.0.0.0 so the user's browser
        # can reach it; it's idempotent if a previous session leaked one on
        # the same port — but we track and stop ours explicitly.
        venv_bin = os.path.join(os.path.dirname(__file__), '.venv', 'bin', 'websockify')
        ws_cmd = venv_bin if os.path.isfile(venv_bin) else 'websockify'
        self.ws = subprocess.Popen([
            ws_cmd, f'0.0.0.0:{self.ws_port}', f'localhost:{self.vnc_port}',
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.4)

        self.state = 'active'

    def stop(self):
        # Close Chrome cleanly so the profile gets flushed to disk.
        if self.chrome and self.chrome.poll() is None:
            self.chrome.terminate()
            try:
                self.chrome.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.chrome.kill()
        for proc_name in ('ws', 'vnc'):
            proc = getattr(self, proc_name)
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        # x11vnc with -bg detaches; track-by-pid is fragile, so fall back to pkill
        subprocess.run(['pkill', '-f', f'x11vnc.*:{self.display}'],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self.xvfb and self.xvfb.poll() is None:
            self.xvfb.terminate()
            try:
                self.xvfb.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.xvfb.kill()
        self.state = 'stopped'


class CaptureSessionManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst.sessions = {}
                cls._instance = inst
        return cls._instance

    def start(self, account_id: int, start_url: str) -> CaptureSession:
        with self._lock:
            for token, s in list(self.sessions.items()):
                if s.state == 'active':
                    s.stop()
                    del self.sessions[token]
            session = CaptureSession(account_id, start_url)
        session.start()
        self.sessions[session.token] = session
        return session

    def finish(self, token: str) -> Optional[CaptureSession]:
        with self._lock:
            session = self.sessions.pop(token, None)
        if session:
            session.stop()
        return session

    def get(self, token: str) -> Optional[CaptureSession]:
        return self.sessions.get(token)
