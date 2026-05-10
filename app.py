"""
Flask web application for NYT Auto-Renewal System
Provides web UI for configuration, monitoring, and management
"""

import atexit
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    """Naive UTC datetime, matching the DB column type. Replaces the
    deprecated `utcnow()` which emits DeprecationWarning on 3.12+."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from werkzeug.middleware.proxy_fix import ProxyFix
from wtforms import (BooleanField, IntegerField, PasswordField, SelectField,
                     StringField, TextAreaField)
from wtforms.validators import DataRequired, NumberRange

from icons import icon
from paths import DATA_DIR, DEFAULT_DB_URL, LOGS_DIR, SCREENSHOTS_DIR
import notify
from renewer import State, renew
from secrets_at_rest import EncryptedString, migrate_plaintext_rows

__version__ = '1.1.0'

startup_time = utcnow()


def _resolve_secret_key() -> str:
    """Get SECRET_KEY from env, or generate-and-persist one for self-hosted
    deployments that didn't bother to set one. Persisting beats regenerating
    each restart (which would invalidate every active session)."""
    val = os.environ.get('SECRET_KEY')
    if val:
        return val
    keyfile = os.path.join(DATA_DIR, 'secret_key')
    if os.path.isfile(keyfile):
        with open(keyfile) as f:
            return f.read().strip()
    new_key = secrets.token_urlsafe(48)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(keyfile, 'w') as f:
        f.write(new_key)
    os.chmod(keyfile, 0o600)
    return new_key


app = Flask(__name__)
app.config['SECRET_KEY'] = _resolve_secret_key()
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', DEFAULT_DB_URL)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['TEMPLATES_AUTO_RELOAD'] = True  # so dev edits to templates reflect without bouncing gunicorn

# Make {{ icon('name') }} available in every template without per-template imports
app.jinja_env.globals['icon'] = icon


# Configure app to work behind proxy (simplified to avoid double-processing)
# Only enable if actually behind a proxy
if os.environ.get('BEHIND_PROXY', 'false').lower() == 'true':
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_prefix=1
    )

# Add JSON filter for templates
import json
@app.template_filter('from_json')
def from_json_filter(value):
    try:
        return json.loads(value) if value else {}
    except:
        return {}

@app.template_filter('library_type_display')
def library_type_display_filter(value):
    """Convert library type codes to user-friendly display names"""
    type_mapping = {
        'generic_oclc': 'OCLC Library',
        'custom': 'Custom Library'
    }
    return type_mapping.get(value, value.replace('_', ' ').title())

# Add datetime context for templates. Templates use `now_utc()` instead of
# `datetime.utcnow()` because the latter is deprecated in 3.12+.
@app.context_processor
def inject_datetime():
    return {'datetime': datetime, 'now_utc': utcnow}

# Add timezone filter for converting UTC to local time
@app.template_filter('strip_status_emoji')
def strip_status_emoji_filter(text):
    """Strip leading status emojis (✅ ❌ ⚠️ 🎯 🔑 🍪 etc.) from a renewal-log
    message. The UI conveys status via colored dots, so the emoji prefix is
    redundant — and the runtime font on some hosts has no emoji glyphs and
    renders them as boxes."""
    if not text:
        return text
    import re
    return re.sub(r'^[☀-➿\U0001F300-\U0001FAFF️\s]+', '', text)


@app.template_filter('localtime')
def localtime_filter(dt):
    """Convert UTC datetime to local timezone"""
    if dt is None:
        return None
    # Get timezone from environment or default to America/New_York
    import pytz
    tz_name = os.environ.get('TZ', 'America/New_York')
    try:
        local_tz = pytz.timezone(tz_name)
        utc_tz = pytz.timezone('UTC')
        # Ensure datetime is timezone-aware
        if dt.tzinfo is None:
            dt = utc_tz.localize(dt)
        return dt.astimezone(local_tz)
    except:
        return dt

# Add version, uptime, and sidebar-badge counts to every template
@app.context_processor
def inject_app_info():
    uptime = utcnow() - startup_time
    hours = int(uptime.total_seconds() // 3600)
    minutes = int((uptime.total_seconds() % 3600) // 60)
    uptime_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
    # Account count powers the sidebar badge; render once for every page,
    # not just the dashboard. Cheap query, runs once per request.
    try:
        account_count = Account.query.count()
    except Exception:
        account_count = 0  # DB not yet initialized (e.g., first migration)
    return {
        'app_version': __version__,
        'app_uptime': uptime_str,
        'account_count': account_count,
    }

# Initialize database
db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Setup logging to both file and console
import logging.handlers
import os

# Only configure logging if not already configured (e.g., when running directly, not via wsgi)
if not logging.getLogger().handlers:
    os.makedirs(LOGS_DIR, exist_ok=True)

    # Configure root logger
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.handlers.RotatingFileHandler(
                os.path.join(LOGS_DIR, 'newspaparr.log'),
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
            ),
        ],
    )
    
    # Quiet down chatty libraries
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info(f"🗂️  Logging initialized — files will be saved to {LOGS_DIR}")
else:
    logger = logging.getLogger(__name__)
    logger.info("📝 Logging already configured, using existing configuration")

# Boot the CaptureSessionManager singleton early so its boot-sweep runs and
# its atexit/SIGTERM hooks register *before* the worker starts handling
# traffic. Without this the sweep is lazy and a gunicorn --reload between
# requests can leave Xvfb/x11vnc/websockify orphans.
from capture_session import CaptureSessionManager as _CSM
_CSM()

# Initialize scheduler (delayed to avoid app context issues)
scheduler = None

def init_scheduler():
    global scheduler
    if scheduler is None:
        scheduler = BackgroundScheduler()
        scheduler.start()
        atexit.register(lambda: scheduler and scheduler.shutdown())
        return True
    return False

# Database Models

class Account(db.Model):
    """Account configuration model"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    library_type = db.Column(db.String(50), nullable=False)
    library_username = db.Column(db.String(100), nullable=False)
    library_password = db.Column(EncryptedString(500), nullable=False)
    username = db.Column(db.String(100), nullable=True)
    password = db.Column(EncryptedString(500), nullable=True)
    newspaper_type = db.Column(db.String(20), nullable=False, default='nyt')
    
    renewal_hours = db.Column(db.Integer, default=24)
    renewal_interval = db.Column(db.Integer, nullable=True)  # Optional override, inherits from library if null
    active = db.Column(db.Boolean, default=True)
    last_renewal = db.Column(db.DateTime)
    next_renewal = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)
    profile_captured_at = db.Column(db.DateTime, nullable=True)
    
    @property
    def display_name(self):
        return f"{self.name} (NYT)"

    @property
    def effective_renewal_interval(self):
        """Get the effective renewal interval - account override or library default"""
        if self.renewal_interval is not None:
            return self.renewal_interval
        
        # Look up library config for default
        library = LibraryConfig.query.filter_by(type=self.library_type, active=True).first()
        if library:
            return library.default_renewal_hours
        
        # Fallback to system default
        return self.renewal_hours or 24

