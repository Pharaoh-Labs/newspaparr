"""
Flask web application for NYT Auto-Renewal System
Provides web UI for configuration, monitoring, and management
"""

import logging
import os
import secrets
from datetime import datetime, timedelta

import pytz
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from extensions import db, migrate
from forms import AccountForm, EditAccountForm, LibraryForm
from helpers import execute_renewal, utcnow
from icons import icon
from models import Account, LibraryConfig, RenewalLog
from paths import DATA_DIR, DEFAULT_DB_URL, LOGS_DIR
import notify
import scheduler as _scheduler_mod
from scheduler import schedule_account_renewal
from secrets_at_rest import migrate_plaintext_rows

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

# Bind extensions to this app
db.init_app(app)
migrate.init_app(app, db)

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

# Models, forms, and the scheduler live in their own modules and are
# imported above. The scheduler is bound to the app + the renewal
# callback inside create_app() once the model layer is ready.

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

@app.route('/accounts/<int:id>/renew', methods=['POST'])
def manual_renewal(id):
    """Manually trigger renewal for an account."""
    account = Account.query.get_or_404(id)
    try:
        result = execute_renewal(account)
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
            default_renewal_hours=form.default_renewal_hours.data,
            active=form.active.data,
        )
        db.session.add(library)
        db.session.commit()
        flash('Library added successfully!', 'success')
        return redirect(url_for('libraries'))

    return render_template('library_form.html', form=form, title='Add Library')

@app.route('/libraries/<int:id>/edit', methods=['GET', 'POST'])
def edit_library(id):
    """Edit existing library configuration"""
    library = LibraryConfig.query.get_or_404(id)
    
    if request.method == 'GET':
        form = LibraryForm(data={
            'name': library.name,
            'type': library.type,
            'nyt_url': library.nyt_url or '',
            'homepage': library.homepage,
            'default_renewal_hours': library.default_renewal_hours,
            'active': library.active,
        })
    else:
        form = LibraryForm()

    if form.validate_on_submit():
        library.name = form.name.data
        library.type = form.type.data
        library.nyt_url = form.nyt_url.data
        library.homepage = form.homepage.data
        library.default_renewal_hours = form.default_renewal_hours.data
        library.active = form.active.data
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
    """Clear all renewal log entries."""
    try:
        deleted_logs = RenewalLog.query.count()
        RenewalLog.query.delete()
        db.session.commit()
        logger.info(f"🧹 Cleared {deleted_logs} log entries")
        return jsonify({
            'success': True,
            'message': f'Cleared {deleted_logs} log entries',
            'deleted_logs': deleted_logs,
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
    status = {
        'total_accounts': len(accounts),
        'active_accounts': len([a for a in accounts if a.active]),
        'scheduled_jobs': _scheduler_mod.job_count(),
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
    } for log in logs]

    return jsonify(log_data)


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
        # library_password rows left from <v1.1.0. Idempotent.
        try:
            migrate_plaintext_rows(db, Account)
        except Exception as e:
            logger.warning(f"plaintext-creds migration failed: {e}")
            db.session.rollback()


def create_app():
    """Application factory pattern"""
    try:
        init_db()

        # Bind the scheduler module to this app + the renewal callback.
        # Done after init_db() so the model layer is ready, before any
        # schedule_account_renewal() call.
        _scheduler_mod.init(app, execute_renewal)

        with app.app_context():
            try:
                active_accounts = Account.query.filter_by(active=True).all()
                for account in active_accounts:
                    schedule_account_renewal(account)
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
