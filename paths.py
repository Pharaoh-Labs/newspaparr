"""Project paths. Resolves data dirs relative to the project root by default,
overridable via NEWSPAPARR_DATA_DIR. Works both in-container (project at /app)
and in a venv on the host."""

import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('NEWSPAPARR_DATA_DIR', os.path.join(PROJECT_ROOT, 'data'))
LOGS_DIR = os.path.join(DATA_DIR, 'logs')
SCREENSHOTS_DIR = os.path.join(DATA_DIR, 'debug', 'screenshots')
DEFAULT_DB_URL = f'sqlite:///{os.path.join(DATA_DIR, "newspaparr.db")}'
