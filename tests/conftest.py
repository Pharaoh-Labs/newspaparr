"""Pytest fixtures — boot the Flask app against a temp SQLite DB so smoke
tests don't touch the real data/ directory."""
import os
import tempfile

import pytest


@pytest.fixture(scope="session")
def app_client():
    """Boot the Flask app once per session against an isolated temp DB."""
    db_fd, db_path = tempfile.mkstemp(suffix=".db", prefix="newspaparr-test-")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["SECRET_KEY"] = "test-key-not-for-prod"

    # Import after env vars are set so the app reads them.
    import app as app_mod  # noqa: WPS433 (intentional late import)

    app_mod.app.config["TESTING"] = True
    app_mod.app.config["WTF_CSRF_ENABLED"] = False  # forms post-test friendly

    with app_mod.app.app_context():
        app_mod.db.create_all()

    client = app_mod.app.test_client()
    yield app_mod, client

    os.close(db_fd)
    os.unlink(db_path)
