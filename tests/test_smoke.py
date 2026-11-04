"""Smoke tests — every public route renders without raising.

These won't catch logic bugs, but they catch "I deleted an import / typoed a
template variable / regressed a route" — exactly the class of mistake that
slipped past me until the sidebar-badge regression was flagged.
"""
import pytest


@pytest.mark.parametrize("path", [
    "/",
    "/accounts",
    "/accounts/add",
    "/libraries",
    "/libraries/add",
    "/logs",
])
def test_route_renders(app_client, path):
    _, client = app_client
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} returned {resp.status_code}"


def test_api_endpoints_return_json(app_client):
    _, client = app_client
    for path in ("/api/accounts", "/api/logs", "/api/status"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.is_json, f"{path} is not JSON: {resp.headers.get('Content-Type')}"


def test_sidebar_badge_renders_on_every_page(app_client):
    """Regression: account_count must be in the context on every page,
    not just the dashboard. (Reported and fixed during the v1.1.0 redesign.)"""
    _, client = app_client
    for path in ("/", "/accounts", "/libraries", "/logs"):
        body = client.get(path).get_data(as_text=True)
        assert "ml-auto" in body and "rounded-full" in body, (
            f"sidebar badge HTML missing on {path}"
        )


def test_renewer_state_enum_complete():
    """The State enum is the contract used by app._execute_renewal — make
    sure every member the app cares about is there."""
    from renewer import State
    expected = {"RENEWED", "NO_SESSION", "SESSION_EXPIRED",
                "LIBRARY_AUTH_FAILED", "NETWORK_ERROR", "UNEXPECTED"}
    actual = {s.name for s in State}
    missing = expected - actual
    assert not missing, f"renewer.State is missing: {missing}"


def test_add_account_with_blank_interval_creates_account(app_client, monkeypatch):
    """Regression: a blank renewal-interval override failed IntegerField
    validation silently, so pressing Create just re-rendered the form."""
    app_mod, client = app_client
    monkeypatch.setattr(app_mod, "schedule_account_renewal", lambda account: None)
    with app_mod.app.app_context():
        lib = app_mod.LibraryConfig(name="Test Library", type="test_lib",
                                    nyt_url="https://example.test/nyt",
                                    default_renewal_hours=24, active=True)
        app_mod.db.session.add(lib)
        app_mod.db.session.commit()

    resp = client.post("/accounts/add", data={
        "name": "Blank Interval",
        "library_type": "test_lib",
        "library_username": "12345",
        "library_password": "pin",
        "renewal_interval": "",
        "active": "y",
    })
    assert resp.status_code == 302, resp.get_data(as_text=True)
    with app_mod.app.app_context():
        account = app_mod.Account.query.filter_by(name="Blank Interval").one()
        assert account.renewal_interval is None


def test_access_code_library_account_needs_no_card(app_client, monkeypatch):
    """Access-code libraries have no library login, so card + PIN are optional."""
    app_mod, client = app_client
    monkeypatch.setattr(app_mod, "schedule_account_renewal", lambda account: None)
    with app_mod.app.app_context():
        lib = app_mod.LibraryConfig(
            name="Code Library", type="code_lib",
            nyt_url="https://www.nytimes.com/subscription/redeem?campaignId=X&gift_code=abc",
            default_renewal_hours=72, active=True)
        app_mod.db.session.add(lib)
        app_mod.db.session.commit()

    resp = client.post("/accounts/add", data={
        "name": "No Card", "library_type": "code_lib",
        "library_username": "", "library_password": "", "active": "y",
    })
    assert resp.status_code == 302, resp.get_data(as_text=True)
    with app_mod.app.app_context():
        account = app_mod.Account.query.filter_by(name="No Card").one()
        assert account.library_username == ""


def test_ezproxy_library_account_still_needs_card(app_client):
    app_mod, client = app_client
    with app_mod.app.app_context():
        lib = app_mod.LibraryConfig(name="Proxy Library", type="proxy_lib",
                                    nyt_url="https://example.test/login?url=nyt",
                                    default_renewal_hours=24, active=True)
        app_mod.db.session.add(lib)
        app_mod.db.session.commit()

    resp = client.post("/accounts/add", data={
        "name": "Missing Card", "library_type": "proxy_lib",
        "library_username": "", "library_password": "", "active": "y",
    })
    assert resp.status_code == 200
    assert "Required for this library." in resp.get_data(as_text=True)
    with app_mod.app.app_context():
        assert app_mod.Account.query.filter_by(name="Missing Card").first() is None
