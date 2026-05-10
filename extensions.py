"""Flask extensions instantiated once and bound to the app at startup.

Lives in its own module so models.py can import `db` without creating a
circular dependency back into app.py."""
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()