class LibraryConfig(db.Model):
    """Library configuration model"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    homepage = db.Column(db.String(500))
    nyt_url = db.Column(db.String(500))  # Direct NYT access URL
    custom_config = db.Column(db.Text)
    default_renewal_hours = db.Column(db.Integer, default=24)
    active = db.Column(db.Boolean, default=True)

class RenewalLog(db.Model):
    """Renewal log model"""
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=utcnow)
    success = db.Column(db.Boolean, nullable=False)
    message = db.Column(db.Text)
    duration_seconds = db.Column(db.Integer)
    result_url = db.Column(db.String(500))
    screenshot_filename = db.Column(db.String(255))  # Final screenshot filename

# Forms
class AccountForm(FlaskForm):
    """Form for account configuration. NYT credentials are optional —
    cookies captured via the dashboard's noVNC capture flow are the primary
    auth path; credentials only matter as a fallback if cookies expire."""
    name = StringField('Account Name', validators=[DataRequired()])
    library_type = SelectField('Library Type', choices=[])
    library_username = StringField('Library Username/Card Number', validators=[DataRequired()])
    library_password = PasswordField('Library Password/PIN', validators=[DataRequired()])
    renewal_interval = IntegerField('Renewal Interval Override (hours)', validators=[])
    active = BooleanField('Active', default=True)

class EditAccountForm(FlaskForm):
    """Form for editing account configuration."""
    name = StringField('Account Name', validators=[DataRequired()])
    library_type = SelectField('Library Type', choices=[])
    library_username = StringField('Library Username/Card Number', validators=[DataRequired()])
    library_password = PasswordField('Library Password/PIN', validators=[])  # editable but optional
    renewal_interval = IntegerField('Renewal Interval Override (hours)', validators=[])
    active = BooleanField('Active', default=True)

class LibraryForm(FlaskForm):
    """Form for library configuration"""
    name = StringField('Library Name', validators=[DataRequired()])
    type = SelectField('Library Type', choices=[
        ('generic_oclc', 'OCLC Library'),
        ('custom', 'Custom Library')
    ])
    nyt_url = StringField('NYT Access URL', validators=[DataRequired()],
                         description='Direct URL for NYT access through your library')
    homepage = StringField('Library Homepage (optional)', description='Main library website URL for linking')
    default_renewal_hours = IntegerField('Default Renewal Hours',
                                          validators=[NumberRange(min=1, max=168)], default=24,
                                          description='Renewals will run at this interval + 1 minute')
    active = BooleanField('Active', default=True)
    custom_config = TextAreaField('Additional Configuration (JSON)',
                                   description='Optional: JSON configuration for advanced settings')

# Routes
@app.route('/')
def index():
    """Dashboard - main page"""
    accounts = Account.query.all()
    recent_logs = RenewalLog.query.order_by(RenewalLog.timestamp.desc()).limit(10).all()
    
    total_accounts = len(accounts)
    active_accounts = len([a for a in accounts if a.active])
    
    recent_renewals = RenewalLog.query.filter(
        RenewalLog.timestamp >= utcnow() - timedelta(days=7)
    ).all()
    
    success_rate = 0
    if recent_renewals:
        successful = len([r for r in recent_renewals if r.success])
        success_rate = (successful / len(recent_renewals)) * 100
    
    # Find next renewal time
    next_renewal = None
    active_accounts_with_renewal = [a for a in accounts if a.active and a.next_renewal]
    if active_accounts_with_renewal:
        next_renewal = min(active_accounts_with_renewal, key=lambda x: x.next_renewal)
    
    # Get latest renewal status for each account
    account_statuses = {}
    for account in accounts:
        latest_log = RenewalLog.query.filter_by(account_id=account.id).order_by(
            RenewalLog.timestamp.desc()
        ).first()
        if latest_log:
            account_statuses[account.id] = {
                'success': latest_log.success,
                'message': latest_log.message,
                'timestamp': latest_log.timestamp
            }
    
    # Create libraries mapping for template
    libraries = {lib.type: lib.name for lib in LibraryConfig.query.all()}
    
    return render_template('dashboard.html',
                         accounts=accounts,
                         recent_logs=recent_logs,
                         total_accounts=total_accounts,
                         active_accounts=active_accounts,
                         success_rate=success_rate,
                         next_renewal=next_renewal,
                         libraries=libraries,
                         account_statuses=account_statuses)

@app.route('/accounts')
def accounts():
    """Account management page"""
    accounts = Account.query.all()
    
    # Get latest renewal status for each account
    account_statuses = {}
    for account in accounts:
        latest_log = RenewalLog.query.filter_by(account_id=account.id).order_by(
            RenewalLog.timestamp.desc()
        ).first()
        if latest_log:
            account_statuses[account.id] = {
                'success': latest_log.success,
                'message': latest_log.message,
                'timestamp': latest_log.timestamp
            }
    
    libraries = {lib.type: lib.name for lib in LibraryConfig.query.all()}
    return render_template('accounts.html', accounts=accounts, libraries=libraries, account_statuses=account_statuses)

@app.route('/accounts/add', methods=['GET', 'POST'])
def add_account():
    """Add new account"""
    form = AccountForm()
    
    # Get available active library configurations from database
    library_configs = LibraryConfig.query.filter_by(active=True).all()
    form.library_type.choices = [(config.type, config.name) for config in library_configs]
    
    if form.validate_on_submit():
        # Find the library configuration
        library_config = LibraryConfig.query.filter_by(type=form.library_type.data).first()
        if not library_config:
            flash('Selected library configuration not found', 'error')
            return redirect(url_for('add_account'))
        
        # Create account (NYT-only as of v1.0.0)
        account = Account(
            name=form.name.data,
            library_type=form.library_type.data,
            library_username=form.library_username.data,
            library_password=form.library_password.data,
            newspaper_type='nyt',
            username=None,
            password=None,
            renewal_hours=library_config.default_renewal_hours,
            renewal_interval=form.renewal_interval.data if form.renewal_interval.data else None,
            active=form.active.data
        )
        
        account.next_renewal = utcnow() + timedelta(hours=account.effective_renewal_interval)
        
        db.session.add(account)
        db.session.commit()
        
        schedule_account_renewal(account)
        
        flash('Account added successfully!', 'success')
        return redirect(url_for('accounts'))
    
    return render_template('account_form.html', form=form, title='Add Account')

@app.route('/accounts/<int:id>/edit', methods=['GET', 'POST'])
def edit_account(id):
    """Edit existing account"""
    account = Account.query.get_or_404(id)
    
    # Store original library password to preserve if not changed
    original_library_password = account.library_password

    form = EditAccountForm(obj=account)

    # Get available active library configurations from database
    library_configs = LibraryConfig.query.filter_by(active=True).all()
    form.library_type.choices = [(config.type, config.name) for config in library_configs]

    # Clear password field on GET to show placeholder
    if request.method == 'GET':
        form.library_password.data = ''

    if form.validate_on_submit():
        # Find the library configuration to get renewal hours
        library_config = LibraryConfig.query.filter_by(type=form.library_type.data).first()
        if not library_config:
            flash('Selected library configuration not found', 'error')
            return redirect(url_for('edit_account', id=id))

        account.name = form.name.data
        account.library_type = form.library_type.data
        account.library_username = form.library_username.data

        # Only update library password if a new value was provided
        if form.library_password.data:
            account.library_password = form.library_password.data
        else:
            account.library_password = original_library_password
            
        account.renewal_hours = library_config.default_renewal_hours
        account.renewal_interval = form.renewal_interval.data if form.renewal_interval.data else None
        account.active = form.active.data
        
        # Update next renewal time since renewal hours may have changed
        account.next_renewal = utcnow() + timedelta(hours=account.effective_renewal_interval)
        
        db.session.commit()
        
        try:
            scheduler.remove_job(f'renewal_{id}')
        except:
            pass
        schedule_account_renewal(account)
        
        flash('Account updated successfully!', 'success')
        return redirect(url_for('accounts'))
    
    return render_template('account_form.html', form=form, title='Edit Account', account=account)

@app.route('/accounts/<int:id>/delete', methods=['POST'])
def delete_account(id):
    """Delete account"""
    account = Account.query.get_or_404(id)
    
    try:
        scheduler.remove_job(f'renewal_{id}')
    except:
        pass
    
    db.session.delete(account)
    db.session.commit()
    
    flash('Account deleted successfully!', 'success')
    return redirect(url_for('accounts'))

def _execute_renewal(account):
    """Run a renewal, write the RenewalLog row, schedule the next attempt,
    fire notifications on state transitions.

    Returns the renewer.RenewalResult."""
    library = LibraryConfig.query.filter_by(type=account.library_type).first()
    if library is None or not library.nyt_url:
        msg = "No library configuration / NYT URL for this account."
        _record_renewal_log(account, success=False, message=msg, duration_ms=0)
        notify.notify_renewal_failed(account.name, msg)
        return None

    # Look up previous attempt so we can detect transitions (failed→ok, ok→failed).
    previous = (RenewalLog.query.filter_by(account_id=account.id)
                .order_by(RenewalLog.id.desc()).first())
    was_failing = previous is not None and not previous.success

    result = renew(
        library_url=library.nyt_url,
        library_user=account.library_username,
        library_pass=account.library_password,
        account_id=account.id,
    )

    _record_renewal_log(account, success=result.success, message=result.message,
                        duration_ms=result.duration_ms, result_url=result.final_url)

    account.last_renewal = utcnow()
    if result.success and result.expiration:
        account.next_renewal = result.expiration + timedelta(minutes=1)
    else:
        # Fall back to library default + 1 minute (covers both
        # success-without-expiration and any failure — failed renewals retry
        # on the same cadence).
        account.next_renewal = (utcnow()
                                + timedelta(hours=account.effective_renewal_interval, minutes=1))
    db.session.commit()
    schedule_account_renewal(account)

    # Notify only on transitions, not on every attempt — avoids spamming.
    if not result.success:
        notify.notify_renewal_failed(account.name, result.message)
    elif was_failing:
        notify.notify_renewal_recovered(account.name)

    return result


def _record_renewal_log(account, *, success, message, duration_ms, result_url=None):
    log = RenewalLog(
        account_id=account.id,
        success=success,
        message=message,
        duration_seconds=int((duration_ms or 0) / 1000),
        result_url=result_url,
    )
    db.session.add(log)
    db.session.commit()


@app.route('/accounts/<int:id>/renew', methods=['POST'])
def manual_renewal(id):
    """Manually trigger renewal for an account."""
    account = Account.query.get_or_404(id)
    try:
        result = _execute_renewal(account)
        if result is None:
            flash(f"Renewal failed for {account.name} — library config missing.", "error")
        elif result.success:
            flash(f"Renewal completed for {account.name} ({result.duration_ms}ms).", "success")
            logger.info(f"Manual renewal succeeded for {account.name}: {result.message}")
        else:
            flash(f"Renewal failed for {account.name}: {result.message}", "error")
            logger.warning(f"Manual renewal failed for {account.name}: {result.message}")
    except Exception as e:
        logger.error(f"Manual renewal crashed for {account.name}: {e}")
        flash(f"Renewal crashed for {account.name}: {e}", "error")
    return redirect(url_for('accounts'))


# --- Capture sessions (in-dashboard browser login) ---

NYT_LOGIN_URL = 'https://myaccount.nytimes.com/auth/login'


@app.route('/accounts/<int:id>/capture')
def capture_view(id):
    account = Account.query.get_or_404(id)
    return render_template('capture_view.html', account=account)


@app.route('/accounts/<int:id>/capture/start', methods=['POST'])
def capture_start(id):
    from capture_session import CaptureSessionManager
    account = Account.query.get_or_404(id)
    try:
        session = CaptureSessionManager().start(account.id, NYT_LOGIN_URL)
    except RuntimeError as e:
        return jsonify(error=str(e)), 409
    return jsonify(token=session.token, ws_port=session.ws_port)


@app.route('/accounts/<int:id>/capture/<token>/finish', methods=['POST'])
def capture_finish(id, token):
    from capture_session import CaptureSessionManager
    payload = request.get_json(silent=True) or {}
    save = bool(payload.get('save'))
    session = CaptureSessionManager().finish(token)
    if session is None or session.account_id != id:
        return jsonify(error='session not found'), 404
    if save:
        account = Account.query.get(id)
        if account is not None:
            account.profile_captured_at = utcnow()
            db.session.commit()
    return jsonify(ok=True, saved=save)


@app.route('/libraries')
def libraries():
    """Library configuration page"""
    configs = LibraryConfig.query.all()
    # Build {library_type: account_count} so the template can display per-config usage
    counts = {}
    for a in Account.query.all():
        counts[a.library_type] = counts.get(a.library_type, 0) + 1
    return render_template('libraries.html', configs=configs, accounts=counts)

@app.route('/libraries/add', methods=['GET', 'POST'])
def add_library():
    """Add new library configuration"""
    form = LibraryForm()
    
    if form.validate_on_submit():
        library = LibraryConfig(
            name=form.name.data,
            type=form.type.data,
            nyt_url=form.nyt_url.data,
            homepage=form.homepage.data,
            custom_config=form.custom_config.data,
            default_renewal_hours=form.default_renewal_hours.data,
            active=form.active.data
        )
        
        # Store additional configuration in custom_config if provided
        import json
        config_data = {}
        if form.custom_config.data:
            try:
                config_data = json.loads(form.custom_config.data)
            except:
                config_data = {}
        
        library.custom_config = json.dumps(config_data) if config_data else None
        
        db.session.add(library)
        db.session.commit()
        
        flash('Library added successfully!', 'success')
        return redirect(url_for('libraries'))
    
    return render_template('library_form.html', form=form, title='Add Library')

@app.route('/libraries/<int:id>/edit', methods=['GET', 'POST'])
def edit_library(id):
    """Edit existing library configuration"""
    library = LibraryConfig.query.get_or_404(id)
    
    # Initialize form data properly
    if request.method == 'GET':
        # Load data from database fields
        form_data = {
            'name': library.name,
            'type': library.type,
            'nyt_url': library.nyt_url or '',
            'homepage': library.homepage,
            'default_renewal_hours': library.default_renewal_hours,
            'active': library.active,
            'custom_config': library.custom_config or ''
        }
        
        form = LibraryForm(data=form_data)
    else:
        form = LibraryForm()
    
    if form.validate_on_submit():
        library.name = form.name.data
        library.type = form.type.data
        library.nyt_url = form.nyt_url.data
        library.homepage = form.homepage.data
        library.default_renewal_hours = form.default_renewal_hours.data
        library.active = form.active.data
        
        # Store additional configuration in custom_config if provided
        import json
        config_data = {}
        if form.custom_config.data:
            try:
                config_data = json.loads(form.custom_config.data)
            except:
                config_data = {}
        
        library.custom_config = json.dumps(config_data) if config_data else None
        
        db.session.commit()
        
        flash('Library updated successfully!', 'success')
        return redirect(url_for('libraries'))
    
    return render_template('library_form.html', form=form, title='Edit Library', library=library)

@app.route('/libraries/<int:id>/delete', methods=['POST'])
def delete_library(id):
    """Delete library configuration"""
    library = LibraryConfig.query.get_or_404(id)
    
    # Check if any accounts are using this library
    accounts_using = Account.query.filter_by(library_type=library.type).count()
    if accounts_using > 0:
        flash(f'Cannot delete library - {accounts_using} accounts are using it', 'error')
        return redirect(url_for('libraries'))
    
    db.session.delete(library)
    db.session.commit()
    
    flash('Library deleted successfully!', 'success')
    return redirect(url_for('libraries'))

@app.route('/logs')
def logs():
    """View renewal logs"""
    page = request.args.get('page', 1, type=int)
    logs = RenewalLog.query.order_by(RenewalLog.timestamp.desc()).paginate(
        page=page, per_page=50, error_out=False
    )
    accounts = {a.id: a for a in Account.query.all()}
    return render_template('logs.html', logs=logs, accounts=accounts)

@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    """Clear all renewal logs and ALL screenshot directories"""
    try:
        deleted_logs = RenewalLog.query.count()
        RenewalLog.query.delete()
        db.session.commit()

        # Sweep any v0.5-era screenshot directories. The HTTP-only renewer
        # doesn't produce screenshots, so this is purely a one-time legacy
        # cleanup; new installs will find the dir empty or absent.
        deleted_dirs = 0
        if os.path.exists(SCREENSHOTS_DIR):
            import shutil
            for item in os.listdir(SCREENSHOTS_DIR):
                if item.startswith('.'):
                    continue
                dir_path = os.path.join(SCREENSHOTS_DIR, item)
                if os.path.isdir(dir_path):
                    try:
                        shutil.rmtree(dir_path)
                        deleted_dirs += 1
                    except OSError as e:
                        logger.warning(f"Failed to delete legacy screenshot dir {item}: {e}")

        logger.info(f"🧹 Cleared {deleted_logs} log entries (+{deleted_dirs} legacy screenshot dirs)")

        return jsonify({
            'success': True,
            'message': f'Cleared {deleted_logs} log entries',
            'deleted_logs': deleted_logs,
            'deleted_directories': deleted_dirs,
        })
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to clear logs: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'Failed to clear logs: {str(e)}'
        }), 500


@app.route('/api/status')
def api_status():
    """API endpoint for system status"""
    accounts = Account.query.all()
    active_jobs = len(scheduler.get_jobs()) if scheduler else 0

    status = {
        'total_accounts': len(accounts),
        'active_accounts': len([a for a in accounts if a.active]),
        'scheduled_jobs': active_jobs,
        'system_status': 'running',
        'last_check': utcnow().isoformat()
    }

    return jsonify(status)

@app.route('/health')
@app.route('/api/health')
def health_check():
    """Health check endpoint for monitoring"""
    try:
        # Check database connection
        db_healthy = True
        try:
            db.session.execute(db.text('SELECT 1'))
        except Exception:
            db_healthy = False
        
        # Check scheduler
        scheduler_healthy = scheduler is not None and scheduler.running

        # Overall health
        is_healthy = db_healthy and scheduler_healthy

        health_status = {
            'status': 'healthy' if is_healthy else 'unhealthy',
            'timestamp': utcnow().isoformat(),
            'version': __version__,
            'uptime_seconds': int((utcnow() - startup_time).total_seconds()),
            'checks': {
                'database': 'healthy' if db_healthy else 'unhealthy',
                'scheduler': 'healthy' if scheduler_healthy else 'unhealthy',
            }
        }
        
        return jsonify(health_status), 200 if is_healthy else 503
        
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e),
            'timestamp': utcnow().isoformat()
        }), 500

@app.route('/api/logs')
def api_logs():
    """API endpoint for all logs"""
    logs = RenewalLog.query.order_by(RenewalLog.timestamp.desc()).all()
    
    log_data = []
    for log in logs:
        account = Account.query.get(log.account_id)
        log_data.append({
            'id': log.id,
            'timestamp': localtime_filter(log.timestamp).isoformat() if log.timestamp else None,
            'success': log.success,
            'message': log.message,
            'duration_seconds': log.duration_seconds,
            'account_id': log.account_id,
            'account_name': account.display_name if account else 'Unknown Account',
            'screenshot_filename': log.screenshot_filename
        })
    
    return jsonify(log_data)

@app.route('/api/accounts')
def api_accounts():
    """API endpoint for all accounts"""
    accounts = Account.query.all()
    
    account_data = [{
        'id': account.id,
        'name': account.name,
        'library_type': account.library_type,
        'newspaper_type': getattr(account, 'newspaper_type', 'nyt'),  # Default to nyt for backward compatibility
        'active': account.active,
        'last_renewal': localtime_filter(account.last_renewal).isoformat() if account.last_renewal else None,
        'next_renewal': localtime_filter(account.next_renewal).isoformat() if account.next_renewal else None
    } for account in accounts]
    
    return jsonify(account_data)

@app.route('/api/accounts/<int:id>/logs')
def api_account_logs(id):
    """API endpoint for account-specific logs"""
    logs = RenewalLog.query.filter_by(account_id=id).order_by(
        RenewalLog.timestamp.desc()
    ).limit(20).all()
    
    log_data = [{
        'timestamp': localtime_filter(log.timestamp).isoformat() if log.timestamp else None,
        'success': log.success,
        'message': log.message,
        'duration': log.duration_seconds,
        'result_url': log.result_url,
        'screenshot_filename': log.screenshot_filename
    } for log in logs]
    
    return jsonify(log_data)

@app.route('/api/screenshots/<path:filepath>')
def serve_screenshot(filepath):
    """Serve a v0.5-era debug screenshot.

    The HTTP-only renewer doesn't produce screenshots, so this only
    matters for users with legacy logs that still reference image paths.
    Kept around so old log entries still link to their images."""
    try:
        from flask import send_from_directory

        # Path-traversal guard
        if '..' in filepath or filepath.startswith('/') or not filepath.endswith('.png'):
            return jsonify({'error': 'Invalid filepath'}), 400

        full = os.path.join(SCREENSHOTS_DIR, filepath)
        if not os.path.exists(full):
            return jsonify({'error': 'Screenshot not found'}), 404
        return send_from_directory(
            os.path.dirname(full), os.path.basename(full), mimetype='image/png'
        )
        
    except Exception as e:
        app.logger.error(f"Error serving screenshot {filepath}: {str(e)}")
        return jsonify({'error': 'Server error'}), 500

# Utility functions
def schedule_account_renewal(account):
    """Schedule renewal job for an account"""
    if not account.active:
        return
    
    # Initialize scheduler if needed
    init_scheduler()
    
    job_id = f'renewal_{account.id}'
    
    try:
        scheduler.remove_job(job_id)
    except:
        pass
    
    # If we have a next_renewal date, schedule for that specific time
    if account.next_renewal:
        from apscheduler.triggers.date import DateTrigger
        import pytz
        
        # Ensure next_renewal is timezone-aware (stored as UTC)
        if account.next_renewal.tzinfo is None:
            next_run = pytz.UTC.localize(account.next_renewal)
        else:
            next_run = account.next_renewal
        
        scheduler.add_job(
            func=run_account_renewal,
            trigger=DateTrigger(run_date=next_run),
            id=job_id,
            args=[account.id],
            replace_existing=True
        )
        logger.info(f"📅 Scheduled renewal for {account.name} at {next_run}")
    else:
        # Fallback to interval-based scheduling using effective interval
        scheduler.add_job(
            func=run_account_renewal,
            trigger=IntervalTrigger(hours=account.effective_renewal_interval, minutes=1),
            id=job_id,
            args=[account.id],
            replace_existing=True
        )
        logger.info(f"⏰ Scheduled renewal for {account.name} every {account.effective_renewal_interval} hours")

def run_account_renewal(account_id):
    """Run renewal for a specific account (called by scheduler)."""
    with app.app_context():
        account = Account.query.get(account_id)
        if not account or not account.active:
            return
        try:
            result = _execute_renewal(account)
            if result is None:
                logger.warning(f"Scheduled renewal skipped for {account.name} — library config missing.")
            elif result.success:
                logger.info(f"Scheduled renewal succeeded for {account.name} ({result.duration_ms}ms)")
            else:
                logger.warning(f"Scheduled renewal failed for {account.name}: {result.message}")
        except Exception as e:
            logger.error(f"Scheduled renewal crashed for {account.name}: {e}")
            # Best-effort: keep the schedule rolling
            try:
                account.next_renewal = (utcnow()
                                        + timedelta(hours=account.effective_renewal_interval, minutes=1))
                db.session.commit()
                logger.info(f"⏰ Scheduled retry for {account.name} using {account.effective_renewal_interval}h 1m interval despite error")
            except Exception as commit_error:
                logger.error(f"Failed to update next_renewal after error: {commit_error}")

        finally:
            # CRITICAL: Always reschedule, even if renewal failed
            # This ensures continuous renewal attempts
            try:
                schedule_account_renewal(account)
            except Exception as schedule_error:
                logger.error(f"Failed to reschedule renewal for {account.name}: {schedule_error}")

def init_db():
    """Initialize database"""
    with app.app_context():
        # Check if we need to add the newspaper_type column
        needs_migration = False
        try:
            # Try to query the newspaper_type column to see if it exists
            db.session.execute(db.text("SELECT newspaper_type FROM account LIMIT 1"))
        except Exception:
            # Column doesn't exist, we need migration
            needs_migration = True
            logger.info("newspaper_type column not found, will add it")
        
        if needs_migration:
            try:
                # Add the newspaper_type column with default value
                logger.info("Adding newspaper_type column to account table")
                db.session.execute(db.text("ALTER TABLE account ADD COLUMN newspaper_type VARCHAR(20) DEFAULT 'nyt'"))
                
                # Update all existing accounts to have newspaper_type = 'nyt'
                try:
                    result = db.session.execute(db.text("UPDATE account SET newspaper_type = 'nyt' WHERE newspaper_type IS NULL"))
                    logger.info(f"Updated {result.rowcount} existing accounts with newspaper_type='nyt'")
                except Exception as e:
                    logger.debug(f"Update skipped (likely already done): {e}")
                
                db.session.commit()
                logger.info("Migration completed successfully")
            except Exception as e:
                logger.error(f"Migration failed: {e}")
                db.session.rollback()
        
        # Check if we need to add the renewal_interval column
        needs_interval_migration = False
        try:
            # Try to query the renewal_interval column to see if it exists
            db.session.execute(db.text("SELECT renewal_interval FROM account LIMIT 1"))
        except Exception:
            # Column doesn't exist, we need migration
            needs_interval_migration = True
            logger.info("renewal_interval column not found, will add it")
        
        if needs_interval_migration:
            try:
                # Add the renewal_interval column (nullable for inheritance)
                logger.info("Adding renewal_interval column to account table")
                db.session.execute(db.text("ALTER TABLE account ADD COLUMN renewal_interval INTEGER"))
                db.session.commit()
                logger.info("renewal_interval migration completed successfully")
            except Exception as e:
                logger.error(f"renewal_interval migration failed: {e}")
                db.session.rollback()

        # Profile capture column (added 2026-05 for in-dashboard noVNC capture)
        try:
            db.session.execute(db.text("SELECT profile_captured_at FROM account LIMIT 1"))
        except Exception:
            try:
                logger.info("Adding profile_captured_at column to account table")
                db.session.execute(db.text("ALTER TABLE account ADD COLUMN profile_captured_at DATETIME"))
                db.session.commit()
            except Exception as e:
                logger.error(f"profile_captured_at migration failed: {e}")
                db.session.rollback()

        # Now create/update all tables
        db.create_all()

        # One-shot at-rest encryption migration: re-encrypt any plaintext
        # library_password rows left from <v1.2.0. Idempotent.
        try:
            migrate_plaintext_rows(db, Account)
        except Exception as e:
            logger.warning(f"plaintext-creds migration failed: {e}")
            db.session.rollback()


def create_app():
    """Application factory pattern"""
    try:
        init_db()
        
        # Schedule renewals for active accounts
        with app.app_context():
            try:
                # Initialize scheduler first
                init_scheduler()
                
                # Schedule all active accounts
                active_accounts = Account.query.filter_by(active=True).all()
                for account in active_accounts:
                    schedule_account_renewal(account)
                    logger.info(f"📅 Scheduled renewal for {account.name}")
                
                logger.info(f"✅ Scheduled {len(active_accounts)} active accounts for renewal")
            except Exception as e:
                logger.warning(f"Could not schedule renewals at startup: {e}")
                logger.info("Renewals will be scheduled on-demand")
        
    except Exception as e:
        logger.error(f"Application initialization failed: {e}")
        # Continue anyway - the app can still start without scheduling
    
    return app

if __name__ == '__main__':
    # Only run development server if called directly
    app = create_app()
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    # Suppress development server warning
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    
    app.run(host='0.0.0.0', port=1851, debug=debug_mode)
