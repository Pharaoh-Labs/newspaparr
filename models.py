"""SQLAlchemy models — the persistent shape of the app.

Three tables:
- Account            one row per (library card + person) pair
- LibraryConfig      one row per library that exposes an EZproxy NYT pass
- RenewalLog         one row per renewal attempt, success or failure

Imports `db` from extensions.py so the models can be imported anywhere
without dragging app.py along.
"""
from datetime import datetime, timezone

from extensions import db
from secrets_at_rest import EncryptedString


def _utcnow() -> datetime:
    """Naive UTC datetime, matching the DB column type. Local copy of the
    helper in app.py to avoid a circular import — DB column defaults are
    evaluated at insert time, so cycle would form."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Account(db.Model):
    """A library card → newspaper account binding."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    library_type = db.Column(db.String(50), nullable=False)
    library_username = db.Column(db.String(100), nullable=False)
    library_password = db.Column(EncryptedString(500), nullable=False)
    newspaper_type = db.Column(db.String(20), nullable=False, default='nyt')

    renewal_hours = db.Column(db.Integer, default=24)
    renewal_interval = db.Column(db.Integer, nullable=True)  # Override; inherits from library if null
    active = db.Column(db.Boolean, default=True)
    last_renewal = db.Column(db.DateTime)
    next_renewal = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_utcnow)
    profile_captured_at = db.Column(db.DateTime, nullable=True)

    @property
    def display_name(self):
        return f"{self.name} (NYT)"

    @property
    def effective_renewal_interval(self):
        """Account override → library default → 24h fallback."""
        if self.renewal_interval is not None:
            return self.renewal_interval
        library = LibraryConfig.query.filter_by(type=self.library_type, active=True).first()
        if library:
            return library.default_renewal_hours
        return self.renewal_hours or 24


class LibraryConfig(db.Model):
    """A library's EZproxy NYT redemption endpoint."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    homepage = db.Column(db.String(500))
    nyt_url = db.Column(db.String(500))           # Direct NYT access URL via EZproxy
    default_renewal_hours = db.Column(db.Integer, default=24)
    active = db.Column(db.Boolean, default=True)


class RenewalLog(db.Model):
    """One row per renewal attempt — success or failure."""
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=_utcnow)
    success = db.Column(db.Boolean, nullable=False)
    message = db.Column(db.Text)
    duration_seconds = db.Column(db.Integer)
    result_url = db.Column(db.String(500))
