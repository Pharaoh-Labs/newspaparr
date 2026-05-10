"""
Gunicorn configuration for Newspaparr
"""
import multiprocessing
import os

# Server socket
bind = "0.0.0.0:1851"
backlog = 2048

# Worker processes
workers = 1  # Reduced to avoid permission issues
worker_class = "gthread"  # Use gthread for better request handling
worker_connections = 1000
threads = 2  # Allow multiple threads per worker
timeout = 600  # 10 minutes to allow for CAPTCHA solving
keepalive = 2

# Restart workers after this many requests, to help limit memory usage
max_requests = 1000
max_requests_jitter = 50

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'

# Process naming
proc_name = 'newspaparr'

# Server mechanics
daemon = False
_data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
pidfile = os.environ.get('GUNICORN_PIDFILE', os.path.join(_data_dir, 'newspaparr.pid'))
user = None
group = None
tmp_upload_dir = None
worker_tmp_dir = None

# SSL (if needed)
keyfile = None
certfile = None

# Environment
raw_env = [
    'FLASK_APP=wsgi:app',
]

# Application settings
preload_app = False  # Changed to False to avoid connection issues
reload = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'