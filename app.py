"""
Flask web application for NYT Auto-Renewal System
Provides web UI for configuration, monitoring, and management
"""

import os
import logging
from datetime import datetime, timedelta
from error_handling import StandardizedLogger, with_error_handling
from config_validation import validate_startup_config, get_validated_config
try:
    import pytz
    TIMEZONE_AVAILABLE = True
except ImportError:
    try:
        import zoneinfo
        TIMEZONE_AVAILABLE = True
    except ImportError:
        TIMEZONE_AVAILABLE = False

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SelectField, IntegerField, BooleanField, TextAreaField
from wtforms.validators import DataRequired, NumberRange
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import atexit
from werkzeug.middleware.proxy_fix import ProxyFix

from library_adapters import LibraryAdapterFactory
from renewal_engine import RenewalEngine

# Application version
__version__ = '0.5.26'

# Validate configuration at startup
if __name__ == '__main__':
    # Only validate on direct execution, not on import
    config_valid = validate_startup_config()
    if not config_valid:
        exit(1)

# Get validated configuration
validated_config = get_validated_config()

# Track startup time
startup_time = datetime.utcnow()

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = validated_config.get('SECRET_KEY', os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production'))
app.config['SQLALCHEMY_DATABASE_URI'] = validated_config.get('DATABASE_URL', os.environ.get('DATABASE_URL', 'sqlite:////app/data/newspaparr.db'))
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False


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

# Add datetime context for templates
@app.context_processor
def inject_datetime():
    return {'datetime': datetime}

# Add timezone filter for converting UTC to local time
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

# Add version and uptime context
@app.context_processor
def inject_app_info():
    uptime = datetime.utcnow() - startup_time
    hours = int(uptime.total_seconds() // 3600)
    minutes = int((uptime.total_seconds() % 3600) // 60)
    uptime_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
    return {
        'app_version': __version__,
        'app_uptime': uptime_str
    }

# Initialize database
db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Setup logging to both file and console
import logging.handlers
import os

# Only configure logging if not already configured (e.g., when running directly, not via wsgi)
if not logging.getLogger().handlers:
    # Create logs directory in data (already mounted volume)
    logs_dir = os.path.join(os.path.dirname(__file__), 'data', 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    # Configure root logger
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            # Console handler
            logging.StreamHandler(),
            # File handler with rotation
            logging.handlers.RotatingFileHandler(
                os.path.join(logs_dir, 'newspaparr.log'),
                maxBytes=10*1024*1024,  # 10MB
                backupCount=5
            )
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"🗂️  Logging initialized from app.py - files will be saved to {logs_dir}")
else:
    logger = logging.getLogger(__name__)
    logger.info("📝 Logging already configured, using existing configuration")

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
    library_password = db.Column(db.String(200), nullable=False)
    username = db.Column(db.String(100), nullable=True)
    password = db.Column(db.String(200), nullable=True)
    newspaper_type = db.Column(db.String(20), nullable=False, default='nyt')
    
    renewal_hours = db.Column(db.Integer, default=24)
    renewal_interval = db.Column(db.Integer, nullable=True)  # Optional override, inherits from library if null
    active = db.Column(db.Boolean, default=True)
    last_renewal = db.Column(db.DateTime)
    next_renewal = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    @property
    def display_name(self):
        """Get display name with newspaper type"""
        newspaper_labels = {
            'nyt': 'NYT',
            'wsj': 'WSJ'
        }
        label = newspaper_labels.get(self.newspaper_type, self.newspaper_type.upper())
        return f"{self.name} ({label})"
    
    @property
    def newspaper_username(self):
        """Generic property for newspaper username"""
        return self.username
    
    @property
    def auth_display(self):
        """Display text for authentication method"""
        return self.username
    
    @newspaper_username.setter
    def newspaper_username(self, value):
        self.username = value
    
    @property
    def newspaper_password(self):
        """Generic property for newspaper password"""
        return self.password
    
    @newspaper_password.setter  
    def newspaper_password(self, value):
        self.password = value
    
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
    wsj_url = db.Column(db.String(500))  # Direct WSJ access URL
    custom_config = db.Column(db.Text)
    default_renewal_hours = db.Column(db.Integer, default=24)
    active = db.Column(db.Boolean, default=True)

class RenewalLog(db.Model):
    """Renewal log model"""
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    success = db.Column(db.Boolean, nullable=False)
    message = db.Column(db.Text)
    duration_seconds = db.Column(db.Integer)
    result_url = db.Column(db.String(500))
    screenshot_filename = db.Column(db.String(255))  # Final screenshot filename

# Forms
class AccountForm(FlaskForm):
    """Form for account configuration"""
    name = StringField('Account Name', validators=[DataRequired()])
    library_type = SelectField('Library Type', choices=[])
    library_username = StringField('Library Username/Card Number', validators=[DataRequired()])
    library_password = PasswordField('Library Password/PIN', validators=[DataRequired()])
    newspaper_type = SelectField('Newspaper', choices=[
        ('nyt', 'New York Times'),
        ('wsj', 'Wall Street Journal')
    ], default='nyt')
    username = StringField('Newspaper Email', validators=[DataRequired()])
    password = PasswordField('Newspaper Password', validators=[DataRequired()])
    renewal_interval = IntegerField('Renewal Interval Override (hours)', validators=[])
    active = BooleanField('Active', default=True)

class EditAccountForm(FlaskForm):
    """Form for editing account configuration - passwords optional"""
    name = StringField('Account Name', validators=[DataRequired()])
    library_type = SelectField('Library Type', choices=[])
    library_username = StringField('Library Username/Card Number', validators=[DataRequired()])
    library_password = PasswordField('Library Password/PIN', validators=[])  # No DataRequired
    newspaper_type = SelectField('Newspaper', choices=[
        ('nyt', 'New York Times'),
        ('wsj', 'Wall Street Journal')
    ], default='nyt')
    username = StringField('Newspaper Email', validators=[DataRequired()])
    password = PasswordField('Newspaper Password', validators=[])  # No DataRequired
    renewal_interval = IntegerField('Renewal Interval Override (hours)', validators=[])
    active = BooleanField('Active', default=True)

class LibraryForm(FlaskForm):
    """Form for library configuration"""
    name = StringField('Library Name', validators=[DataRequired()])
    type = SelectField('Library Type', choices=[
        ('generic_oclc', 'OCLC Library'),
        ('custom', 'Custom Library')
    ])
    nyt_url = StringField('NYT Access URL', validators=[DataRequired()], description='Direct URL for NYT access through your library')
    wsj_url = StringField('WSJ Access URL', validators=[DataRequired()], description='Direct URL for WSJ access through your library')
    homepage = StringField('Library Homepage (optional)', description='Main library website URL for linking')
    default_renewal_hours = IntegerField('Default Renewal Hours', validators=[NumberRange(min=1, max=168)], default=24, description='Renewals will run at this interval + 1 minute')
    active = BooleanField('Active', default=True)
    custom_config = TextAreaField('Additional Configuration (JSON)', description='Optional: JSON configuration for advanced settings')

# Routes
@app.route('/')
def index():
    """Dashboard - main page"""
    accounts = Account.query.all()
    recent_logs = RenewalLog.query.order_by(RenewalLog.timestamp.desc()).limit(10).all()
    
    total_accounts = len(accounts)
    active_accounts = len([a for a in accounts if a.active])
    
    recent_renewals = RenewalLog.query.filter(
        RenewalLog.timestamp >= datetime.utcnow() - timedelta(days=7)
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
                         account_count=total_accounts,
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
        
        # Create account
        account = Account(
            name=form.name.data,
            library_type=form.library_type.data,
            library_username=form.library_username.data,
            library_password=form.library_password.data,
            newspaper_type=form.newspaper_type.data,
            username=form.username.data,
            password=form.password.data,
            renewal_hours=library_config.default_renewal_hours,
            renewal_interval=form.renewal_interval.data if form.renewal_interval.data else None,
            active=form.active.data
        )
        
        account.next_renewal = datetime.utcnow() + timedelta(hours=account.effective_renewal_interval)
        
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
    
    # Store original passwords to preserve if not changed
    original_library_password = account.library_password
    original_password = account.password
    
    form = EditAccountForm(obj=account)  # Use EditAccountForm which has optional passwords
    
    # Get available active library configurations from database
    library_configs = LibraryConfig.query.filter_by(active=True).all()
    form.library_type.choices = [(config.type, config.name) for config in library_configs]
    
    # Clear password fields on GET to show placeholders
    if request.method == 'GET':
        form.library_password.data = ''
        form.password.data = ''
    
    if form.validate_on_submit():
        # Find the library configuration to get renewal hours
        library_config = LibraryConfig.query.filter_by(type=form.library_type.data).first()
        if not library_config:
            flash('Selected library configuration not found', 'error')
            return redirect(url_for('edit_account', id=id))
        
        account.name = form.name.data
        account.library_type = form.library_type.data
        account.library_username = form.library_username.data
        
        # Only update passwords if new values provided
        if form.library_password.data:
            account.library_password = form.library_password.data
        else:
            account.library_password = original_library_password
            
        account.newspaper_type = form.newspaper_type.data
        account.username = form.username.data
        
        if form.password.data:
            account.password = form.password.data
        else:
            account.password = original_password
            
        account.renewal_hours = library_config.default_renewal_hours
        account.renewal_interval = form.renewal_interval.data if form.renewal_interval.data else None
        account.active = form.active.data
        
        # Update next renewal time since renewal hours may have changed
        account.next_renewal = datetime.utcnow() + timedelta(hours=account.effective_renewal_interval)
        
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
    """Manually trigger renewal for specific account"""
    account = Account.query.get_or_404(id)
    logger = StandardizedLogger(__name__)
    
    try:
        # Always use GUI mode with virtual display for better anti-detection
        headless = False
        
        renewal_engine = RenewalEngine(headless=headless)
        success, result_url, expiration_datetime = renewal_engine.renew_account(account)
        
        if success:
            # Update scheduling based on expiration date if available
            if expiration_datetime:
                account.next_renewal = expiration_datetime + timedelta(minutes=1)
                # Convert to local time for logging
                local_next = localtime_filter(account.next_renewal)
                logger.info(f"📅 Updated next renewal for {account.name} ({account.newspaper_type.upper()}) based on expiration: {local_next.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            else:
                # Fallback: 24 hours + 1 minute from now (successful renewal time)
                account.next_renewal = datetime.utcnow() + timedelta(hours=24, minutes=1)
                # Convert to local time for logging
                local_next = localtime_filter(account.next_renewal)
                logger.info(f"⏰ Updated next renewal for {account.name} ({account.newspaper_type.upper()}) using 24h+1m interval: {local_next.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            
            # Update last renewal time and reschedule
            account.last_renewal = datetime.utcnow()
            db.session.commit()
            schedule_account_renewal(account)
            
            # Get the latest log entry to show the result
            latest_log = RenewalLog.query.filter_by(account_id=account.id).order_by(RenewalLog.timestamp.desc()).first()
            if latest_log and latest_log.result_url:
                flash(f'Renewal completed for {account.name}. <a href="{latest_log.result_url}" target="_blank" class="underline">View result page</a>', 'success')
            else:
                flash(f'Renewal completed for {account.name}', 'success')
            
            logger.info("Manual renewal completed successfully", account=account.name)
        else:
            flash(f'Renewal failed for {account.name}', 'error')
            logger.warning("Manual renewal failed", account=account.name)
        
    except Exception as e:
        logger.error("Manual renewal encountered error", error=e, account=account.name)
        flash(f'Renewal failed for {account.name} - {str(e)}', 'error')
    
    return redirect(url_for('accounts'))


@app.route('/libraries')
def libraries():
    """Library configuration page"""
    configs = LibraryConfig.query.all()
    accounts = Account.query.all()
    return render_template('libraries.html', configs=configs, accounts=accounts)

@app.route('/libraries/add', methods=['GET', 'POST'])
def add_library():
    """Add new library configuration"""
    form = LibraryForm()
    
    if form.validate_on_submit():
        library = LibraryConfig(
            name=form.name.data,
            type=form.type.data,
            nyt_url=form.nyt_url.data,
            wsj_url=form.wsj_url.data,
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
            'wsj_url': library.wsj_url or '',
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
        library.wsj_url = form.wsj_url.data
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
    return render_template('logs.html', logs=logs)

@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    """Clear all renewal logs and ALL screenshot directories"""
    try:
        # Clear all renewal logs from database
        deleted_logs = RenewalLog.query.count()
        RenewalLog.query.delete()
        db.session.commit()
        
        # Delete ALL screenshot/HTML directories (not just ones with logs)
        screenshots_dir = os.path.join(os.path.dirname(__file__), 'data', 'debug', 'screenshots')
        deleted_dirs = 0
        
        if os.path.exists(screenshots_dir):
            import shutil
            # Get all directories in screenshots folder
            for item in os.listdir(screenshots_dir):
                if item.startswith('.'):  # Skip hidden files like .DS_Store
                    continue
                    
                dir_path = os.path.join(screenshots_dir, item)
                if os.path.isdir(dir_path):
                    try:
                        shutil.rmtree(dir_path)
                        deleted_dirs += 1
                        logger.info(f"🗑️  Deleted attempt directory: {item}")
                    except Exception as e:
                        logger.warning(f"Failed to delete directory {item}: {str(e)}")
        
        logger.info(f"🧹 Manual cleanup: cleared {deleted_logs} log entries and {deleted_dirs} screenshot directories")
        
        return jsonify({
            'success': True,
            'message': f'Cleared {deleted_logs} log entries and {deleted_dirs} screenshot directories',
            'deleted_logs': deleted_logs,
            'deleted_directories': deleted_dirs
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
    
    # Check proxy status
    try:
        from on_demand_proxy import get_proxy_manager
        proxy_manager = get_proxy_manager()
        proxy_status = {
            'running': proxy_manager.is_proxy_running(),
            'type': 'on-demand',
            'security': 'enhanced'
        }
    except Exception:
        proxy_status = {
            'running': False,
            'type': 'on-demand',
            'security': 'enhanced'
        }
    
    status = {
        'total_accounts': len(accounts),
        'active_accounts': len([a for a in accounts if a.active]),
        'scheduled_jobs': active_jobs,
        'proxy_status': proxy_status,
        'system_status': 'running',
        'last_check': datetime.utcnow().isoformat()
    }
    
    return jsonify(status)

@app.route('/api/config')
def api_config():
    """API endpoint for configuration validation"""
    from config_validation import ConfigValidator
    
    validator = ConfigValidator()
    result = validator.validate_config()
    
    # Create safe config (mask sensitive values)
    safe_config = {}
    for key, value in result.config.items():
        if key in ['SECRET_KEY', 'CAPSOLVER_API_KEY'] and value:
            safe_config[key] = f"{str(value)[:8]}..." if len(str(value)) > 8 else "***"
        else:
            safe_config[key] = value
    
    return jsonify({
        'is_valid': result.is_valid,
        'errors': result.errors,
        'warnings': result.warnings,
        'config': safe_config
    })

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
        
        # Check proxy status
        proxy_healthy = True
        try:
            from on_demand_proxy import get_proxy_manager
            proxy_manager = get_proxy_manager()
            proxy_healthy = proxy_manager.is_proxy_running()
        except Exception:
            proxy_healthy = False
        
        # Check scheduler
        scheduler_healthy = scheduler is not None and scheduler.running
        
        # Overall health
        is_healthy = db_healthy and scheduler_healthy
        
        health_status = {
            'status': 'healthy' if is_healthy else 'unhealthy',
            'timestamp': datetime.utcnow().isoformat(),
            'version': __version__,
            'uptime_seconds': int((datetime.utcnow() - startup_time).total_seconds()),
            'checks': {
                'database': 'healthy' if db_healthy else 'unhealthy',
                'scheduler': 'healthy' if scheduler_healthy else 'unhealthy',
                'proxy': 'healthy' if proxy_healthy else 'unhealthy'
            }
        }
        
        return jsonify(health_status), 200 if is_healthy else 503
        
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e),
            'timestamp': datetime.utcnow().isoformat()
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
    """Serve screenshot files from debug directory (supports subfolders)"""
    try:
        from flask import send_from_directory
        import os
        
        # Construct the path to the screenshots directory
        screenshots_dir = os.path.join(os.path.dirname(__file__), 'data', 'debug', 'screenshots')
        
        # Security check: ensure filepath is safe
        if '..' in filepath or filepath.startswith('/') or not filepath.endswith('.png'):
            return jsonify({'error': 'Invalid filepath'}), 400
        
        # Handle both old flat structure and new subfolder structure
        screenshot_path = os.path.join(screenshots_dir, filepath)
        
        # If the file doesn't exist in subfolder, try the flat structure (backwards compatibility)
        if not os.path.exists(screenshot_path):
            # Extract just the filename for backwards compatibility
            filename = os.path.basename(filepath)
            flat_path = os.path.join(screenshots_dir, filename)
            if os.path.exists(flat_path):
                return send_from_directory(screenshots_dir, filename, mimetype='image/png')
            else:
                return jsonify({'error': 'Screenshot not found'}), 404
        
        # Serve from subfolder structure
        directory = os.path.dirname(screenshot_path)
        filename = os.path.basename(screenshot_path)
        
        return send_from_directory(directory, filename, mimetype='image/png')
        
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
        logger.info(f"📅 Scheduled renewal for {account.name} ({account.newspaper_type.upper()}) at {next_run}")
    else:
        # Fallback to interval-based scheduling using effective interval
        scheduler.add_job(
            func=run_account_renewal,
            trigger=IntervalTrigger(hours=account.effective_renewal_interval, minutes=1),
            id=job_id,
            args=[account.id],
            replace_existing=True
        )
        logger.info(f"⏰ Scheduled renewal for {account.name} ({account.newspaper_type.upper()}) every {account.effective_renewal_interval} hours")

def run_account_renewal(account_id):
    """Run renewal for a specific account (called by scheduler)"""
    with app.app_context():
        account = Account.query.get(account_id)
        if not account or not account.active:
            return
        
        # Always use GUI mode with virtual display for better anti-detection
        headless = False
        renewal_engine = RenewalEngine(headless=headless)
        success, result_url, expiration_datetime = renewal_engine.renew_account(account)
        
        # Update renewal tracking
        account.last_renewal = datetime.utcnow()
        
        # Use expiration date + 1 minute if available, otherwise fall back to 24h intervals
        if success and expiration_datetime:
            # Schedule next renewal for 1 minute after pass expires
            account.next_renewal = expiration_datetime + timedelta(minutes=1)
            logger.info(f"📅 Scheduled next renewal for {account.name} ({account.newspaper_type.upper()}) based on expiration: {account.next_renewal}")
        else:
            # Fallback to intervals + 1 minute when no expiration date available
            account.next_renewal = datetime.utcnow() + timedelta(hours=account.effective_renewal_interval, minutes=1)
            logger.info(f"⏰ Scheduled next renewal for {account.name} ({account.newspaper_type.upper()}) using {account.effective_renewal_interval}h 1m interval: {account.next_renewal}")
        
        db.session.commit()
        
        # Reschedule the account renewal with updated timing
        schedule_account_renewal(account)

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
        
        # Now create/update all tables
        db.create_all()
        

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
                    logger.info(f"📅 Scheduled renewal for {account.name} ({account.newspaper_type.upper()})")
                
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
